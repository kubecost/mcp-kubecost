# syntax=docker/dockerfile:1
# ==============================================================================
# Stage 1: Builder
# Install Python, uv, and project dependencies. Builder is discarded; only
# /app (venv + application) is copied into the runtime image.
# Python is installed at /usr/bin/python3.12 so the venv shebang matches the
# same path provided by the pkg stage in the final image.
# ==============================================================================
FROM registry.access.redhat.com/ubi9-minimal:latest AS builder

RUN microdnf install -y python3.12 python3.12-pip && \
    microdnf clean all

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
# Stage 2: Python runtime rootfs
# Start from ubi-micro (no package manager) and add only Python 3.12, CA
# certs, and tzdata via dnf --installroot. This keeps the runtime UBI-based
# while excluding rpm/microdnf/libarchive from the final image.
#
# tsflags=noscripts keeps rpm scriptlets out of the installroot, so
# ca-certificates never runs `update-ca-trust extract` and its extracted
# bundles -- %ghost files, generated rather than shipped -- are absent. That
# leaves /etc/pki/tls/cert.pem dangling and OpenSSL with an empty default
# trust store, which broke OIDC discovery once FastMCP 4 switched to httpx2:
# httpx2 verifies against the system store via truststore, where httpx 0.28
# used certifi's bundled PEM. Copy this stage's own extracted tree in -- the
# same content the scriptlet would have produced. Deliberately not
# SSL_CERT_FILE, which would override a CA mounted into
# /etc/pki/ca-trust/source/anchors.
# ==============================================================================
FROM registry.access.redhat.com/ubi9/ubi-micro:latest AS micro

FROM registry.access.redhat.com/ubi9:latest AS pkg
COPY --from=micro / /mnt/rootfs
RUN dnf install --installroot=/mnt/rootfs --releasever=9 \
    --setopt=install_weak_deps=false --setopt=tsflags=noscripts --nodocs -y \
    python3.12 python3.12-libs ca-certificates tzdata \
    libstdc++ && \
    dnf reinstall --installroot=/mnt/rootfs --releasever=9 --setopt=tsflags=noscripts --nodocs -y tzdata && \
    dnf update --installroot=/mnt/rootfs -y && \
    dnf --installroot=/mnt/rootfs clean all && \
    cp -a /etc/pki/ca-trust/extracted/. /mnt/rootfs/etc/pki/ca-trust/extracted/ && \
    rm -rf /mnt/rootfs/var/cache/* /mnt/rootfs/var/lib/dnf /mnt/rootfs/var/log/* /mnt/rootfs/tmp/* && \
    printf 'nonroot:x:65532:65532:nonroot:/app:/sbin/nologin\n' >> /mnt/rootfs/etc/passwd && \
    printf 'nonroot:x:65532:\n' >> /mnt/rootfs/etc/group && \
    mkdir -p /mnt/rootfs/app /mnt/rootfs/licenses /mnt/rootfs/var/lib/mcp-kubecost && \
    chown 65532:0 /mnt/rootfs/var/lib/mcp-kubecost && \
    chmod 0755 /mnt/rootfs/app && \
    chmod g=u /mnt/rootfs/var/lib/mcp-kubecost

# ==============================================================================
# Stage 3: Final image
# /mnt/rootfs is already a complete ubi-micro rootfs plus Python, so FROM
# scratch avoids shipping that base twice. Contents remain UBI 9 (required for
# the Red Hat OpenShift Operator); only the declared parent changes.
# ==============================================================================
# FROM scratch
## commenting out the distroless step above, there is concern that the use of scratch will void our certification

FROM registry.access.redhat.com/ubi9/ubi-micro:latest
COPY --from=pkg /mnt/rootfs /

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
