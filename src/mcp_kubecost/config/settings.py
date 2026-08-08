"""Environment-backed runtime settings."""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlparse

from mcp_kubecost.errors import ConfigError


@dataclass(frozen=True)
class Settings:
    """Runtime settings used by transport and tool layers."""

    kubecost_base_url: str
    kubecost_api_base_path: str
    KUBECOST_API_KEY: str | None
    use_cac_views: bool
    ssl_verify: bool | str  # passed directly to httpx verify=
    request_timeout_seconds: float
    retry_count: int
    default_window: str
    http_host: str
    http_port: int
    show_banner: bool
    log_level: str

    def to_loggable_dict(self) -> dict:
        """Return a copy of settings safe for logging (sensitive fields redacted)."""
        d = dataclasses.asdict(self)
        if d.get("KUBECOST_API_KEY") is not None:
            d["KUBECOST_API_KEY"] = "***"
        return d


def _get_required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _get_url_env(name: str) -> str:
    """Return a required env var that must be an http(s) URL."""
    value = _get_required_env(name).rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError(f"Invalid URL for {name}: {value!r} (expected http(s)://host[...])")
    return value


def _get_ssl_verify_env() -> bool | str:
    """Return the httpx ssl verify value from SSL_VERIFY / SSL_CA_BUNDLE.

    SSL_CA_BUNDLE=/path/to/ca.crt  → use that bundle (implies verify=True)
    KUBECOST_SSL_VERIFY=false               → disable verification (insecure)
    (default)                      → True (httpx default)
    """
    ca_bundle = os.getenv("SSL_CA_BUNDLE", "").strip()
    if ca_bundle:
        return ca_bundle
    return _get_bool_env("KUBECOST_SSL_VERIFY", True)


def _get_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, str(default)).strip().lower()
    if raw in ("1", "true", "yes"):
        return True
    if raw in ("0", "false", "no"):
        return False
    raise ConfigError(f"Invalid boolean for {name}: {raw!r} (expected true/false)")


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
    kubecost_base_url = _get_url_env("KUBECOST_BASE_URL")

    return Settings(
        kubecost_base_url=kubecost_base_url,
        kubecost_api_base_path=os.getenv("KUBECOST_API_BASE_PATH", "/model").strip().rstrip("/"),
        KUBECOST_API_KEY=os.getenv("KUBECOST_API_KEY", "").strip() or None,
        ssl_verify=_get_ssl_verify_env(),
        request_timeout_seconds=_get_float_env("REQUEST_TIMEOUT_SECONDS", 15.0),
        retry_count=_get_int_env("REQUEST_RETRY_COUNT", 2),
        default_window=os.getenv("DEFAULT_WINDOW", "15d").strip(),
        http_host=os.getenv("MCP_HTTP_HOST", "127.0.0.1").strip(),
        http_port=_get_int_env("MCP_HTTP_PORT", 8000),
        show_banner=False,
        log_level=os.getenv("FASTMCP_LOG_LEVEL", "INFO").upper(),
        use_cac_views=_get_bool_env("USE_CAC_VIEWS", False),
    )
