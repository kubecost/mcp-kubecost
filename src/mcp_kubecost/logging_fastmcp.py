"""Global logging configuration for the MCP server.

Import this module once in server.py to configure the root logger.
All other modules should use: logging.getLogger(__name__)
"""

import logging
import os

from mcp_kubecost.config.settings import is_http_mode

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
