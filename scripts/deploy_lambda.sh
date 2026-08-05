#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
FUNCTION_NAME="${LAMBDA_FUNCTION_NAME:-$(terraform -chdir="${ROOT_DIR}/terraform" output -raw lambda_function_name)}"
ECR_REPOSITORY_URL="${ECR_REPOSITORY_URL:-$(terraform -chdir="${ROOT_DIR}/terraform" output -raw ecr_repository_url)}"
AWS_REGION="${AWS_REGION:-us-east-1}"
IMAGE_TAG="${IMAGE_TAG:-$(git -C "${ROOT_DIR}" rev-parse --short HEAD 2>/dev/null || date -u +%Y%m%d%H%M%S)}"

ECR_REGISTRY="${ECR_REPOSITORY_URL%%/*}"
aws ecr get-login-password --region "${AWS_REGION}" | docker login --username AWS --password-stdin "${ECR_REGISTRY}"
docker build --platform linux/amd64 --tag "${ECR_REPOSITORY_URL}:${IMAGE_TAG}" "${ROOT_DIR}/src/api"
docker push "${ECR_REPOSITORY_URL}:${IMAGE_TAG}"
aws lambda update-function-code --function-name "${FUNCTION_NAME}" --image-uri "${ECR_REPOSITORY_URL}:${IMAGE_TAG}" --publish
aws lambda wait function-updated --function-name "${FUNCTION_NAME}"
