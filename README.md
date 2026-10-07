# Document RAG MVP

Scalable document RAG with **ingestion-time** parsing, chunking, and embedding so
queries stay fast. n8n orchestrates HTTP calls to two Python services.

```
Client → n8n ──┬─▶ ingestion-svc (FastAPI)  → LlamaParse → chunk → embed → Postgres/pgvector
               └─▶ query-svc     (FastAPI)  → embed q → top-k → LLM → answer + citations
```

## Stack

- **n8n** (self-hosted) — orchestration only, no parsing logic in Code nodes
- **PostgreSQL 16 + pgvector** — `documents`, `document_chunks`, `ingestion_jobs`
- **Redis** — wired up for n8n queue mode and future Python job queue
- **FastAPI** ingestion + query services (Python 3.11)
- **LlamaParse** for PDF / image → markdown
- **OpenAI-compatible** embeddings + LLM (Anthropic also supported for the LLM)

## Layout

```
db/migrations/        SQL run on first DB init
services/shared/      rag_shared package (settings, db, embeddings, llm, chunking)
services/ingestion/   FastAPI: /ingest, /documents, retag, /regions, /documents/{id}/reprocess
services/query/       FastAPI: /query
n8n/workflows/        Importable JSON workflows
scripts/smoke_test.sh End-to-end test
assets/documents/     versioned source PDFs (upload via the UI or smoke_test)
storage/              originals/  markdown/   (runtime ingest output; bind-mounted)
```

## First-time setup

1. **Get fresh API keys** (rotate any that have been pasted into chats).
   - LlamaCloud: https://cloud.llamaindex.ai
   - OpenAI (or another OpenAI-compatible provider) for embeddings + LLM.
2. Copy and fill the env file:
   ```bash
   cp .env.example .env
   ```
   At minimum set: `LLAMA_CLOUD_API_KEY`, `LLM_API_KEY`, `EMBEDDING_API_KEY`,
   `N8N_ENCRYPTION_KEY`, `SERVICE_API_KEY`. Set `ADMIN_PASSWORD` too if the web
   console should ask for a login — left empty, it is open to anyone who can
   reach the port. (On Azure the login is Microsoft Entra instead; see
   [infra/README.md](infra/README.md).)
3. Bring up the stack:
   ```bash
   docker compose up -d --build
   ```
4. Verify:
   ```bash
   curl http://localhost:8001/healthz
   curl http://localhost:8002/healthz
   open http://localhost:8080        # web console (login if ADMIN_PASSWORD is set)
   open http://localhost:5678        # n8n UI
   ```

## Embedding dimension

`document_chunks.embedding` is `vector(1536)` (matches `text-embedding-3-small`).
If you switch embedding models, run a migration to alter the column dimension
**and** reprocess every document — old vectors won't be comparable.

## Ingest a file

```bash
curl -X POST http://localhost:8001/ingest \
  -H "x-api-key: $SERVICE_API_KEY" \
  -F "file=@brochure.pdf" \
  -F "collection=marketing" \
  -F "scope=EU,CH"
# → {"document_id": "…", "status": "processing"}
```

`scope` is a comma-separated list of region or group codes from the vocabulary
in `db/migrations/05-document-scoping.sql` — `GLOBAL`, `EU`, `NORDICS`, `DACH`,
or any ISO country code seeded there. Groups are expanded to their member
countries when the document is tagged, so `EU` stores 27 rows in
`applies_to_regions` and the query path stays a plain array overlap. An unknown
code is refused with a 400 rather than being widened to global.

It is **optional at the API** so this command, `scripts/smoke_test.sh` and the
n8n ingest workflow all keep working, but **required in the web console**, where
a person is making the choice. An upload with no scope is global and is listed
as `unscoped`, not as a deliberate decision — see
[docs/document-scoping.md](docs/document-scoping.md).

**The retrieval filter does not use any of this yet.** Tagging exists so that it
can be done and checked before a tagging mistake can produce a wrong answer.

### Duplicates

Uploading bytes that are already in the corpus answers **409** with the document
that holds them, instead of parsing and indexing the file a second time:

```json
{"detail": {"message": "already uploaded as faktura.pdf", "document_id": "…", "original_filename": "faktura.pdf"}}
```

