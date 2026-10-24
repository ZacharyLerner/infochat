#!/usr/bin/env bash
# Check that the pipeline's output works in llm-backend: converts FILE through the
# local pipeline (local_test.sh), embeds the .docling.json into a backend workspace,
# asks QUESTION, prints the answer's cited sources, then removes the document again.
#
#   docker compose up -d --build        # this folder (LocalStack + Lambda)
#   WORKSPACE=<slug> ./scripts/check_backend.sh samples/sample.pdf "What is Docling?"
#
# BACKEND defaults to the local test stack (http://localhost:4301). Use a test
# workspace: the document is embedded for real while the check runs.
set -euo pipefail

FILE=${1:?usage: $0 <file> <question>}
QUESTION=${2:?usage: $0 <file> <question>}
BACKEND=${BACKEND:-http://localhost:4301}
WORKSPACE=${WORKSPACE:?set WORKSPACE to a backend test workspace slug}
# The backend ignores the header when it has no ADMIN_API_KEY configured.
KEY_HEADER="X-API-Key: ${ADMIN_API_KEY:-}"

cd "$(dirname "$0")/.."
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

output=$(./scripts/local_test.sh "$FILE" | tee /dev/stderr | sed -n 's/^Output written to: //p')
[[ -n "$output" ]] || { echo "conversion failed" >&2; exit 1; }
name=$(basename "$output")
docker compose exec -T localstack awslocal s3 cp "$output" - > "$TMP/$name"

echo "== embedding $name into $WORKSPACE"
curl -sf -H "$KEY_HEADER" -F "file=@$TMP/$name;filename=$name" \
  "$BACKEND/api/workspace/$WORKSPACE/embed" > "$TMP/embed.json"
cat "$TMP/embed.json"; echo
doc_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["doc_id"])' "$TMP/embed.json")
trap 'curl -s -o /dev/null -X DELETE -H "$KEY_HEADER" "$BACKEND/api/workspace/$WORKSPACE/embed/$doc_id"; rm -rf "$TMP"' EXIT

echo "== asking: $QUESTION"
curl -sf -H "$KEY_HEADER" -H "Content-Type: application/json" \
  -d "$(python3 -c 'import json,sys; print(json.dumps({"question": sys.argv[1]}))' "$QUESTION")" \
  "$BACKEND/api/workspace/$WORKSPACE/query" | python3 -c '
import json, sys
r = json.load(sys.stdin)
print("answer:", (r.get("answer") or r.get("response") or "")[:500])
for d in (r.get("sources") or {}).get("documents", []):
    print("  [%s] cited=%s title=%r uri=%r headings=%s" % (d.get("n"), d.get("cited"), d.get("title"), d.get("uri"), d.get("headings")))
'
echo "== removing $doc_id"
