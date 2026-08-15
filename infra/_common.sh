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
  # The web app has a public FQDN; refuse to ship it without a login gate.
  need ADMIN_PASSWORD
  need SESSION_SECRET
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

apply_secrets() {
  local name="$1"
  az containerapp secret set \
    --name "$name" \
    --resource-group "$RESOURCE_GROUP" \
    --secrets \
      "postgres-password=$POSTGRES_PASSWORD" \
      "llm-api-key=$LLM_API_KEY" \
      "embedding-api-key=$EMBEDDING_API_KEY" \
      "llama-cloud-api-key=$LLAMA_CLOUD_API_KEY" \
      "service-api-key=$SERVICE_API_KEY" \
      "admin-password=$ADMIN_PASSWORD" \
      "session-secret=$SESSION_SECRET" \
      "acr-password=$ACR_PASS" \
    --output none
}
