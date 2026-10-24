#!/usr/bin/env bash
# Build and push the image to ECR, and optionally update an existing Lambda.
#
#   AWS_REGION=us-east-1 ./scripts/push_to_ecr.sh
#   TAG=v4 FUNCTION_NAME=docling-md-converter ./scripts/push_to_ecr.sh   # also redeploys
#
# PLATFORM must match the Lambda function's architecture
# (linux/arm64 -> arm64, linux/amd64 -> x86_64).
set -euo pipefail

AWS_REGION=${AWS_REGION:-us-east-1}
REPO=${REPO:-docling-conversion-image}   # the repo the live Lambda runs from
TAG=${TAG:-$(date +%Y%m%d-%H%M%S)}
PLATFORM=${PLATFORM:-linux/arm64}

cd "$(dirname "$0")/.."
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGISTRY="$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"
IMAGE="$REGISTRY/$REPO:$TAG"

aws ecr describe-repositories --region "$AWS_REGION" --repository-names "$REPO" >/dev/null 2>&1 \
  || aws ecr create-repository --region "$AWS_REGION" --repository-name "$REPO" \
       --image-scanning-configuration scanOnPush=true >/dev/null

aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY"

# --provenance=false: Lambda rejects multi-manifest images that buildx
# produces by default when attestations are attached.
docker buildx build --platform "$PLATFORM" --provenance=false -t "$IMAGE" --push .
echo "Pushed $IMAGE"

if [[ -n "${FUNCTION_NAME:-}" ]]; then
  aws lambda update-function-code --region "$AWS_REGION" \
    --function-name "$FUNCTION_NAME" --image-uri "$IMAGE" >/dev/null
  echo "Updated Lambda $FUNCTION_NAME -> $IMAGE"
fi
