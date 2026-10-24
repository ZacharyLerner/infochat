#!/usr/bin/env bash
# End-to-end local test: uploads a file to LocalStack S3, which notifies SQS,
# which the poller hands to the Lambda container. Waits for the result message
# and prints the S3 location plus a summary of the output.
#
#   docker compose up -d --build
#   ./scripts/local_test.sh path/to/file.pdf
set -euo pipefail

FILE=${1:?usage: $0 <file>}
TIMEOUT=${TIMEOUT:-900}
NAME=$(basename "$FILE")
KEY="uploads/$(date +%s)-$NAME"
SOURCE="s3://docling-input/$KEY"
RESULTS_URL=http://localhost:4566/000000000000/docling-results

cd "$(dirname "$0")/.."
aws() { docker compose exec -T localstack awslocal "$@"; }

docker compose cp "$FILE" "localstack:/tmp/$NAME" >/dev/null 2>&1
aws s3 cp "/tmp/$NAME" "$SOURCE" >/dev/null
echo "Uploaded $SOURCE — waiting for conversion (first run includes model load)..."

start=$(date +%s)
while (( $(date +%s) - start < TIMEOUT )); do
  msgs=$(aws sqs receive-message --queue-url "$RESULTS_URL" \
           --max-number-of-messages 10 --wait-time-seconds 20 --output json || true)
  [[ -z "$msgs" ]] && continue
  # Prints "<receipt-handle>\t<output-uri>\t<body>" for the message matching our upload
  match=$(SOURCE="$SOURCE" python3 -c '
import json, os, sys
for m in json.load(sys.stdin).get("Messages", []):
    body = json.loads(m["Body"])
    if body.get("source") == os.environ["SOURCE"]:
        print(m["ReceiptHandle"], body["output"], json.dumps(body), sep="\t")
        break
' <<<"$msgs")
  if [[ -n "$match" ]]; then
    IFS=$'\t' read -r handle output body <<<"$match"
    aws sqs delete-message --queue-url "$RESULTS_URL" --receipt-handle "$handle"
    echo "Result: $body"
    echo "Output written to: $output"
    # DoclingDocument JSON is one long line: print a summary instead of the raw file
    aws s3 cp "$output" - | python3 -c '
import json, sys
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except ValueError:
    print(raw[:2000]); sys.exit()
heads = [t["text"] for t in d.get("texts", []) if t.get("label") in ("title", "section_header")]
print("schema", d.get("version"), "| name", d.get("name"), "| origin.uri", (d.get("origin") or {}).get("uri"))
print(len(d.get("texts", [])), "texts,", len(d.get("tables", [])), "tables,", len(raw), "bytes")
print("headings:", heads[:8])
'

    exit 0
  fi
done

echo "Timed out after ${TIMEOUT}s. Check: docker compose logs docling-lambda sqs-poller" >&2
exit 1
