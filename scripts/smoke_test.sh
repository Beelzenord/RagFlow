#!/usr/bin/env bash
# Minimal end-to-end smoke test. Requires `curl` and `jq`.
# Usage: ./scripts/smoke_test.sh path/to/file.pdf "What is the document about?"
set -euo pipefail

FILE="${1:?usage: smoke_test.sh <file> <question>}"
QUESTION="${2:?usage: smoke_test.sh <file> <question>}"
INGEST_URL="${INGEST_URL:-http://localhost:8001}"
QUERY_URL="${QUERY_URL:-http://localhost:8002}"
API_KEY="${SERVICE_API_KEY:-}"

auth=()
if [[ -n "$API_KEY" ]]; then auth=(-H "x-api-key: $API_KEY"); fi

echo "→ Uploading $FILE"
DOC_ID=$(curl -fsS -X POST "$INGEST_URL/ingest" \
  "${auth[@]}" \
  -F "file=@${FILE}" \
  -F "collection=smoke-test" | jq -r .document_id)
echo "  document_id=$DOC_ID"

echo "→ Polling status (up to 5 min)"
for i in $(seq 1 60); do
  status=$(curl -fsS "${auth[@]}" "$INGEST_URL/documents/$DOC_ID" | jq -r .status)
  echo "  [$i] $status"
  if [[ "$status" == "completed" ]]; then break; fi
  if [[ "$status" == "failed" ]]; then
    curl -fsS "${auth[@]}" "$INGEST_URL/documents/$DOC_ID" | jq .
    exit 1
  fi
  sleep 5
done

echo "→ Asking: $QUESTION"
curl -fsS -X POST "$QUERY_URL/query" \
  "${auth[@]}" \
  -H "content-type: application/json" \
  -d "$(jq -n --arg q "$QUESTION" --arg d "$DOC_ID" '{question:$q, document_id:$d}')" | jq .
