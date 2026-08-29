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

_FASTMCP_ARGS = ("fastmcp", "run", "config/fastmcp-http.json", "--skip-env")


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


def _normalize_http_path(raw: str) -> str:
    """Return MCP_HTTP_PATH as a clean absolute path.

    Mirrors ``config.settings._get_oidc_redirect_path()``: reject anything that
    is not a plain path, then normalize the slashes. ``"/"`` is a legitimate
    value here (unlike the OAuth callback path) — it is the whole point of the
    setting.
    """
    if "://" in raw or "?" in raw or "#" in raw:
        raise ValueError(f"Invalid MCP_HTTP_PATH: {raw!r} (expected a path like /mcp, not a URL)")
    if ".." in raw:
        raise ValueError(f"Invalid MCP_HTTP_PATH: {raw!r} (must not contain '..')")
    if raw.startswith("-"):
        # A leading dash would be consumed by the fastmcp CLI as a flag rather
        # than read as the value of --path.
        raise ValueError(f"Invalid MCP_HTTP_PATH: {raw!r} (must not start with '-')")
    path = raw if raw.startswith("/") else f"/{raw}"
    path = path.rstrip("/")
    return path or "/"


def _fastmcp_args() -> tuple[str, ...]:
    """Return the ``fastmcp run`` argv, honouring MCP_HTTP_PATH.

    ``fastmcp run --path`` overrides FastMCP's own ``/mcp/`` default for the
    MCP endpoint, so the route can be changed at deploy time without rebuilding
    the image. Set MCP_HTTP_PATH="/" when a reverse proxy strips a path prefix
    (e.g. nginx maps /mcp/* to this Service), so that the prefix-stripped OAuth
    paths (/authorize, /token, ...) and the MCP endpoint all resolve at the root
    of this server.

    Raises:
        ValueError: MCP_HTTP_PATH is set to something that is not a path.
    """
    raw = os.environ.get("MCP_HTTP_PATH", "").strip()
    if not raw:
        return _FASTMCP_ARGS
    # A requested path is always forwarded, even when it matches FastMCP's
    # current default: the chart sets it to pin the route, not to restate a
    # default that could move under us.
    return (*_FASTMCP_ARGS, "--path", _normalize_http_path(raw))


def main() -> None:
    _load_env_file()
    _configure_http_logging()

    try:
        args = _fastmcp_args()
    except ValueError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)

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