Send `-F "allow_duplicate=true"` to upload it anyway — `scripts/smoke_test.sh`
does, because it re-uploads the same file every run. The n8n ingest workflow does
not, so a re-upload through it now gets the 409 too. Documents uploaded before
migration 06 have no fingerprint until you run the backfill once (also a button on
the filing page):

```bash
curl -X POST -H "x-api-key: $SERVICE_API_KEY" http://localhost:8001/documents/backfill-fingerprints
```

### Retagging

Scope is metadata, not vectors, so changing it re-parses and re-embeds nothing —
the chunk count stays as it is and no ingestion job runs:

```bash
curl -X PATCH http://localhost:8001/documents/<id>/scope \
  -H "x-api-key: $SERVICE_API_KEY" -H "content-type: application/json" \
  -d '{"scope": "EU,CH"}'

# Several at once - all or nothing:
curl -X PATCH http://localhost:8001/documents/scope \
  -H "x-api-key: $SERVICE_API_KEY" -H "content-type: application/json" \
  -d '{"document_ids": ["<id>", "<id>"], "scope": "NORDICS"}'
```

An empty scope is refused on a retag although `/ingest` accepts one: on upload it
means a caller that does not know about scoping, but on a retag it can only be a
blank submit. Use `GLOBAL` for everywhere.

### Countries

Migration 07 holds every ISO 3166-1 country (plus Kosovo, `XK`, which is
user-assigned rather than official), with the 32 from the starter vocabulary
enabled. Only enabled countries are offered in the pickers, and tagging a
disabled one is refused with a message saying so. Enable a market from the
**Countries** tab of the filing page, or:

```bash
curl -H "x-api-key: $SERVICE_API_KEY" http://localhost:8001/regions          # catalogue + usage
curl -X PATCH http://localhost:8001/regions/JP \
  -H "x-api-key: $SERVICE_API_KEY" -H "content-type: application/json" \
  -d '{"active": true}'
```

Enabling is always safe. Disabling is refused with **409** while any document or
group still uses the country — retag those documents first. Groups themselves are
still edited by migration: changing a group's members does not update documents
already tagged with it, so that needs its own re-expansion step before it can be a
button. See [docs/document-scoping.md](docs/document-scoping.md).

Read the vocabulary the pickers are built from (enabled countries only):

```bash
curl -H "x-api-key: $SERVICE_API_KEY" http://localhost:8001/scopes
```

### Listing and filtering

`GET /documents` with no parameters answers as it always has — newest first, up
to 200 — which is what the console sidebar uses. The filing page adds:

| param | |
| --- | --- |
| `q` | filename contains |
| `scope` | tagged with a code (`EU`, `DE`, `GLOBAL`), or `unscoped` |
| `status` | `uploaded`, `processing`, `completed`, `degraded`, `failed` |
| `sort` | `created_desc`, `created_asc`, `filename`, `attention` (failed, then unscoped, then degraded) |
| `limit`, `offset` | paging; the response carries `total` |

`scope` matches what a document was *tagged* with: `EU` finds documents whose
admin picked EU, not every document that happens to cover an EU country.

Poll status:

```bash
curl -H "x-api-key: $SERVICE_API_KEY" \
     http://localhost:8001/documents/<id>
```

Reprocess (e.g. after switching embedding model):

```bash
curl -X POST -H "x-api-key: $SERVICE_API_KEY" \
     http://localhost:8001/documents/<id>/reprocess
```

## Query

```bash
curl -X POST http://localhost:8002/query \
  -H "x-api-key: $SERVICE_API_KEY" \
  -H "content-type: application/json" \
  -d '{"question": "What does the brochure say about pricing?", "collection": "marketing"}'
```

Response:

```json
{
  "answer": "The brochure lists three tiers… [1][2]",
  "citations": [
    {"n": 1, "document_id": "…", "filename": "brochure.pdf", "page_number": 3, "heading": "Pricing", "score": 0.83},
    ...
  ]
}
```

## n8n workflows

