"""Global logging configuration for the MCP server.

Import this module once in server.py to configure the root logger.
All other modules should use: logging.getLogger(__name__)
"""

from __future__ import annotations

import logging
import os

from mcp_kubecost.config.settings import is_http_mode

_HEALTH_PATH = "/health"


class HealthProbeLogFilter(logging.Filter):
    """Drop uvicorn access lines for Kubernetes GET /health probes."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not _is_health_access_record(record)


def _is_health_access_record(record: logging.LogRecord) -> bool:
    args = record.args
    if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
        return args[2].split("?", 1)[0] == _HEALTH_PATH
    try:
        message = record.getMessage()
    except (TypeError, ValueError):
        return False
    return '"GET /health HTTP/' in message or '"GET /health?' in message


def _install_health_probe_log_filter() -> None:
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, HealthProbeLogFilter) for f in access.filters):
        access.addFilter(HealthProbeLogFilter())


# Get log level from environment variable, default to INFO
log_level = os.getenv("FASTMCP_LOG_LEVEL", "INFO").upper()
level = getattr(logging, log_level, logging.INFO)
logger = logging.getLogger(__name__)

_raw_rich = os.getenv("FASTMCP_ENABLE_RICH_LOGGING")
if _raw_rich is not None:
    _enable_rich = _raw_rich.strip().lower() in ("1", "true", "yes")
else:
    _enable_rich = not is_http_mode()

if _enable_rich:
    from rich.console import Console
    from rich.logging import RichHandler

    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[
            RichHandler(
                console=Console(stderr=True),
                show_time=True,
                show_level=True,
                show_path=True,
                rich_tracebacks=True,
            )
        ],
    )
else:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler()],
    )

_install_health_probe_log_filter()
