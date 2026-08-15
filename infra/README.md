# Azure first deploy

Same three services as local compose. Azure replaces the Postgres volume and the `./storage` bind mount. n8n and Redis stay on your laptop and are not created here.

```
az login
cp infra/azure.env.example infra/azure.env   # new keys, not your laptop .env
./infra/up.sh
./infra/migrate.sh
./infra/deploy.sh
```

`up.sh` is the slow, once-only step. After that, a code change is `./infra/deploy.sh` (ACR builds `linux/amd64` — do not build those images on this Mac).

## What you put in `azure.env`

| Variable | Notes |
|---|---|
| `LLM_API_KEY` / `EMBEDDING_API_KEY` | New OpenAI keys. Same value is fine. |
| `LLAMA_CLOUD_API_KEY` | New LlamaCloud key. No Azure equivalent. |
| `SERVICE_API_KEY` | `openssl rand -hex 32` — not the local value. |
| `POSTGRES_PASSWORD` | New. Flexible Server rejects the username `postgres`; keep `POSTGRES_USER=ragadmin`. |
| `LOCATION` | `swedencentral`. If a SKU is missing, set `westeurope` and re-run `up.sh`. |

Leave `ELEVENLABS_API_KEY` unset. Do not copy n8n or Redis variables.

## Local vs Azure env (same images)

| | Local compose | Azure |
|---|---|---|
| `POSTGRES_HOST` | `postgres` | `*.postgres.database.azure.com` |
| `POSTGRES_SSL` | `prefer` | `require` |
| `INGESTION_URL` | `http://ingestion:8001` | `http://<prefix>-ingestion` (no port — Container Apps ingress) |
| `QUERY_URL` | `http://query:8002` | `http://<prefix>-query` |
| `STORAGE_DIR` | `/data/storage` | `/data/storage` on Azure Files, ingestion only |

Using `:8001` / `:8002` on Azure is how every UI action 502s.

## First boot — prove it is not an empty shell

1. Open the URL `deploy.sh` prints (`https://<web-app>.<env>.<region>.azurecontainerapps.io`).
2. **Documents list is empty.** That is the new cloud database, not a broken UI.
3. Upload a **small** PDF. Status should move `uploaded` → `processing` → `completed` or `degraded`. A 31-page paper can take minutes (LlamaParse is still outbound).
4. Ask a question. Tokens should stream. “Show source details” should list the file, a page, and `found by vector|keyword|both`.
5. Click the download link. That path is web → ingestion → Azure Files. A 502 here is a bad Files mount or a wrong `INGESTION_URL`.

If the UI loads but every action fails:

- web logs `ingestion service unreachable` → `INGESTION_URL` still has a port, or ingress is HTTPS-only between apps
- ingestion/query logs `ssl` / `password authentication` → `POSTGRES_SSL` is not `require`, or the password was not URL-safe (the app quotes it; the env value must still be the real password)
- upload 500 immediately → `migrate.sh` was skipped (`vector` / `document_chunks` missing)

## What these scripts do not do

They do not apply `03`/`04` (already in `02` on a fresh database), do not copy your local corpus, do not create n8n/Redis/Azure OpenAI, and do not add Entra or GitHub Actions.
