# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:trixie-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    UV_PYTHON_CACHE_DIR=/root/.cache/uv/python \
    UV_PYTHON_INSTALL_DIR=/python \
    UV_PYTHON_PREFERENCE=only-managed

WORKDIR /app

# Install locked third-party dependencies in a separately cached layer.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-editable

COPY src/ /app/src/
COPY fastmcp-http.json /app/fastmcp-http.json

# Install the application as a non-editable production package.
RUN --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=README.md,target=README.md \
    uv sync --locked --no-editable

# ── Runtime stage ────────────────────────────────────────────────────────────
# distroless/base has libc + libssl but no Python, no shell, no pebble.
# We bring our own Python (uv-managed) and venv from the builder.
FROM gcr.io/distroless/cc-debian12:nonroot

# Copy the uv-managed Python runtime (venv symlinks point into here)
COPY --from=builder /python /python
COPY --from=builder /app/ /app/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    FASTMCP_SHOW_SERVER_BANNER=false \
    FASTMCP_TELEMETRY_MODE=off \
    OTEL_SERVICE_NAME=mcp-kubecost

WORKDIR /app

EXPOSE 3030

CMD ["/app/.venv/bin/mcp-kubecost-http"]
