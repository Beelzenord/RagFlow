# Shared by up.sh / migrate.sh / deploy.sh. Source only, do not run.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${AZURE_ENV_FILE:-$SCRIPT_DIR/azure.env}"

die() { echo "error: $*" >&2; exit 1; }

need() {
  local name="$1"
  local val="${!name:-}"
  [[ -n "$val" ]] || die "$name is empty — set it in $ENV_FILE"
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing command: $1"
}

load_azure_env() {
  [[ -f "$ENV_FILE" ]] || die "missing $ENV_FILE — copy infra/azure.env.example and fill it in"
  # shellcheck disable=SC1090
  set -a
  source "$ENV_FILE"
  set +a

  LOCATION="${LOCATION:-swedencentral}"
  RESOURCE_GROUP="${RESOURCE_GROUP:-rg-ragflow}"
  PREFIX="${PREFIX:-ragflow}"
  FILE_SHARE="${FILE_SHARE:-rag-storage}"
  ACA_STORAGE_NAME="${ACA_STORAGE_NAME:-ragfiles}"
  POSTGRES_USER="${POSTGRES_USER:-ragadmin}"
  POSTGRES_DB="${POSTGRES_DB:-rag}"
  # entra: Container Apps authentication signs people in with their work
  # account. password: the app's own shared login. Defaults to entra because the
  # web app has a public FQDN, and deploy.sh refuses to ship entra mode unless
  # Easy Auth is actually enabled.
  AUTH_MODE="${AUTH_MODE:-entra}"
  case "$AUTH_MODE" in
    entra|password) ;;
    *) die "AUTH_MODE must be entra or password, got '$AUTH_MODE'" ;;
  esac

  need_cmd az
  az account show >/dev/null 2>&1 || die "not logged in — run: az login"

  local sub
  sub="$(az account show --query id -o tsv)"
  local suffix
  suffix="$(printf '%s' "$sub$RESOURCE_GROUP$PREFIX" | shasum -a 256 | cut -c1-6)"

  # ACR: alphanumeric only, 5-50 chars. Storage: 3-24 lowercase alphanumeric.
  ACR_NAME="${ACR_NAME:-${PREFIX}acr${suffix}}"
  STORAGE_ACCOUNT="${STORAGE_ACCOUNT:-${PREFIX}st${suffix}}"
  POSTGRES_SERVER="${POSTGRES_SERVER:-${PREFIX}-pg-${suffix}}"
  ACA_ENV="${ACA_ENV:-${PREFIX}-env}"

  WEB_APP="${PREFIX}-web"
  INGEST_APP="${PREFIX}-ingestion"
  QUERY_APP="${PREFIX}-query"

  ACR_LOGIN_SERVER="${ACR_NAME}.azurecr.io"
  POSTGRES_FQDN="${POSTGRES_SERVER}.postgres.database.azure.com"
}

require_app_secrets() {
  need POSTGRES_PASSWORD
  need LLM_API_KEY
  need EMBEDDING_API_KEY
  need LLAMA_CLOUD_API_KEY
  need SERVICE_API_KEY
  # The web app has a public FQDN; refuse to ship it without a login gate. In
  # entra mode the gate is Easy Auth, checked separately by require_easy_auth.
  if [[ "$AUTH_MODE" == "password" ]]; then
    need ADMIN_PASSWORD
    need SESSION_SECRET
  fi
}

# AUTH_MODE=entra means the app trusts the platform to have signed the user in.
# If Easy Auth is off, that assumption is wrong, so check before deploying.
require_easy_auth() {
  [[ "$AUTH_MODE" == "entra" ]] || return 0

  local enabled action
  enabled="$(az containerapp auth show --name "$WEB_APP" --resource-group "$RESOURCE_GROUP" \
    --query 'platform.enabled' -o tsv 2>/dev/null || true)"
  if [[ "$enabled" != "true" ]]; then
    die "AUTH_MODE=entra but Container Apps authentication is not enabled on $WEB_APP.
  Enable it (portal: Container App > Settings > Authentication > Add identity provider >
  Microsoft), or set AUTH_MODE=password in $ENV_FILE to use the app's own login."
  fi

  action="$(az containerapp auth show --name "$WEB_APP" --resource-group "$RESOURCE_GROUP" \
    --query 'globalValidation.unauthenticatedClientAction' -o tsv 2>/dev/null || true)"
  # The app rejects requests without a principal header regardless, so this is a
  # bad prompt rather than an open door: anonymous visitors get a bare 302
  # instead of the Microsoft sign-in page.
  if [[ "$action" == "AllowAnonymous" ]]; then
    echo "  warning: unauthenticatedClientAction=AllowAnonymous - set it to RedirectToLoginPage" >&2
  fi
}

acr_creds() {
  ACR_USER="$(az acr credential show -n "$ACR_NAME" --query username -o tsv)"
  ACR_PASS="$(az acr credential show -n "$ACR_NAME" --query 'passwords[0].value' -o tsv)"
}

# Space-separated KEY=VAL list for az containerapp --env-vars / --set-env-vars.
python_env_vars() {
  cat <<EOF
POSTGRES_HOST=$POSTGRES_FQDN
POSTGRES_PORT=5432
POSTGRES_DB=$POSTGRES_DB
POSTGRES_USER=$POSTGRES_USER
POSTGRES_PASSWORD=secretref:postgres-password
POSTGRES_SSL=require
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o-mini
LLM_API_KEY=secretref:llm-api-key
LLM_BASE_URL=https://api.openai.com/v1
EMBEDDING_PROVIDER=openai
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIM=1536
EMBEDDING_API_KEY=secretref:embedding-api-key
EMBEDDING_BASE_URL=https://api.openai.com/v1
STORAGE_DIR=/data/storage
LLAMA_CLOUD_API_KEY=secretref:llama-cloud-api-key
SERVICE_API_KEY=secretref:service-api-key
EOF
}

# Newline-separated NAME=VALUE list for --secrets. The admin login secrets are
# omitted when unset: Azure rejects an empty secret value, and entra mode has no
# password to store.
app_secrets() {
  printf '%s\n' \
    "postgres-password=$POSTGRES_PASSWORD" \
    "llm-api-key=$LLM_API_KEY" \
    "embedding-api-key=$EMBEDDING_API_KEY" \
    "llama-cloud-api-key=$LLAMA_CLOUD_API_KEY" \
    "service-api-key=$SERVICE_API_KEY" \
    "acr-password=$ACR_PASS"
  [[ -n "${ADMIN_PASSWORD:-}" ]] && printf '%s\n' "admin-password=$ADMIN_PASSWORD"
  [[ -n "${SESSION_SECRET:-}" ]] && printf '%s\n' "session-secret=$SESSION_SECRET"
  return 0
}

apply_secrets() {
  local name="$1"
  local args=()
  local line
  while IFS= read -r line; do
    [[ -n "$line" ]] && args+=("$line")
  done < <(app_secrets)
  az containerapp secret set \
    --name "$name" \
    --resource-group "$RESOURCE_GROUP" \
    --secrets "${args[@]}" \
    --output none
}
