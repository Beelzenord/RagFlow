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
services/ingestion/   FastAPI: /ingest, /documents/{id}, /documents/{id}/reprocess
services/query/       FastAPI: /query
n8n/workflows/        Importable JSON workflows
scripts/smoke_test.sh End-to-end test
storage/              originals/  markdown/   (bind-mounted into ingestion)
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
   `N8N_ENCRYPTION_KEY`, `SERVICE_API_KEY`.
3. Bring up the stack:
   ```bash
   docker compose up -d --build
   ```
4. Verify:
   ```bash
   curl http://localhost:8001/healthz
   curl http://localhost:8002/healthz
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
  -F "collection=marketing"
# → {"document_id": "…", "status": "processing"}
```

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

## Production checklist

- [ ] Rotate any API keys ever pasted into a chat or commit.
- [ ] Replace the bind-mounted `./storage` with S3 / MinIO and update
      `services/ingestion/app/storage.py`.
- [ ] Put a reverse proxy (Caddy / Traefik) in front of n8n and the FastAPI
      services; terminate TLS there.
- [ ] Tighten `SERVICE_API_KEY` rotation; store in a secrets manager.
- [ ] Replace `BackgroundTasks` with Arq/RQ for retry semantics + visibility.
- [ ] Add Prometheus metrics endpoint to both services.
