# Document scoping — regional and future dimensions

Status: **design, not built.** Nothing in this document exists in the schema yet.
It records the decisions and the reasoning behind them so that whoever picks this
up — including a later you — does not have to re-derive them.

The problem: a global firm's policies differ by country. A German employee asking
about vacation should get the German rules, not the Swedish ones, and should not
have to know which document to look in. Some policies are global, some are
EU-wide with national exceptions, some are one country only.

## First: relevance, not confidentiality

These get conflated and they are not the same thing.

**Relevance** — the Swedish policy is not hidden from a German, it is the wrong
answer for them. Filtering it out is a *default*, and an HR person comparing
markets may legitimately widen the search.

**Confidentiality** — the Swedish policy must never reach a German, whatever they
ask and whatever they send.

|                        | Relevance            | Confidentiality        |
| ---------------------- | -------------------- | ---------------------- |
| Scope derived from     | may involve the client | the token, only        |
| Override               | allowed              | role-gated, or none    |
| Untagged document      | may fail open        | must fail closed       |
| A wrong tag is         | a bad answer         | a breach               |

**This design targets relevance.** Treating routine policy as secret creates
friction with no benefit, and the corpus today is not sensitive.

But the confidentiality placeholder is not an empty column — it is the
*derivation path*. Scope is read from the Entra token server-side from day one,
even though relevance would tolerate a client parameter. Doing it that way means
a future strict tier is *removing an override*. Deriving it from a dropdown
instead would mean rewriting, because a client-supplied filter can never become
a boundary. The same instinct as `require_voice` being separate from
`require_admin`: get the shape right before the rule matters.

The one column added now for that future is `sensitivity`, defaulting to
`normal`. When a genuinely restricted document arrives — works council
agreements, anything naming individuals — it is a flag rather than a migration
and a full reprocess.

## Vocabulary: a closed set, two kinds

Freeform tags are the failure mode to avoid. Once `sweden`, `SE` and `Sverige`
all exist in the corpus, no filtering logic recovers. Admins pick from a
vocabulary; they do not type.

**`regions`** — leaf nodes, ISO 3166-1 alpha-2. Code plus label. Exists so the
picker has a source of truth and a typo is impossible.

**`region_groups`** — named sets, so nobody ticks 27 boxes for an EU directive.

| code      | label          | members             | specificity |
| --------- | -------------- | ------------------- | ----------- |
| `GLOBAL`  | Everywhere     | —                   | 0           |
| `EU`      | European Union | AT, BE, …           | 1           |
| `NORDICS` | Nordics        | SE, NO, DK, FI, IS  | 1           |
| `DACH`    | DACH           | DE, AT, CH          | 1           |
| *(leaf)*  | Germany        | DE                  | 2           |

Specificity is the point of the whole design: `GLOBAL(0) ⊂ group(1) ⊂
country(2)`. Groups may overlap freely — Germany is in both `EU` and `DACH` —
because precedence is resolved per match, not per group.

## Schema additions to `documents`

| column                      | purpose                                                                 |
| --------------------------- | ----------------------------------------------------------------------- |
| `applies_to_regions text[]` | **expanded leaves**, GIN-indexed. `NULL`/empty means global. The hot filter. |
| `scope_entries jsonb`       | what the admin picked: `[{code:EU,kind:group,spec:1},{code:CH,kind:region,spec:2}]` |
| `scope_labels text[]`       | human labels for citations — "European Union", "Switzerland"             |
| `scope jsonb`               | generic bag for future dimensions: `{department:[…], employment_type:[…]}` |
| `sensitivity text`          | `'normal'` default; the confidentiality hook                             |
| `effective_from` / `effective_to` | optional, cheap now, saves a migration later                       |
| `supersedes uuid`           | optional; stops v1 and v2 both being retrieved as current                |

**Why region gets dedicated columns while other dimensions go in `scope` jsonb.**
Region is the dimension we know we need, and it sits on the hot query path —
array containment, a GIN index, and an `ORDER BY` on specificity. The dimensions
we might want later have no known shape and are not on that path. Optimise what
is known; keep what is not generic. Adding `department` later is then zero
migration.

