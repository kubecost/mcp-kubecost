"""Environment-backed runtime settings."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from mcp_kubecost.errors import ConfigError


@dataclass(frozen=True)
class Settings:
    """Runtime settings used by transport and tool layers."""

    kubecost_base_url: str
    kubecost_base_path: str
    kubecost_container_savings_path: str
    KUBECOST_API_KEY: str | None
    request_timeout_seconds: float
    retry_count: int
    default_window: str
    http_host: str
    http_port: int
    show_banner: bool


def _get_required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _get_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"Invalid integer for {name}: {raw}") from exc


def _get_float_env(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"Invalid float for {name}: {raw}") from exc


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and cache settings from environment."""
    # Default to Kubecost Kubecost API if not specified
    kubecost_base_url = os.getenv("KUBECOST_BASE_URL", "https://actions.demo.kubecost.cloud").strip().rstrip("/")

    return Settings(
        kubecost_base_url=kubecost_base_url,
        kubecost_base_path=os.getenv("KUBECOST_BASE_PATH", "/model/allocation").strip(),
        kubecost_container_savings_path=os.getenv(
            "KUBECOST_CONTAINER_SAVINGS_PATH", "/model/savings/requestSizingV2"
        ).strip(),
        KUBECOST_API_KEY=os.getenv("KUBECOST_API_KEY", "").strip() or None,
        request_timeout_seconds=_get_float_env("REQUEST_TIMEOUT_SECONDS", 15.0),
        retry_count=_get_int_env("REQUEST_RETRY_COUNT", 2),
        default_window=os.getenv("DEFAULT_WINDOW", "7d").strip(),
        http_host=os.getenv("MCP_HTTP_HOST", "127.0.0.1").strip(),
        http_port=_get_int_env("MCP_HTTP_PORT", 8000),
        show_banner=False,
    )
