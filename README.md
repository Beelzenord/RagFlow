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

## Who may upload and delete

The console has two roles. Everyone gets the same chat page. An **admin** also
gets a **Chat | Dashboard** switch in the topbar. The Dashboard
(`/dashboard`) is where documents are uploaded, previewed, deleted and given an
audience, and where markets are managed. In the chat, an admin can also aim a
question at one document ("Search in") and see source details. A **reader**
asks questions and downloads the documents an answer cites, but is never shown
what else the corpus holds. To a reader the Dashboard does not exist: the
switch is not in their page, `/dashboard` sends them back to the chat, and its
script is a 404. On Azure the role comes from an Entra app role in the sign-in
token ([infra/README.md](infra/README.md)); locally there is one account and it
is the admin.

To see the reader console without a second account, set `DEV_FORCE_ROLE=reader`
in `.env` and restart the web container. The Dashboard switch, the Search-in
picker and the source-details toggle disappear. `POST /api/upload`, `DELETE
/api/documents/{id}`, `GET /api/documents` and every markets or audience write
all answer 403: the roles are enforced in the BFF, not just hidden in the page.
The switch is ignored when `AUTH_MODE=entra`, so it cannot follow you into a
deployment.

```bash
cd services/web && python3 -m unittest discover -s tests -t .
cd services/ingestion && python3 -m unittest discover -s tests -t .
cd services/query && python3 -m unittest discover -s tests -t .
```

## Markets: who each document is for

An admin adds the countries the firm operates in (ISO 3166-1, picked by name on
the Dashboard's **Markets** tab), and can name groups of them, for example
*Scandinavia = Sweden, Norway, Denmark*. Each document is then either for
**Everyone (Global)** or **only certain markets**: any mix of countries and
groups. Global is the default, so every upload, and every document that existed
before markets did, is for everyone until an admin says otherwise.

In the chat, **Answers for** lets each person pick their market; it is
remembered per browser and hidden while no markets exist. With a market picked,
answers draw on Global documents plus documents for that market. **All markets**
searches everything. Aiming a question at one document overrides the market.

This steers relevance; it is **not access control**. Anyone can switch markets,
and a reader can still download any document an answer cites.

- **Groups are live.** A group is stored by reference and resolved when a
  question is asked, so editing its countries changes every document tagged
  with it at once. The edit dialog says how many documents that is.
- **Nothing in use can be removed.** A country still in a group or named on a
  document, or a group still on a document, can't be removed. The Dashboard
  disables the button and says why, and the database's foreign keys refuse it
  regardless (409).
- **Changing who a document is for never re-processes it.** Only the audience
  rows change; it applies from the next question.

Ingestion API (all behind `x-api-key`; the web BFF proxies each one as
`/api/...`, admin-only except the trimmed `GET /api/markets`):

| Endpoint | Does |
| --- | --- |
| `GET /markets` | Countries and groups, with what uses each, plus the Global document count |
| `GET /markets/catalogue` | Every ISO country, marked `added` or not |
| `POST /markets/countries` `{"code": "SE"}` | Add a country (name taken from ISO) |
| `DELETE /markets/countries/{code}` | Remove one; 409 with the reason while in use |
| `POST /markets/groups` `{"name", "members"}` | Create a group from added countries |
| `PUT /markets/groups/{id}` `{"name", "members"}` | Rename or change members |
| `DELETE /markets/groups/{id}` | 409 while a document uses it |
| `PUT /documents/{id}/audience` `{"everyone": false, "countries": ["DE"], "groups": ["<id>"]}` | Set who a document is for |
| `PUT /documents/audience` `{"document_ids": [...], ...}` | The same for up to 500 documents, all or nothing |
| `GET /documents?q=&who=&limit=&offset=` | `who` is `global`, a country code (directly or via a group) or a group id; returns `total` |

Pass `"market": "SE"` to `POST /query` for the same filter the chat applies.

The schema is `db/migrations/05-markets.sql`. Like `03` and `04`, it only runs
by itself on a fresh Postgres volume; apply it to an existing one by hand:

```bash
docker compose exec -T postgres psql -U postgres -d postgres < db/migrations/05-markets.sql
```

## Languages

The interface speaks **English, Swedish, Norwegian (Bokmål), Finnish and
Danish**. Each person gets, in order:

1. the language they picked in the topbar (remembered per browser);
2. otherwise the first of their browser's languages that is supported;
3. otherwise English.

No Azure or Entra settings are involved. Switching takes effect at once,
without a reload, so a conversation on screen is kept.

- **The words** live in `services/web/app/locales/{en,sv,nb,fi,da}.json`.
  `en.json` is the full set of keys. Plurals are `{"one": …, "other": …}`, and
  placeholders look like `{name}`.
- **To add or change text**, edit every file. `tests/test_i18n.py` fails on a
  missing key, a mismatched placeholder, or a key a page or script uses that
  isn't in `en.json`.
- **Country names and dates** aren't in the dictionaries. The browser supplies
  them in the right language (`Intl`), so "Sverige", "Ruotsi" and "Tyskland"
  cost nothing.
- **Answers** follow the language of the question, as before; the interface
  language doesn't change them.
- **Voice** follows the interface language unless an admin picks another in
  its own dropdown. Norwegian, Finnish and Danish use the default ElevenLabs
  voice unless `ELEVENLABS_VOICE_ID_NB`, `_FI` or `_DA` is set.
- **What readers are sent:** `GET /strings.js` (`static/lang.js` is the engine)
  serves the dictionaries. A reader's copy leaves out the Dashboard's wording,
  so the Dashboard still doesn't exist from where they sit.

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