**Specificity is a property of the match, not the document.** A document scoped
to `{EU, CH}` is matched by a German through `EU` (spec 1) and by a Swiss
employee through `CH` (spec 2). That is why `scope_entries` is kept rather than
collapsing to a single number on the row.

### Chunk level — already possible, no migration

`document_chunks.metadata` is an existing jsonb that currently stores `{}`. When
the mixed handbook arrives — one file with country annexes inside it — a chunk
under `## Germany` carries a region override there. Retrieval already joins
`documents`, so it coalesces: chunk override when present, document scope
otherwise.

Do not copy document scope onto every chunk. That is update amplification for no
gain, given the join is already happening.

## Query flow

1. The BFF reads the region from the Entra token. **Server-derived; never a
   client parameter.**
2. It passes region and role to the query service over the existing internal
   auth.
3. `_build_filter` adds to what it already does:
   - scope: global `OR` contains the caller's region
   - `sensitivity = 'normal'` — inert today, the hook for later
   - currently effective, if dating is in use
4. Embed the question. Unchanged.
5. Vector search over the filtered set, pulling the existing rerank pool.
6. For each hit, resolve which entry matched and its specificity.
7. Rank with tier awareness — see *Tier starvation* below.
8. Source blocks carry the scope, extending the format already built:

   ```
   [1] (Handbook.pdf, p.3 — Vacation) — Global
   [2] (DE-Annex.pdf, p.1 — Urlaub) — Germany
   ```

9. The system prompt states the precedence rule: where sources conflict, the more
   specific scope governs; say which one applied; if only a less-specific source
   exists, say that too.
10. The answer opens with the rule it used — "Under the German annex…". This
    matters most in voice mode, where no citations are shown.

**Filtering alone is not enough.** A German asking about vacation may legitimately
match all three tiers: the global handbook, the EU directive, the German annex.
Retrieving only the narrowest is wrong — the annex may cover only the exception
while the handbook covers everything else. Retrieve across tiers, label them, and
let the model apply precedence. That mirrors how the documents are actually meant
to be read.

## Tagging flow

1. Upload **requires** a scope choice. No silent default.
2. On save, groups expand to leaf regions; entries and labels are stored.
3. The admin list shows a scope badge, and untagged documents are surfaced rather
   than buried.
4. When group membership changes — a country joins the EU — an admin action
   re-expands the affected documents.

**Retagging never requires reprocessing.** Scope is metadata, not vectors: no
LlamaParse, no re-embedding, effective immediately. Unlike changing the embedding
model, a wrong tag is cheap to fix. That should lower the stakes on rollout
considerably.

## Identity: a claim, not a group

`infra/README.md` explains why app roles were chosen over group IDs: a user in
many groups has their group claim replaced by a "look it up yourself" pointer.
**That same overage applies to region-by-group.** Do not put region in groups for
exactly the reason roles were kept out of them.

Use an extension attribute on the user, mapped into the token as a custom claim.
One claim, always present, no overage.

If the claim is missing, fall back to **global only** — the user sees global
policy and no country-specific content. Safe, and visibly degraded so that
somebody reports it. Never infer a region from locale, IP or language.

## Known traps

### Tier starvation

A German employee asks *"How many vacation days do I get?"*. The corpus holds:

- **Global Employee Handbook** — 40 pages, whose vacation section spans roughly
  fifteen chunks: entitlement principles, how to request leave, carryover,
  accrual, part-time proration, public holidays.
- **German Annex** — 2 pages, one relevant chunk: *"Urlaubsanspruch: gesetzliches
  Minimum 20 Tage, das Unternehmen gewährt 30."*

Scope filtering does its job exactly. `{global, DE}` — no Swedish content comes
near the candidate set. Then similarity ranking runs, and it knows nothing about
specificity:

