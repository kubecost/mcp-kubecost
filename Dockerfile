# syntax=docker/dockerfile:1
# ==============================================================================
# Stage 1: Builder
# Install Python, uv, and project dependencies. Builder is discarded; only
# /app (venv + application) is copied into the runtime image.
# Python is installed at /usr/bin/python3.12 so the venv shebang matches the
# same path provided by hi/python:3.12 in the final image.
# ==============================================================================
FROM registry.access.redhat.com/hi/python:3.12-builder AS builder

# The builder image runs as 65532; root is needed to write /app
USER 0
WORKDIR /app
RUN python3.12 -m pip install --no-cache-dir uv

# UV_PYTHON pins the interpreter the venv is built against: the venv shebang must
# be a path that also exists in the final image, and uv will otherwise resolve to
# a managed CPython download that the runtime stage does not carry.
ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_PYTHON=/usr/bin/python3.12 \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1

# Dependencies resolve from the lockfile alone, so this layer survives source edits.
COPY pyproject.toml uv.lock /app/
RUN python3.12 -m uv sync --locked --no-install-project --no-editable --extra otel

# Keep group read/execute for OpenShift's arbitrary UID in group 0, but do not
# let the runtime process modify application code or installed packages.
COPY README.md /app/
COPY src/ /app/src/
COPY config/fastmcp-http.json /app/config/fastmcp-http.json
RUN python3.12 -m uv sync --locked --no-editable --extra otel && \
    chmod -R g=u,go-w /app && \
    install -d -m 0770 /var/lib/mcp-kubecost && \
    chmod -R g=u /var/lib/mcp-kubecost

# ==============================================================================
# Stage 2: Final image
# Red Hat Hardened Image: distroless Python 3.12 (no shell, no package manager)
# with an extracted CA trust store; passes the Red Hat OpenShift Operator's
# BasedOnUbi preflight check.
# ==============================================================================
FROM registry.access.redhat.com/hi/python:3.12

# The app never runs pip, and the base's pip vendors packages with HIGH/CRITICAL
# CVEs (e.g. msgpack 1.1.2). Exec form because the image has no shell.
USER 0
RUN ["python3", "-m", "pip", "uninstall", "--yes", "pip"]

ARG version=dev
LABEL name="mcp-kubecost" \
    vendor="Kubecost by IBM" \
    summary="FinOps MCP server for Kubecost analytics." \
    description="Read-only Kubecost cost allocation and container rightsizing data for MCP clients." \
    maintainer="kubecost-image-support@wwpdl.vnet.ibm.com" \
    version="${version}" \
    release="${version}"

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Etc/UTC \
    FASTMCP_SHOW_SERVER_BANNER=false \
    FASTMCP_TELEMETRY_MODE=off \
    OTEL_SERVICE_NAME=mcp-kubecost

WORKDIR /app
COPY --from=builder --chown=0:0 /app /app
COPY --from=builder --chown=65532:0 /var/lib/mcp-kubecost/ /var/lib/mcp-kubecost/
COPY LICENSE /licenses/LICENSE

EXPOSE 3030
VOLUME ["/var/lib/mcp-kubecost"]
USER 65532
CMD ["/app/.venv/bin/mcp-kubecost-http"]
