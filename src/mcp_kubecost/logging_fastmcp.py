"""Global logging configuration for the MCP server.

Import this module once in server.py to configure the root logger.
All other modules should use: logging.getLogger(__name__)
"""

import logging
import os

from rich.console import Console
from rich.logging import RichHandler

# Get log level from environment variable, default to INFO
log_level = os.getenv("FASTMCP_LOG_LEVEL", "INFO").upper()
level = getattr(logging, log_level, logging.INFO)
logger = logging.getLogger(__name__)
# Configure root logger with Rich formatting to match FastMCP output
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
