# Azure first deploy

Same three services as local compose. Azure replaces the Postgres volume and the `./storage` bind mount. n8n and Redis stay on your laptop and are not created here.

```
az login
cp infra/azure.env.example infra/azure.env   # new keys, not your laptop .env
./infra/up.sh
./infra/migrate.sh
./infra/deploy.sh
```

`up.sh` is the slow, once-only step. Safe to re-run after a partial failure — existing resources are reused. After that, a code change is `./infra/deploy.sh` (ACR builds `linux/amd64` — do not build those images on this Mac).

## What you put in `azure.env`

| Variable | Notes |
|---|---|
| `LLM_API_KEY` / `EMBEDDING_API_KEY` | New OpenAI keys. Same value is fine. |
| `LLAMA_CLOUD_API_KEY` | New LlamaCloud key. No Azure equivalent. |
| `SERVICE_API_KEY` | `openssl rand -hex 32` — not the local value. |
| `POSTGRES_PASSWORD` | New. Flexible Server rejects the username `postgres`; keep `POSTGRES_USER=ragadmin`. |
| `AUTH_MODE` | `entra` (Microsoft sign-in) or `password` (the app's own shared login). |
| `ADMIN_PASSWORD` / `SESSION_SECRET` | Only for `AUTH_MODE=password`. Required in that mode — the web app has a public URL. |
| `LOCATION` | `swedencentral`. If a SKU is missing, set `westeurope` and re-run `up.sh`. |

Leave `ELEVENLABS_API_KEY` unset. Do not copy n8n or Redis variables.

## Sign-in (`AUTH_MODE=entra`)

Container Apps authentication ("Easy Auth") signs people in with their work account before a request reaches the container. Enable it on the **web** app only, once, in the portal:

1. Container App > Settings > **Authentication** > Add identity provider > **Microsoft**.
2. Set unauthenticated requests to **Redirect to login page**, so visitors get the Microsoft prompt instead of a bare redirect.
3. In Entra > Enterprise applications > the new app > Properties, set **Assignment required = Yes**.
4. Users and groups > assign your access group (e.g. `RAG Console Users`).

Access is then a group membership change. `deploy.sh` checks Easy Auth is enabled before it builds anything and refuses to deploy `entra` mode without it, so the app cannot end up public by accident.

The app also rejects any request that arrives without the platform's principal header, which means a mistaken "allow unauthenticated" setting fails closed rather than publishing the corpus. That header is trustworthy only because Easy Auth strips client-supplied copies — which is why `AUTH_MODE=entra` must never be used locally.

`ADMIN_PASSWORD` is ignored in this mode, and `deploy.sh` removes it from the web app so nobody meets two login screens. Local compose keeps the password gate.

## Who may upload and delete

Signing in gets someone in; an **app role** decides what they may do. Two roles, on the same app registration Easy Auth uses:

| Role | May |
|---|---|
| `Admin` | Upload, delete, browse the documents list, scope a question to one document, see source details |
| `Reader` | Ask questions and download the documents an answer cites — nothing that reveals the rest of the corpus |

Define them once — Entra > App registrations > the app > **App roles** > Create app role — with **Allowed member types = Users/Groups** and the value spelled exactly `Admin` and `Reader`. Then Enterprise applications > the same app > **Users and groups** and assign your admin group to `Admin` and everyone else to `Reader`.

Roles rather than group IDs on purpose: a role keeps working when a group is renamed or replaced, and it survives in the token where a person in many groups would have their group claim dropped for an unhelpful "look it up yourself" pointer instead.

Two things that surprise people:

- **Assigning a *group* to an app role needs an Entra ID P1 licence.** Without one, assign individual users, or keep everyone at the default and grant `Admin` per person.
- **A new role is not retroactive.** Anyone already signed in keeps the token they have, so they must sign out (`/.auth/logout`) and back in before it takes effect.

An account that holds neither role can read and nothing more, so forgetting an assignment is never an accidental promotion — the web app logs the name it saw. Upload, delete and the documents list all check the role themselves; the UI hiding a control only stops mistakes, not requests. If the roles must be named differently, set `ENTRA_ADMIN_ROLE` and `ENTRA_READER_ROLE` in `azure.env`.

## Local vs Azure env (same images)

| | Local compose | Azure |
|---|---|---|
| `POSTGRES_HOST` | `postgres` | `*.postgres.database.azure.com` |
| `POSTGRES_SSL` | `prefer` | `require` |
| `INGESTION_URL` | `http://ingestion:8001` | `http://<prefix>-ingestion` (no port — Container Apps ingress) |
| `QUERY_URL` | `http://query:8002` | `http://<prefix>-query` |
| `STORAGE_DIR` | `/data/storage` | `/data/storage` on Azure Files, ingestion only |
| `AUTH_MODE` | `password` | `entra` (Easy Auth in front) |

Using `:8001` / `:8002` on Azure is how every UI action 502s.

## First boot — prove it is not an empty shell

1. Open the URL `deploy.sh` prints (`https://<web-app>.<env>.<region>.azurecontainerapps.io`).
2. You get the Microsoft prompt and sign in with your work account. The topbar then shows your address; `AUTH_MODE=password` shows the app's own form instead.
3. **Documents list is empty.** That is the new cloud database, not a broken UI.
4. Upload a **small** PDF. Status should move `uploaded` → `processing` → `completed` or `degraded`. A 31-page paper can take minutes (LlamaParse is still outbound).
5. Ask a question. Tokens should stream. “Show source details” should list the file, a page, and `found by vector|keyword|both`.
6. Click the download link. That path is web → ingestion → Azure Files. A 502 here is a bad Files mount or a wrong `INGESTION_URL`.

If the UI loads but every action fails:

- web logs `ingestion service unreachable` → `INGESTION_URL` still has a port, or ingress is HTTPS-only between apps
- ingestion/query logs `ssl` / `password authentication` → `POSTGRES_SSL` is not `require`, or the password was not URL-safe (the app quotes it; the env value must still be the real password)
- upload 500 immediately → `migrate.sh` was skipped (`vector` / `document_chunks` missing)
- every page redirects to `/.auth/login/aad` in a loop → Easy Auth is enabled but not returning a principal header (check the identity provider is Microsoft and the token store is on)

Sign-in gates the browser only. `ingestion` and `query` stay internal and keep their own `x-api-key` check.

Everyone who can sign in still sees the same corpus: Entra decides *who gets in*, not which documents they may read. Per-group document scoping is a separate change.

## Cold start

`query` and `ingestion` both keep one warm replica; `web` still scales to zero.
Query is pinned because it is the only service a user waits on synchronously —
a scale-from-zero start would land entirely in the first question's latency.
Ingestion is pinned for a different reason: it runs its pipeline in FastAPI
`BackgroundTasks`, which hold no in-flight HTTP request, so a replica scaled to
zero can be reclaimed mid-parse and silently drop the job. It can safely drop to
0 once ingestion moves to the Redis queue.

The query service also warms its connection pool and the HNSW index at startup
(`_warm_retrieval` in `services/query/app/main.py`), so the first real question
does not pay for a cold pool or an index read off disk. It reuses a stored
embedding as the probe vector, so it costs no API tokens.

### TODO — tune the scale-down cooldown (not implemented)

An alternative to a permanently warm replica: raise the scale rule's
`cooldownPeriod` (default 300s) to roughly an hour, so a replica stays warm
through a working session and still drops to zero overnight. Cheaper than
always-on, at the cost of a cold first question each morning. It lives under
`properties.template.scale` and likely needs `az containerapp update --yaml`
rather than a CLI flag — verify against the current API version before relying
on it.

Do **not** solve this with a cron keep-alive ping. With a 300s cooldown that
means pinging every ~4 minutes, which keeps a replica up around the clock
anyway, but billed at the active rate rather than the cheaper idle rate a
`minReplicas` replica gets — more expensive than simply pinning it, plus churn.

## What these scripts do not do

They do not apply `03`/`04` (already in `02` on a fresh database), do not copy your local corpus, do not create n8n/Redis/Azure OpenAI, and do not add GitHub Actions. They do not create the Entra app registration or the access group either — that part is the portal steps above; the scripts only verify Easy Auth is on.
