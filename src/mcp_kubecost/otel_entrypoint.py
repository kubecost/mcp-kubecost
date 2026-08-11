"""Distroless-safe HTTP entrypoint that gates OpenTelemetry on FASTMCP_TELEMETRY_MODE.

On FastMCP 3.4.x, FASTMCP_TELEMETRY_MODE is not read by FastMCP itself; we reuse
the name as the process-wide switch for whether the OTEL SDK runs via
``opentelemetry-instrument``. Any value other than ``off`` enables the SDK.
"""

from __future__ import annotations

import os
import sys

_FASTMCP_ARGS = ("fastmcp", "run", "fastmcp-http.json", "--skip-env")


def _telemetry_enabled() -> bool:
    mode = os.environ.get("FASTMCP_TELEMETRY_MODE", "off").strip().lower()
    return mode not in {"", "off"}


def main() -> None:
    if _telemetry_enabled():
        argv = ["opentelemetry-instrument", *_FASTMCP_ARGS]
    else:
        argv = list(_FASTMCP_ARGS)
    try:
        os.execvp(argv[0], argv)
    except OSError as exc:
        print(f"Failed to exec {argv[0]!r}: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
