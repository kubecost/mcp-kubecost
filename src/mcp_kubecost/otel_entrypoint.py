"""Distroless-safe HTTP entrypoint that gates OpenTelemetry on FASTMCP_TELEMETRY_MODE.

On FastMCP 3.4.x, FASTMCP_TELEMETRY_MODE is not read by FastMCP itself; we reuse
the name as the process-wide switch for whether the OTEL SDK runs via
``opentelemetry-instrument``. Any value other than ``off`` enables the SDK.

This module runs *before* ``server.py`` is imported, so it deliberately avoids
importing anything from ``mcp_kubecost.config``: the settings layer belongs to
the server process, and a config import failure here would leave the container
with no diagnostic at all. Validation errors are reported on stderr with a
non-zero exit instead of raising ``ConfigError``.
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

_FASTMCP_ARGS = ("fastmcp", "run", "config/fastmcp-http.json", "--skip-env", "--path", "/mcp")


def _configure_http_logging() -> None:
    """Configure plain, banner-free FastMCP output before the CLI imports FastMCP.

    ``server.py`` also enforces these settings, which covers direct
    ``fastmcp run`` usage. The container entrypoint must set them earlier so
    CLI startup messages and import failures cannot initialize Rich handlers
    or render the terminal-oriented server banner in pod logs.
    """
    os.environ["FASTMCP_ENABLE_RICH_LOGGING"] = "false"
    os.environ["FASTMCP_SHOW_SERVER_BANNER"] = "false"


def _load_env_file() -> None:
    """Read ``.env`` into ``os.environ`` before the argv is built.

    ``server.py`` also calls ``load_dotenv(".env")``, but that happens inside
    the process this module ``execvp``s — far too late to influence the argv
    assembled here. ``load_dotenv`` does not overwrite variables that are
    already set, so the real process environment (Docker ``-e``, the chart's
    ConfigMap) still wins over the file; and because ``execvp`` passes the
    current ``os.environ`` to the new image, everything loaded here reaches the
    server process too.
    """
    load_dotenv(".env")


def _telemetry_enabled() -> bool:
    mode = os.environ.get("FASTMCP_TELEMETRY_MODE", "off").strip().lower()
    return mode not in {"", "off"}


def _fastmcp_args() -> tuple[str, ...]:
    """Return the ``fastmcp run`` argv with the public MCP path pinned."""
    return _FASTMCP_ARGS


def main() -> None:
    _load_env_file()
    _configure_http_logging()

    args = _fastmcp_args()

    if _telemetry_enabled():
        try:
            os.execvp("opentelemetry-instrument", ["opentelemetry-instrument", *args])
        except OSError as exc:
            # opentelemetry-instrument ships in the optional 'otel' extra. Serving
            # without traces beats refusing to start over a missing exporter.
            print(
                f"Could not exec 'opentelemetry-instrument' ({exc}); starting without telemetry. "
                "Install the 'otel' extra (uv sync --extra otel) to enable tracing.",
                file=sys.stderr,
            )

    try:
        os.execvp(args[0], list(args))
    except OSError as exc:
        print(f"Failed to exec {args[0]!r}: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
