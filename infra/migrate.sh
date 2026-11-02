#!/usr/bin/env bash
# Apply 01-extensions.sql, 02-schema.sql, 05-markets.sql and 06-answer-log.sql to a Flexible Server
# database. 03 and 04 are upgrades for an old local volume — 02 already has those
# objects. Every file is idempotent, so re-running this upgrades an existing
# database too.

source "$(cd "$(dirname "$0")" && pwd)/_common.sh"
load_azure_env
need POSTGRES_PASSWORD

az extension add --name rdbms-connect --upgrade >/dev/null 2>&1 || az extension add --name rdbms-connect >/dev/null

run_sql() {
  local file="$1"
  echo "→ $(basename "$file")"
  if az postgres flexible-server execute \
    --name "$POSTGRES_SERVER" \
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
run_sql "$REPO_ROOT/db/migrations/05-markets.sql"
run_sql "$REPO_ROOT/db/migrations/06-answer-log.sql"

echo "→ verify extensions, columns and tables"

# One query per call: az returns the rows of the last statement only.
verify() {
  if az postgres flexible-server execute \
    --name "$POSTGRES_SERVER" \
    --admin-user "$POSTGRES_USER" \
    --admin-password "$POSTGRES_PASSWORD" \
    --database-name "$POSTGRES_DB" \
    --querytext "$1"; then
    return 0
  fi
  if command -v psql >/dev/null 2>&1; then
    PGPASSWORD="$POSTGRES_PASSWORD" psql \
      "host=$POSTGRES_FQDN port=5432 dbname=$POSTGRES_DB user=$POSTGRES_USER sslmode=require" \
      -c "$1"
  fi
}

verify 'SELECT extname FROM pg_extension ORDER BY 1;'
verify 'SELECT column_name, data_type
  FROM information_schema.columns
 WHERE table_name = '\''document_chunks'\''
   AND column_name IN ('\''embedding'\'','\''content_tsv'\'')
 ORDER BY 1;'
verify 'SELECT tablename
  FROM pg_tables
 WHERE tablename IN ('\''markets'\'','\''market_groups'\'','\''market_group_members'\'','\''document_audience'\'','\''answer_log'\'')
 ORDER BY 1;'

echo
echo "Schema is on $POSTGRES_FQDN/$POSTGRES_DB. Next: ./infra/deploy.sh"
