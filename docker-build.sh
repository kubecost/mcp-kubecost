#!/usr/bin/env bash
set -euo pipefail

# Login to AWS ECR
echo "Logging into AWS ECR..."
AWS_PROFILE=default aws ecr-public get-login-password --region us-east-1 | docker login --username AWS --password-stdin public.ecr.aws/kubecost
# Increment version
just _increment_version

# Update version references
just _update_version_refs

# Get version
VERSION=$(<version.txt)
BASE_IMAGE=$(<version-base-image.txt)
MCP_APP_SERVER="public.ecr.aws/kubecost/checker:$VERSION"
# Build and push app image (FROM pre-built base — no apt-get)
echo "Building and pushing version $VERSION..."
docker buildx build \
    --platform linux/amd64,linux/arm64 \
    --build-arg VERSION=$VERSION \
    --build-arg BASE_IMAGE=$BASE_IMAGE \
    -t $MCP_APP_SERVER \
    -f Dockerfile \
    --push .

echo ""
echo "✅ Build successful! Version $VERSION pushed to ECR"
echo "$MCP_APP_SERVER"