In the n8n UI (http://localhost:5678) → **Workflows → Import from File** and
select each file from `n8n/workflows/`. Two workflows ship out of the box:

| Workflow             | Webhook path        | Purpose                              |
| -------------------- | ------------------- | ------------------------------------ |
| RAG · Ingest Document| `POST /webhook/rag/ingest` | Forwards a binary upload to `/ingest` |
| RAG · Ask Question   | `POST /webhook/rag/ask`    | Forwards a JSON question to `/query`  |

Both workflows read `INGESTION_URL`, `QUERY_URL`, and `SERVICE_API_KEY` from
n8n's env (set in `docker-compose.yml`).

## Who may upload and delete

The console has two roles. An **admin** gets the whole console: upload, the
documents list, per-document scoping and source details. A **reader** gets a
centred chat and nothing else — they ask questions and download the documents an
answer cites, but they are never shown what else the corpus holds. On Azure the
role comes from an Entra app role in the sign-in token
([infra/README.md](infra/README.md)); locally there is one account and it is the
admin.

To see the reader console without a second account, set `DEV_FORCE_ROLE=reader`
in `.env` and restart the web container. The sidebar, the Scope picker and the
source-details toggle disappear, and `POST /api/upload`, `DELETE
/api/documents/{id}` and `GET /api/documents` all answer 403 — the roles are
enforced in the BFF, not just hidden in the page. The switch is ignored when
`AUTH_MODE=entra`, so it cannot follow you into a deployment.

### The filing page

Admins get **Filing** at `/admin` (linked from the Documents panel): search and
filter the whole corpus, retag one document or a selection, delete, check older
files for duplicates, and enable countries. It opens on *needs attention first*,
so failed and unscoped documents are at the top.

For a reader it does not exist: `/admin` redirects to the chat and its script is
a 404. The page lives in `services/web/app/admin/`, outside `static/`, because
the static mount serves everything in there to every signed-in user.

```bash
cd services/web && python3 -m unittest discover -s tests -t .
cd services/ingestion && python3 -m unittest discover -s tests -t .
```

## Smoke test

```bash
export SERVICE_API_KEY=$(grep ^SERVICE_API_KEY .env | cut -d= -f2)
./scripts/smoke_test.sh sample.pdf "Summarize this document"
```

## Scaling notes (already designed in)

- **Ingestion is async** — `/ingest` returns 202 immediately and a
  `BackgroundTasks` worker drives the pipeline. To move to a real queue, swap
  the `BackgroundTasks` call in `services/ingestion/app/main.py` for an RQ /
  Arq / Celery enqueue against Redis. The pipeline function `run_ingestion`
  is already side-effect-isolated and idempotent.
- **More workers**: scale ingestion containers horizontally
  (`docker compose up -d --scale ingestion=3`). Postgres becomes the
  serialization point.
- **Metadata filters**: `documents` carries `user_id`, `collection`, and a
  `metadata` jsonb column. `/query` accepts `document_id` / `collection` /
  `user_id` filters today; extend in `services/query/app/main.py:_build_filter`
  for arbitrary jsonb predicates.
- **n8n queue mode**: set `EXECUTIONS_MODE=queue` in `.env` and add worker
  containers using the same image with `command: ["worker"]`.

## Avoided anti-patterns

- ❌ No parsing/OCR/chunking at query time — all preprocessed during ingestion.
- ❌ No heavy logic in n8n Code nodes — n8n just calls HTTP endpoints.
- ❌ No vectors-only storage — original file, markdown, and chunk metadata are
  all preserved (`storage/originals/`, `storage/markdown/`, `documents.markdown_text`).
- ❌ No silent failures — failures land in `documents.error_message` and
  `ingestion_jobs.error_message`, and the document is reprocess-able.

## Azure

First cloud deploy (Container Apps + Flexible Server + Azure Files) is
documented in [`infra/README.md`](infra/README.md). The Documents list is empty
on first boot — upload a PDF after `./infra/deploy.sh`, then ask.

## Production checklist

- [ ] Rotate any API keys ever pasted into a chat or commit.
- [ ] Replace the bind-mounted `./storage` with S3 / MinIO and update
      `services/ingestion/app/storage.py`.
- [ ] Put a reverse proxy (Caddy / Traefik) in front of n8n and the FastAPI
      services; terminate TLS there.
- [ ] Tighten `SERVICE_API_KEY` rotation; store in a secrets manager.
- [ ] Replace `BackgroundTasks` with Arq/RQ for retry semantics + visibility.
- [ ] Add Prometheus metrics endpoint to both services.
