#!/usr/bin/env bash
# Apply the schema to a Flexible Server database, fresh or already deployed.
# 03 and 04 are upgrades for an old local volume — 02 already has those objects.
# 05 onwards are not in 02. Each is safe to re-run (IF NOT EXISTS, ON CONFLICT
# DO NOTHING), so this script both builds a fresh database and upgrades one that
# an earlier version of it set up.

source "$(cd "$(dirname "$0")" && pwd)/_common.sh"
load_azure_env
need POSTGRES_PASSWORD

az extension add --name rdbms-connect --upgrade >/dev/null 2>&1 || az extension add --name rdbms-connect >/dev/null

run_sql() {
  local file="$1"
  echo "→ $(basename "$file")"
  if az postgres flexible-server execute \
    --name "$POSTGRES_SERVER" \
    --resource-group "$RESOURCE_GROUP" \
    --admin-user "$POSTGRES_USER" \
    --admin-password "$POSTGRES_PASSWORD" \
    --database-name "$POSTGRES_DB" \
    --file-path "$file" \
    --output none; then
    return 0
  fi
  if command -v psql >/dev/null 2>&1; then
    echo "  falling back to psql (sslmode=require)"
    PGPASSWORD="$POSTGRES_PASSWORD" psql \
      "host=$POSTGRES_FQDN port=5432 dbname=$POSTGRES_DB user=$POSTGRES_USER sslmode=require" \
      -v ON_ERROR_STOP=1 \
      -f "$file"
    return
  fi
  die "could not apply $file — install the rdbms-connect extension or psql"
}

run_sql "$REPO_ROOT/db/migrations/01-extensions.sql"
run_sql "$REPO_ROOT/db/migrations/02-schema.sql"
# Document scoping: the region vocabulary and the scope columns /ingest writes.
# Without it the first upload fails - the INSERT names columns that do not exist.
run_sql "$REPO_ROOT/db/migrations/05-document-scoping.sql"
run_sql "$REPO_ROOT/db/migrations/06-document-fingerprint.sql"
run_sql "$REPO_ROOT/db/migrations/07-region-catalogue.sql"

echo "→ verify extensions + columns"
VERIFY='SELECT extname FROM pg_extension ORDER BY 1;
SELECT column_name, data_type
  FROM information_schema.columns
 WHERE table_name = '\''document_chunks'\''
   AND column_name IN ('\''embedding'\'','\''content_tsv'\'')
 ORDER BY 1;
SELECT column_name
  FROM information_schema.columns
 WHERE table_name = '\''documents'\''
   AND column_name IN ('\''applies_to_regions'\'','\''scope_entries'\'','\''content_sha256'\'')
 ORDER BY 1;
SELECT count(*) AS countries, count(*) FILTER (WHERE active) AS enabled FROM regions;'

if az postgres flexible-server execute \
  --name "$POSTGRES_SERVER" \
  --resource-group "$RESOURCE_GROUP" \
  --admin-user "$POSTGRES_USER" \
  --admin-password "$POSTGRES_PASSWORD" \
  --database-name "$POSTGRES_DB" \
  --querytext "$VERIFY"; then
  :
elif command -v psql >/dev/null 2>&1; then
  PGPASSWORD="$POSTGRES_PASSWORD" psql \
    "host=$POSTGRES_FQDN port=5432 dbname=$POSTGRES_DB user=$POSTGRES_USER sslmode=require" \
    -c "$VERIFY"
fi

echo
echo "Schema is on $POSTGRES_FQDN/$POSTGRES_DB. Next: ./infra/deploy.sh"