| rank | chunk                              | scope   |
| ---- | ---------------------------------- | ------- |
| 1    | Handbook — Vacation overview       | Global  |
| 2    | Handbook — Requesting vacation     | Global  |
| 3    | Handbook — Carryover rules         | Global  |
| 4    | Handbook — Accrual                 | Global  |
| 5    | Handbook — Part-time proration     | Global  |
| 6    | Handbook — Public holidays         | Global  |
| **7**| **German Annex — 30 days**         | **Germany** |

Six slots, and the one chunk that answers the question is ranked out. The
employee gets entitlement principles, or a global default that is wrong for them.

**The filter worked perfectly and the answer is still wrong.** That is the whole
point: a verbose general document simply has more chances to score well, so the
specific tier gets starved of slots by the general one. Filtering is necessary
and is not sufficient.

Mitigation is at ranking time — guarantee slots for the most specific tier that
has any match, either by reserving them in the existing pool or by a second
bounded query merged in. Which is right depends on the real ratio of global to
national content, so this wants evidence before a choice.

### The mixed document

Scope is tagged per document, which is fine while each file covers one scope.
Then HR publishes what global firms actually publish — one PDF, 80 pages:

```
Global Employee Handbook
  Chapters 1-10   global policies
  Appendix A      Sweden-specific variations
  Appendix B      Germany-specific variations
  Appendix C      France-specific variations
```

Every candidate tag fails:

| tag          | result                                                              |
| ------------ | ------------------------------------------------------------------- |
| `GLOBAL`     | the German sees it, and also retrieves Appendix A — the Swedish rules, which is precisely what this was built to prevent |
| `{SE,DE,FR}` | same failure; everyone retrieves every appendix                      |
| `DE`         | Swedes lose the entire handbook                                      |

**There is no correct document-level tag**, because the document contains several
scopes at once. This is a structural limit of the mechanism rather than a bug in
it: the design is complete and coherent right up until this file arrives, and
then it has no answer.

Options, in order of effort: require admins to split it, which is arguably honest
since the annexes *are* separate documents; infer scope per chunk at ingestion,
since the chunker already records `heading` and a chunk under *"Appendix B:
Germany"* can inherit `DE`; or tag ranges after ingestion. Start document-level
knowing this is coming — `document_chunks.metadata` already exists and holds
`{}` today, so the column this will need is in place.

**Freeform tags.** Stated again because it is the one mistake that cannot be
fixed later by code.

## Cross-region access

An HR person comparing markets is a real need, and under relevance it is easy. It
should be **explicit** ("Compare with: Sweden", never a silent widening),
**attributed in the answer** ("This is the Swedish policy; it does not apply to
you"), and probably **narrow** — admin only, using the role machinery that already
exists. Building it explicit now means a future confidentiality tier adds a guard
rather than forcing a redesign.

## Migration shape

Every addition is a nullable column or a small lookup table. Existing documents
land as `applies_to_regions = NULL`, which reads as global and stays visible to
everyone exactly as today. **The change is non-breaking on day one** and retagging
proceeds at its own pace. That is the argument for landing the schema before the
UI.

## Verifying it before anyone relies on it

- **"View as region DE"** for admins — the `DEV_FORCE_ROLE` trick applied to
  scope, and the cheapest possible confidence check.
- **Coverage matrix** — topics against regions, showing empty cells. Turns
  tagging from a chore into gap-finding.
- **A fixture set** — roughly ten questions per region with the expected source
  document, so a retagging mistake surfaces as a failing check rather than as a
  wrong answer to an employee.

## Open questions

These are genuinely unsettled, not rhetorical.

1. **Tier-starvation strategy** — reserved slots in the rerank pool, or a second
   bounded query per tier? Needs real data to choose.
2. **Chunk-level timing** — is the mixed handbook a day-one problem or a later
   one? Depends on whether admins can be asked to split files.
3. **Vocabulary ownership** — who maintains `region_groups`, and does it need a
   UI or is a migration acceptable?
4. **Effective dating** — worth landing with the scope work, or a separate pass?
   It shares the metadata plumbing but has its own UI cost.
5. **Multiple dimensions at once** — when department arrives, do dimensions AND
   together, and does specificity then need to be per-dimension?
