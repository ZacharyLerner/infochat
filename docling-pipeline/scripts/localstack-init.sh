#!/bin/bash
# Runs inside LocalStack once it's ready: creates buckets, queues and the
# S3 -> SQS notification that mirrors the production wiring. Only uploads/ (what
# local_test.sh writes) notifies, so other writes (e.g. uploader state files) don't.
set -euo pipefail

awslocal s3 mb s3://docling-input
awslocal s3 mb s3://docling-output
awslocal sqs create-queue --queue-name docling-jobs --attributes VisibilityTimeout=900
awslocal sqs create-queue --queue-name docling-results

awslocal s3api put-bucket-notification-configuration \
  --bucket docling-input \
  --notification-configuration '{
    "QueueConfigurations": [{
      "QueueArn": "arn:aws:sqs:us-east-1:000000000000:docling-jobs",
      "Events": ["s3:ObjectCreated:*"],
      "Filter": {"Key": {"FilterRules": [{"Name": "prefix", "Value": "uploads/"}]}}
    }]
  }'

echo "docling local resources ready"
