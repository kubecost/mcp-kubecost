"""Operator-facing OpenTelemetry status. Does not import the OTEL SDK."""

from __future__ import annotations

import logging
import os
import sys
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

# Distro defaults when the corresponding OTEL_*_EXPORTER var is unset.
_DISTRO_EXPORTER_DEFAULT = "otlp"


def _telemetry_mode() -> str:
    return os.environ.get("FASTMCP_TELEMETRY_MODE", "off").strip().lower() or "off"


def _telemetry_requested() -> bool:
    return _telemetry_mode() not in {"off"}


def _auto_instrumentation_loaded() -> bool:
    """Return True when ``opentelemetry-instrument`` sitecustomize ran in this process."""
    sitecustomize = sys.modules.get("sitecustomize")
    filename = (getattr(sitecustomize, "__file__", None) or "").replace("\\", "/")
    if "opentelemetry" in filename:
        return True
    return any(name.startswith("opentelemetry.instrumentation.auto_instrumentation") for name in sys.modules)


def _exporter(var: str) -> str:
    return os.environ.get(var, "").strip() or _DISTRO_EXPORTER_DEFAULT


def _redact_endpoint(url: str) -> str:
    """Strip userinfo so an OTLP URL with embedded credentials is safe to log."""
    parts = urlsplit(url)
    if not parts.username and not parts.password:
        return url
    host = parts.hostname or ""
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _quiet_sdk_loggers() -> None:
    """Keep the OTEL SDK off the INFO stream unless this server is at DEBUG."""
    if logging.getLogger("mcp_kubecost").isEnabledFor(logging.DEBUG):
        return
    otel = logging.getLogger("opentelemetry")
    if otel.level == logging.NOTSET or otel.level < logging.WARNING:
        otel.setLevel(logging.WARNING)


def log_telemetry_status() -> None:
    """Log observed telemetry state after ``logging_fastmcp`` is configured.

    Reports whether auto-instrumentation actually loaded, not only whether
    ``FASTMCP_TELEMETRY_MODE`` is set. Never logs ``OTEL_*HEADERS`` or other
    credential-bearing variables.
    """
    _quiet_sdk_loggers()

    if not _telemetry_requested():
        logger.debug("OpenTelemetry wrapper is off (FASTMCP_TELEMETRY_MODE=%s)", _telemetry_mode())
        return

    mode = _telemetry_mode()
    if not _auto_instrumentation_loaded():
        logger.warning(
            "FASTMCP_TELEMETRY_MODE=%s but OpenTelemetry auto-instrumentation is not loaded; "
            "traces will not be exported. Use mcp-kubecost-http with the 'otel' extra installed, "
            "or invoke opentelemetry-instrument yourself.",
            mode,
        )
        return

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    service = os.environ.get("OTEL_SERVICE_NAME", "").strip() or "mcp-kubecost"
    traces = _exporter("OTEL_TRACES_EXPORTER")
    metrics = _exporter("OTEL_METRICS_EXPORTER")
    logs = _exporter("OTEL_LOGS_EXPORTER")

    if not endpoint:
        logger.warning(
            "OpenTelemetry auto-instrumentation is loaded but OTEL_EXPORTER_OTLP_ENDPOINT is unset; "
            "spans will not be exported."
        )
        return

    logger.info(
        "OpenTelemetry is active: service=%s endpoint=%s traces=%s metrics=%s logs=%s",
        service,
        _redact_endpoint(endpoint),
        traces,
        metrics,
        logs,
    )
