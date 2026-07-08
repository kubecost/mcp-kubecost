# syntax=docker/dockerfile:1
# check=skip=InvalidDefaultArgInFrom

FROM python:3.12-slim

USER root

# one big layer that patches any fixable CVEs and adds a non-root user
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir --upgrade "pip>=26.1.2" \
    && groupadd --gid 1001 appuser \
    && useradd --uid 1001 --gid 1001 --create-home --shell /bin/bash appuser

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
COPY . /app
# Disable development dependencies
ENV UV_NO_DEV=1
ENV PATH="/app/.venv/bin:${PATH}"

RUN uv sync --locked \
    && chown -R appuser:appuser /app \
    && chown -R appuser:appuser /home/appuser

# AKP requires number based user, not "appuser"
USER 1001
EXPOSE 3030

CMD ["uv", "run", "fastmcp", "run", "fastmcp-docker.json"]
