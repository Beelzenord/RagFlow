#!/usr/bin/env bash
# Create the Azure resources once. Safe to re-run: existing resources are reused.
# Does not build or push application images — that is deploy.sh.
#
#   az login
#   cp infra/azure.env.example infra/azure.env   # then fill secrets
#   ./infra/up.sh

source "$(cd "$(dirname "$0")" && pwd)/_common.sh"
load_azure_env
require_app_secrets

echo "subscription : $(az account show --query name -o tsv)"
echo "group        : $RESOURCE_GROUP  ($LOCATION)"
echo "acr          : $ACR_NAME"
echo "postgres     : $POSTGRES_SERVER"
echo "storage      : $STORAGE_ACCOUNT"
echo "apps         : $WEB_APP  $INGEST_APP  $QUERY_APP"

echo "→ providers"
for ns in Microsoft.App Microsoft.ContainerRegistry Microsoft.DBforPostgreSQL Microsoft.Storage Microsoft.OperationalInsights; do
  az provider register --namespace "$ns" --wait >/dev/null
done

echo "→ resource group"
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none

echo "→ container registry"
if ! az acr show --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" >/dev/null 2>&1; then
  az acr create \
    --name "$ACR_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --sku Basic \
    --admin-enabled true \
    --output none
fi
acr_creds

echo "→ storage account + file share"
if ! az storage account show --name "$STORAGE_ACCOUNT" --resource-group "$RESOURCE_GROUP" >/dev/null 2>&1; then
  az storage account create \
    --name "$STORAGE_ACCOUNT" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --sku Standard_LRS \
    --kind StorageV2 \
    --https-only true \
    --output none
fi
STORAGE_KEY="$(az storage account keys list --account-name "$STORAGE_ACCOUNT" --resource-group "$RESOURCE_GROUP" --query '[0].value' -o tsv)"
az storage share-rm create \
  --resource-group "$RESOURCE_GROUP" \
  --storage-account "$STORAGE_ACCOUNT" \
  --name "$FILE_SHARE" \
  --quota 10 \
  --output none >/dev/null

echo "→ postgres flexible server"
if ! az postgres flexible-server show --name "$POSTGRES_SERVER" --resource-group "$RESOURCE_GROUP" >/dev/null 2>&1; then
  if ! az postgres flexible-server create \
    --name "$POSTGRES_SERVER" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --admin-user "$POSTGRES_USER" \
    --admin-password "$POSTGRES_PASSWORD" \
    --sku-name Standard_B1ms \
    --tier Burstable \
    --storage-size 32 \
    --version 16 \
    --public-access 0.0.0.0 \
    --yes \
    --output none; then
    echo "Sweden Central / B1ms create failed — retry with LOCATION=westeurope or a larger SKU" >&2
    exit 1
  fi
fi

az postgres flexible-server db create \
  --resource-group "$RESOURCE_GROUP" \
  --server-name "$POSTGRES_SERVER" \
  --database-name "$POSTGRES_DB" \
  --output none >/dev/null || true

# 0.0.0.0–0.0.0.0 is Azure's "allow Azure services" rule (Container Apps egress).
az postgres flexible-server firewall-rule create \
  --resource-group "$RESOURCE_GROUP" \
  --name "$POSTGRES_SERVER" \
  --rule-name AllowAzureServices \
  --start-ip-address 0.0.0.0 \
  --end-ip-address 0.0.0.0 \
  --output none >/dev/null || true

LAPTOP_IP="$(curl -fsS https://api.ipify.org || true)"
if [[ -n "${LAPTOP_IP:-}" ]]; then
  az postgres flexible-server firewall-rule create \
    --resource-group "$RESOURCE_GROUP" \
    --name "$POSTGRES_SERVER" \
    --rule-name AllowLaptop \
    --start-ip-address "$LAPTOP_IP" \
    --end-ip-address "$LAPTOP_IP" \
    --output none >/dev/null || true
  echo "  laptop firewall : $LAPTOP_IP"
fi

echo "→ allow VECTOR + PGCRYPTO"
az postgres flexible-server parameter set \
  --resource-group "$RESOURCE_GROUP" \
  --server-name "$POSTGRES_SERVER" \
  --name azure.extensions \
  --value VECTOR,PGCRYPTO \
  --output none

