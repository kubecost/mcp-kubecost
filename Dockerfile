# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.12.0-trixie-slim@sha256:b3781c0d61af34f63032d5221a6bf2e46b2a16225a531d2dea0836f09861c190 AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    UV_PYTHON_CACHE_DIR=/root/.cache/uv/python \
    UV_PYTHON_INSTALL_DIR=/python \
    UV_PYTHON_PREFERENCE=only-managed

# Pin the Python patch release for reproducible builds.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv python install 3.12.13

WORKDIR /app

# Install locked third-party dependencies in a separately cached layer.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-editable

# Keep the configuration in the project root so its relative source path works
# consistently both locally and inside the image.
COPY src/ /app/src/
COPY fastmcp-http.json /app/fastmcp-http.json

# Install the application as a non-editable production package.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=README.md,target=README.md \
    uv sync --locked --no-editable


FROM debian:trixie-slim@sha256:020c0d20b9880058cbe785a9db107156c3c75c2ac944a6aa7ab59f2add76a7bd AS runtime

# Use a fixed non-root identity. Avoid --system because UID 10001 is outside
# Debian's system-user range.
RUN groupadd --gid 10001 nonroot \
 && useradd --uid 10001 \
            --gid nonroot \
            --create-home \
            --no-log-init \
            --shell /usr/sbin/nologin \
            nonroot

# Keep application code and interpreters root-owned and immutable to the
# runtime process.
COPY --from=builder /python /python
COPY --from=builder /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    HOME=/home/nonroot \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FASTMCP_CHECK_FOR_UPDATES=off

WORKDIR /app

USER 10001:10001

EXPOSE 3030/tcp

CMD ["fastmcp", "run", "fastmcp-http.json", "--skip-env", "--no-banner"]
