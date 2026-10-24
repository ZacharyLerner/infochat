#!/usr/bin/env bash
# Invoke the Lambda container directly with an SQS-shaped event file,
# bypassing SQS. The referenced S3 object must already exist in LocalStack.
#
#   ./scripts/invoke_direct.sh events/sqs_direct.json
set -euo pipefail
EVENT=${1:-events/sqs_direct.json}
curl -s -X POST "http://localhost:9000/2015-03-31/functions/function/invocations" \
  -H "Content-Type: application/json" --data-binary "@$EVENT"
echo
