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
        try:
            os.execvp("opentelemetry-instrument", ["opentelemetry-instrument", *_FASTMCP_ARGS])
        except OSError as exc:
            # opentelemetry-instrument ships in the optional 'otel' extra. Serving
            # without traces beats refusing to start over a missing exporter.
            print(
                f"Could not exec 'opentelemetry-instrument' ({exc}); starting without telemetry. "
                "Install the 'otel' extra (uv sync --extra otel) to enable tracing.",
                file=sys.stderr,
            )

    try:
        os.execvp(_FASTMCP_ARGS[0], list(_FASTMCP_ARGS))
    except OSError as exc:
        print(f"Failed to exec {_FASTMCP_ARGS[0]!r}: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
