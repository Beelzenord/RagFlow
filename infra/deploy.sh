#!/usr/bin/env bash
# Build linux/amd64 images in ACR and point the three Container Apps at them.
# Tag is the current git SHA so a rollback is another --image ...:<oldsha>.

source "$(cd "$(dirname "$0")" && pwd)/_common.sh"
load_azure_env
require_app_secrets
need_cmd git
acr_creds
# Before the builds, so a missing Easy Auth setup fails in seconds rather than
# after three container images.
require_easy_auth

SHA="$(git -C "$REPO_ROOT" rev-parse --short HEAD)"
echo "deploying $SHA to $RESOURCE_GROUP  (auth: $AUTH_MODE)"

build() {
  local image="$1"
  local dockerfile="$2"
  echo "→ acr build $image:$SHA  ($dockerfile, linux/amd64)"
  az acr build \
    --registry "$ACR_NAME" \
    --image "$image:$SHA" \
    --image "$image:latest" \
    --file "$dockerfile" \
    --platform linux/amd64 \
    "$REPO_ROOT"
}

build web services/web/Dockerfile
build ingestion services/ingestion/Dockerfile
build query services/query/Dockerfile

set_registry() {
  local name="$1"
  az containerapp registry set \
    --name "$name" \
    --resource-group "$RESOURCE_GROUP" \
    --server "$ACR_LOGIN_SERVER" \
    --username "$ACR_USER" \
    --password "$ACR_PASS" \
    --output none
}

update_python_app() {
  local name="$1"
  local image="$2"
  local port="$3"
  local min_r="$4"
  local max_r="$5"

  set_registry "$name"
  apply_secrets "$name"

  local ev=()
  while IFS= read -r line; do
    [[ -n "$line" ]] && ev+=("$line")
  done < <(python_env_vars)

  echo "→ update $name  $image:$SHA  port $port"
  az containerapp update \
    --name "$name" \
    --resource-group "$RESOURCE_GROUP" \
    --image "$ACR_LOGIN_SERVER/$image:$SHA" \
    --set-env-vars "${ev[@]}" \
    --min-replicas "$min_r" \
    --max-replicas "$max_r" \
    --output none

  az containerapp ingress update \
    --name "$name" \
    --resource-group "$RESOURCE_GROUP" \
    --target-port "$port" \
    --allow-insecure true \
    --output none
}

update_python_app "$INGEST_APP" ingestion 8001 1 1
update_python_app "$QUERY_APP" query 8002 0 2

echo "→ mount Azure Files on $INGEST_APP at /data/storage"
tmp="$(mktemp)"
az containerapp show \
  --name "$INGEST_APP" \
  --resource-group "$RESOURCE_GROUP" \
  --output json \
  | python3 "$SCRIPT_DIR/_mount_files.py" "$ACA_STORAGE_NAME" /data/storage \
  > "$tmp"
az containerapp update \
  --name "$INGEST_APP" \
  --resource-group "$RESOURCE_GROUP" \
  --yaml "$tmp" \
  --output none
rm -f "$tmp"

echo "→ update $WEB_APP"
set_registry "$WEB_APP"
apply_secrets "$WEB_APP"
web_env=(
  "INGESTION_URL=http://$INGEST_APP"
  "QUERY_URL=http://$QUERY_APP"
  "SERVICE_API_KEY=secretref:service-api-key"
  "WEB_HTTP_TIMEOUT=120"
  "AUTH_MODE=$AUTH_MODE"
  "SESSION_COOKIE_SECURE=1"
)
if [[ "$AUTH_MODE" == "password" ]]; then
  web_env+=(
    "ADMIN_USERNAME=${ADMIN_USERNAME:-admin}"
    "ADMIN_PASSWORD=secretref:admin-password"
    "SESSION_SECRET=secretref:session-secret"
  )
fi

az containerapp update \
  --name "$WEB_APP" \
  --resource-group "$RESOURCE_GROUP" \
  --image "$ACR_LOGIN_SERVER/web:$SHA" \
  --set-env-vars "${web_env[@]}" \
  --min-replicas 0 \
  --max-replicas 2 \
  --output none

if [[ "$AUTH_MODE" == "entra" ]]; then
  # A left-over password from an earlier deploy would put a second login behind
  # the Microsoft one. --set-env-vars merges, so it has to be removed by name.
  az containerapp update \
    --name "$WEB_APP" \
    --resource-group "$RESOURCE_GROUP" \
    --remove-env-vars ADMIN_PASSWORD ADMIN_USERNAME \
    --output none 2>/dev/null || true
fi
az containerapp ingress update \
  --name "$WEB_APP" \
  --resource-group "$RESOURCE_GROUP" \
  --target-port 8080 \
  --type external \
  --allow-insecure false \
  --output none || az containerapp ingress update \
    --name "$WEB_APP" \
    --resource-group "$RESOURCE_GROUP" \
    --target-port 8080 \
    --output none

FQDN="$(az containerapp show --name "$WEB_APP" --resource-group "$RESOURCE_GROUP" --query properties.configuration.ingress.fqdn -o tsv)"
echo
echo "Deployed $SHA"
echo "UI: https://$FQDN"
echo
if [[ "$AUTH_MODE" == "entra" ]]; then
  echo "Sign in with your work account. Access is whoever is assigned to the"
  echo "enterprise application in Entra; the topbar shows who you are."
else
  echo "Sign in as ${ADMIN_USERNAME:-admin} with the ADMIN_PASSWORD from $ENV_FILE."
fi
echo
echo "First boot: the Documents list is empty. That is the new cloud database,"
echo "not a broken UI. Upload a small PDF, wait for completed/degraded, then ask."
