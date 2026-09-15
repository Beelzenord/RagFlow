"""Turn the codes an admin picked into the columns retrieval will filter on.

Groups are expanded to leaf regions here, at tag time, rather than being
resolved on every query. That keeps the read path a plain array overlap and
follows the principle the rest of this system is built on: expensive work during
ingestion, cheap reads at query time. The cost is re-expanding when a group's
membership changes, which is rare and is an admin action.

Specificity is carried per entry rather than collapsed onto the row, because it
is a property of the *match*: a document scoped to {EU, CH} is matched by a
German through EU and by a Swiss employee through CH, and precedence later
depends on which one it was.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

# Everywhere. Stored as the absence of a region constraint rather than as a
# member of the array, so it cannot be misspelled into a new region.
GLOBAL_CODE = "GLOBAL"

# Leaf regions are always the narrowest tier. Held as a constant rather than a
# column because it would be the same value on every row.
REGION_SPECIFICITY = 2


class ScopeError(ValueError):
    """A scope the vocabulary does not accept. Caller answers 400.

    Never widened into a default: a typo that silently became a global document
    is the failure this whole mechanism exists to avoid.
    """


@dataclass(frozen=True)
class ResolvedScope:
    """What the three scope columns should be set to. `regions is None` is global."""

    regions: list[str] | None
    entries: list[dict[str, Any]]
    labels: list[str]

    @property
    def is_global(self) -> bool:
        return self.regions is None


def parse_codes(raw: str | None) -> list[str]:
    """Split the comma-separated form field, matching ALLOWED_MIME_TYPES' style.

    Order is preserved and duplicates dropped, so the entries a person sees back
    are the ones they picked, in the order they picked them.
    """
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for part in raw.split(","):
        code = part.strip().upper()
        if code and code not in seen:
            seen.add(code)
            out.append(code)
    return out


async def resolve_scope(session, raw: str | None) -> ResolvedScope:
    """Expand picked codes into regions, entries and labels.

    Empty input is global, which is what an untagged upload from the smoke test
    or an n8n workflow means. The console requires a choice; the API does not,
    so machine callers keep working.
    """
    codes = parse_codes(raw)
    if not codes:
        return ResolvedScope(regions=None, entries=[], labels=[])

    if GLOBAL_CODE in codes:
        if len(codes) > 1:
            raise ScopeError(
                f"{GLOBAL_CODE} cannot be combined with other scopes - it already covers them"
            )
        return ResolvedScope(
            regions=None,
            entries=[{"code": GLOBAL_CODE, "kind": "global", "specificity": 0}],
            labels=["Everywhere"],
        )

    groups = {
        r["code"]: r
        for r in (
            await session.execute(
                text(
                    "SELECT code, label, member_regions, specificity "
                    "FROM region_groups WHERE code = ANY(:codes)"
                ),
                {"codes": codes},
            )
        ).mappings()
    }
    regions = {
        r["code"]: r
        for r in (
            await session.execute(
                text("SELECT code, label FROM regions WHERE code = ANY(:codes)"),
                {"codes": codes},
            )
        ).mappings()
    }

    unknown = [c for c in codes if c not in groups and c not in regions]
    if unknown:
        raise ScopeError(f"unknown scope(s): {', '.join(unknown)}")

    leaves: list[str] = []
    entries: list[dict[str, Any]] = []
    labels: list[str] = []
    for code in codes:
        if code in groups:
            row = groups[code]
            members = list(row["member_regions"] or [])
            # A group with no members would silently scope the document to
            # nothing, which reads as "nobody can find it" rather than as an
            # error. Say so instead.
            if not members:
                raise ScopeError(f"scope {code} has no member regions")
            leaves.extend(members)
            entries.append(
                {"code": code, "kind": "group", "specificity": int(row["specificity"])}
            )
            labels.append(row["label"])
        else:
            row = regions[code]
            leaves.append(code)
            entries.append(
                {"code": code, "kind": "region", "specificity": REGION_SPECIFICITY}
            )
            labels.append(row["label"])

    # Groups overlap - Germany is in both EU and DACH - so the leaf list has to
    # be deduplicated. Sorted so two equivalent pickings store identically and
    # diffs stay readable.
    return ResolvedScope(regions=sorted(set(leaves)), entries=entries, labels=labels)
