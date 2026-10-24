#!/usr/bin/env bash
# Read-only health check of the deployed pipeline: Lambda state, queue and DLQ depth,
# errors in the last 24 h, and the newest outputs. Changes nothing.
#
#   ./scripts/status.sh
#   PREFIX=<workspace-slug>/ ./scripts/status.sh    # outputs for one workspace
set -euo pipefail
export AWS_PAGER=""

AWS_REGION=${AWS_REGION:-us-east-1}
FUNCTION_NAME=${FUNCTION_NAME:-docling-md-converter}
QUEUE=${QUEUE:-docling-md-queue}
OUTPUT_BUCKET=${OUTPUT_BUCKET:-docling-md-workspace-bucket}
PREFIX=${PREFIX:-}

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
queue_url() { echo "https://sqs.$AWS_REGION.amazonaws.com/$ACCOUNT_ID/$1"; }
depth() {
  aws sqs get-queue-attributes --region "$AWS_REGION" --queue-url "$(queue_url "$1")" \
    --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible \
    --query 'Attributes.[ApproximateNumberOfMessages,ApproximateNumberOfMessagesNotVisible]' --output text \
    | awk '{print "waiting " $1 ", in flight " $2}'
}

echo "== Lambda $FUNCTION_NAME"
aws lambda get-function --region "$AWS_REGION" --function-name "$FUNCTION_NAME" \
  --query '[Configuration.State, Configuration.LastModified, Code.ImageUri]' --output text
echo "   (Inactive = idle; it wakes on the next message)"

echo "== Queues"
echo "jobs: $(depth "$QUEUE")"
echo "DLQ:  $(depth "$QUEUE-dlq")"

echo "== Errors, last 24 h"
since=$(( ($(date +%s) - 86400) * 1000 ))
aws logs filter-log-events --region "$AWS_REGION" --log-group-name "/aws/lambda/$FUNCTION_NAME" \
  --start-time "$since" --filter-pattern '?ERROR ?"Task timed out"' \
  --query 'events[].message' --output text | tr '\t' '\n' | grep -v '^$' | tail -10 || true

echo "== Newest outputs in s3://$OUTPUT_BUCKET/$PREFIX"
aws s3 ls "s3://$OUTPUT_BUCKET/$PREFIX" --recursive --region "$AWS_REGION" | sort | tail -10
