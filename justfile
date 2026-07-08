# Task Runner
# https://github.com/casey/just

default:
    @just --list
# Cross-platform sed (works on both Linux and macOS)
_sed pattern file:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ "$OSTYPE" == "darwin"* ]]; then
        sed -i '' "{{pattern}}" "{{file}}"
    else
        sed -i "{{pattern}}" "{{file}}"
    fi
_update_version_refs:
    #!/usr/bin/env bash
    set -euo pipefail
    VERSION=$(uv version --project orchestrator --short)
    echo "Updating version references to $VERSION..."

    # Update deployment manifest image tag
    just _sed "s|image: 297945954695.dkr.ecr.us-east-1.amazonaws.com/mcp-kubecost:.*|image: 297945954695.dkr.ecr.us-east-1.amazonaws.com/mcp-kubecost:$VERSION|" k8s/deployment.yaml
    echo "Updated k8s/deployment.yaml image tag to $VERSION"

docker-build-run:
    #!/usr/bin/env bash
    set -euo pipefail
    docker buildx build --load --progress plain \
        -t mcp-kubecost \
        -f ./Dockerfile .
    echo ""
    echo -e "\033[33m  Image built. Wait a few seconds for the server to start.\033[0m"
    echo -e "\033[33m  Then open a new terminal and run your tests. Example command:\033[0m"
    echo -e "\033[33m  fastmcp call mcp-http.json get_container_savings_recommendations --input-json '{\"window\": \"15d\"}'\033[0m"
    docker run --rm \
      --name mcp-kubecost \
      -p 3030:3030 \
      mcp-kubecost

docker-build-push:
    #!/usr/bin/env bash
    set -euo pipefail
    # just test-basic
    uv version --bump patch
    VERSION=$(uv version --project orchestrator --short)
    REGION=us-east-1
    BASE_REGISTRY_PATH=297945954695.dkr.ecr.$REGION.amazonaws.com
    CONTAINER_IMAGE=$BASE_REGISTRY_PATH/mcp-kubecost:$VERSION
    echo "Building and pushing base image..."
    docker buildx build \
      --platform linux/amd64,linux/arm64 \
      -t $CONTAINER_IMAGE \
      -f Dockerfile \
      --push .
    just _update_version_refs
    echo ""
    echo "✅ Image pushed to $CONTAINER_IMAGE"

test:
    uv run pytest

test-all:
    uv run pytest -m ""