echo "→ container apps environment"
if ! az containerapp env show --name "$ACA_ENV" --resource-group "$RESOURCE_GROUP" >/dev/null 2>&1; then
  if ! az containerapp env create \
    --name "$ACA_ENV" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --logs-destination none \
    --output none; then
    echo "  retrying env create with a Log Analytics workspace"
    LAW="${PREFIX}-logs"
    az monitor log-analytics workspace create \
      --resource-group "$RESOURCE_GROUP" \
      --workspace-name "$LAW" \
      --location "$LOCATION" \
      --output none
    LAW_ID="$(az monitor log-analytics workspace show -g "$RESOURCE_GROUP" -n "$LAW" --query customerId -o tsv)"
    LAW_KEY="$(az monitor log-analytics workspace get-shared-keys -g "$RESOURCE_GROUP" -n "$LAW" --query primarySharedKey -o tsv)"
    az containerapp env create \
      --name "$ACA_ENV" \
      --resource-group "$RESOURCE_GROUP" \
      --location "$LOCATION" \
      --logs-destination log-analytics \
      --logs-workspace-id "$LAW_ID" \
      --logs-workspace-key "$LAW_KEY" \
      --output none
  fi
fi

echo "→ register Azure Files on the environment"
az containerapp env storage set \
  --name "$ACA_ENV" \
  --resource-group "$RESOURCE_GROUP" \
  --storage-name "$ACA_STORAGE_NAME" \
  --access-mode ReadWrite \
  --azure-file-account-name "$STORAGE_ACCOUNT" \
  --azure-file-account-key "$STORAGE_KEY" \
  --azure-file-share-name "$FILE_SHARE" \
  --output none

create_or_update_app() {
  local name="$1"
  local ingress="$2"
  local target_port="$3"
  local min_replicas="$4"
  shift 4
  local extra=("$@")

  if az containerapp show --name "$name" --resource-group "$RESOURCE_GROUP" >/dev/null 2>&1; then
    echo "  reuse $name"
    apply_secrets "$name"
    return
  fi

  echo "  create $name (placeholder image; deploy.sh replaces it)"
  az containerapp create \
    --name "$name" \
    --resource-group "$RESOURCE_GROUP" \
    --environment "$ACA_ENV" \
    --image mcr.microsoft.com/k8se/quickstart:latest \
    --ingress "$ingress" \
    --target-port "$target_port" \
    --min-replicas "$min_replicas" \
    --max-replicas 2 \
    --secrets \
      "postgres-password=$POSTGRES_PASSWORD" \
      "llm-api-key=$LLM_API_KEY" \
      "embedding-api-key=$EMBEDDING_API_KEY" \
      "llama-cloud-api-key=$LLAMA_CLOUD_API_KEY" \
      "service-api-key=$SERVICE_API_KEY" \
      "admin-password=$ADMIN_PASSWORD" \
      "session-secret=$SESSION_SECRET" \
      "acr-password=$ACR_PASS" \
    --registry-server "$ACR_LOGIN_SERVER" \
    --registry-username "$ACR_USER" \
    --registry-password "$ACR_PASS" \
    "${extra[@]}" \
    --output none
}

# Placeholder listens on 80. deploy.sh switches target ports to 8080/8001/8002
# and points INGESTION_URL / QUERY_URL at http://<app-name> (ACA ingress, not the container port).
create_or_update_app "$WEB_APP" external 80 0
create_or_update_app "$INGEST_APP" internal 80 1
create_or_update_app "$QUERY_APP" internal 80 0

echo "→ allow HTTP between apps (so INGESTION_URL=http://$INGEST_APP works)"
for app in "$WEB_APP" "$INGEST_APP" "$QUERY_APP"; do
  az containerapp ingress update \
    --name "$app" \
    --resource-group "$RESOURCE_GROUP" \
    --allow-insecure true \
    --output none || true
done

echo
echo "Resources are up. Next:"
echo "  ./infra/migrate.sh    # CREATE EXTENSION + schema on a fresh database"
echo "  ./infra/deploy.sh     # build linux/amd64 images and point the apps at them"
echo
echo "Postgres host : $POSTGRES_FQDN"
echo "Database      : $POSTGRES_DB"
