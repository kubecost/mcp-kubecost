"""Environment-backed runtime settings."""

from __future__ import annotations

import dataclasses
import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from urllib.parse import urlparse

from mcp_kubecost.errors import ConfigError

logger = logging.getLogger(__name__)


class AuthMode(Enum):
    """Supported authentication modes for the HTTP transport."""

    NONE = "none"
    OPEN = "open"  # Helm: explicit no-auth when a route is exposed; runtime same as none
    OIDC = "oidc"
    API_KEY = "api_key"


_HTTP_TRANSPORTS = frozenset({"http", "sse", "streamable-http"})


@dataclass(frozen=True)
class Settings:
    """Runtime settings used by transport and tool layers."""

    kubecost_base_url: str
    kubecost_api_base_path: str
    KUBECOST_API_KEY: str | None
    require_client_api_key: bool
    use_cac_views: bool
    ssl_verify: bool | str  # passed directly to httpx verify=
    request_timeout_seconds: float
    retry_count: int
    default_window: str
    log_level: str
    rate_limit_requests_per_second: float
    rate_limit_burst_capacity: int
    max_concurrent_tool_calls: int

    # OIDC authentication (HTTP transport only)
    auth_mode: AuthMode
    oidc_issuer_url: str | None
    oidc_client_id: str | None
    oidc_client_secret: str | None
    oidc_audience: str | None
    oidc_base_url: str | None
    oidc_redirect_path: str
    oidc_required_scopes: list[str]
    oidc_storage_path: str
    oidc_jwt_signing_key: str | None
    oidc_storage_encryption_key: str | None

    def to_loggable_dict(self) -> dict:
        """Return a copy of settings safe for logging (sensitive fields redacted)."""
        d = dataclasses.asdict(self)
        if d.get("KUBECOST_API_KEY") is not None:
            d["KUBECOST_API_KEY"] = "***"
        for name in (
            "oidc_client_secret",
            "oidc_jwt_signing_key",
            "oidc_storage_encryption_key",
        ):
            if d.get(name) is not None:
                d[name] = "***"
        # Serialize auth_mode as its string value for readability
        d["auth_mode"] = self.auth_mode.value
        return d


def is_http_mode(argv: Sequence[str] | None = None) -> bool:
    """Return True when this process is (or will be) serving Streamable HTTP.

    FastMCP's CLI applies ``transport`` from ``config/fastmcp-http.json`` only when it
    calls ``run_async``, so ``fastmcp.settings.transport`` is still ``stdio``
    while ``server.py`` is imported. Detect HTTP from ``FASTMCP_TRANSPORT`` or
    the CLI argv instead.
    """
    transport = os.getenv("FASTMCP_TRANSPORT", "").strip().lower()
    if transport in _HTTP_TRANSPORTS:
        return True
    args = list(sys.argv if argv is None else argv)
    if any("fastmcp-http.json" in arg for arg in args):
        return True
    for i, arg in enumerate(args):
        if arg.startswith("--transport="):
            return arg.split("=", 1)[1].strip().lower() in _HTTP_TRANSPORTS
        if arg in {"--transport", "-t"} and i + 1 < len(args):
            return args[i + 1].strip().lower() in _HTTP_TRANSPORTS
    return False


def apply_http_rich_logging() -> None:
    """Disable FastMCP Rich/ANSI logs when serving HTTP.

    FastMCP configures its logger at import time. The CLI imports FastMCP
    before ``server.py``, so we both set the env var and reconfigure the
    already-created singleton. Call after ``import fastmcp``.
    """
    if not is_http_mode():
        return
    os.environ["FASTMCP_ENABLE_RICH_LOGGING"] = "false"
    import fastmcp
    from fastmcp.utilities.logging import configure_logging

    fastmcp.settings.enable_rich_logging = False
    configure_logging(level=fastmcp.settings.log_level)
    logger.debug("FastMCP rich logging enabled: %s", fastmcp.settings.enable_rich_logging)


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
    """Return the httpx ssl verify value from KUBECOST_SSL_VERIFY / SSL_CA_BUNDLE.

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


def _get_auth_mode() -> AuthMode:
    """Parse AUTH_MODE from environment, defaulting to 'none'."""
    raw = os.getenv("AUTH_MODE", "none").strip().lower()
    if raw == "both":
        raise ConfigError("AUTH_MODE='both' was removed; set AUTH_MODE=oidc and REQUIRE_CLIENT_API_KEY=true")
    try:
        return AuthMode(raw)
    except ValueError as exc:
        valid = ", ".join(m.value for m in AuthMode)
        raise ConfigError(f"Invalid AUTH_MODE: {raw!r} (expected one of: {valid})") from exc


def _get_oidc_scopes() -> list[str]:
    """Parse OIDC_REQUIRED_SCOPES as comma-separated list."""
    raw = os.getenv("OIDC_REQUIRED_SCOPES", "openid,profile").strip()
    return [s.strip() for s in raw.split(",") if s.strip()]


_DEFAULT_OIDC_REDIRECT_PATH = "/auth-mcp"
_DEFAULT_OIDC_STORAGE_PATH = "/var/lib/mcp-kubecost/oauth"


def _get_oidc_redirect_path() -> str:
    """Return the FastMCP OAuth callback path (OIDC_REDIRECT_PATH).

    Defaults to ``/auth-mcp`` — most deployments run this server as a
    sub-path on an existing Kubecost frontend, where FastMCP's own default
    of ``/auth/callback`` collides with Kubecost's ``auth_request`` on
    ``location /auth``. Use ``/auth/callback`` instead when this server has
    a dedicated hostname (not a Kubecost sub-path). Only this callback is
    remountable; ``/register``, ``/authorize``, and ``/token`` stay at the
    server root.
    """
    raw = os.getenv("OIDC_REDIRECT_PATH", _DEFAULT_OIDC_REDIRECT_PATH).strip()
    if not raw:
        return _DEFAULT_OIDC_REDIRECT_PATH
    if "://" in raw or "?" in raw or "#" in raw:
        raise ConfigError(f"Invalid OIDC_REDIRECT_PATH: {raw!r} (expected a path like /auth-mcp, not a URL)")
    if ".." in raw:
        raise ConfigError(f"Invalid OIDC_REDIRECT_PATH: {raw!r} (must not contain '..')")
    path = raw if raw.startswith("/") else f"/{raw}"
    if path != "/":
        path = path.rstrip("/")
    if path == "/":
        raise ConfigError("Invalid OIDC_REDIRECT_PATH: '/' (use a dedicated callback path)")
    return path


def _get_oidc_storage_path() -> str:
    """Return the absolute directory used for encrypted OAuth state."""
    raw = os.getenv("OIDC_STORAGE_PATH", _DEFAULT_OIDC_STORAGE_PATH).strip()
    path = os.path.abspath(raw)
    if not raw or not os.path.isabs(raw):
        raise ConfigError(f"Invalid OIDC_STORAGE_PATH: {raw!r} (expected an absolute path)")
    return path


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and cache settings from environment."""
    kubecost_base_url = _get_url_env("KUBECOST_BASE_URL")

    auth_mode = _get_auth_mode()

    # Validate that OIDC vars are present when OIDC is enabled
    oidc_issuer_url: str | None = os.getenv("OIDC_ISSUER_URL", "").strip() or None
    oidc_client_id: str | None = os.getenv("OIDC_CLIENT_ID", "").strip() or None
    oidc_client_secret: str | None = os.getenv("OIDC_CLIENT_SECRET", "").strip() or None
    oidc_audience: str | None = os.getenv("OIDC_AUDIENCE", "").strip() or None
    oidc_base_url: str | None = os.getenv("OIDC_BASE_URL", "").strip() or None
    oidc_jwt_signing_key: str | None = os.getenv("OIDC_JWT_SIGNING_KEY", "").strip() or None
    oidc_storage_encryption_key: str | None = os.getenv("OIDC_STORAGE_ENCRYPTION_KEY", "").strip() or None

    if auth_mode == AuthMode.OIDC:
        missing = []
        if not oidc_issuer_url:
            missing.append("OIDC_ISSUER_URL")
        if not oidc_client_id:
            missing.append("OIDC_CLIENT_ID")
        if not oidc_client_secret:
            missing.append("OIDC_CLIENT_SECRET")
        if not oidc_base_url:
            missing.append("OIDC_BASE_URL")
        if not oidc_jwt_signing_key:
            missing.append("OIDC_JWT_SIGNING_KEY")
        if not oidc_storage_encryption_key:
            missing.append("OIDC_STORAGE_ENCRYPTION_KEY")
        if missing:
            raise ConfigError(f"AUTH_MODE={auth_mode.value} requires: {', '.join(missing)}")

        assert oidc_jwt_signing_key is not None
        if len(oidc_jwt_signing_key) < 32:
            raise ConfigError("OIDC_JWT_SIGNING_KEY must be at least 32 characters")

    # AUTH_MODE=api_key is the same gate as REQUIRE_CLIENT_API_KEY=true.
    require_client_api_key = _get_bool_env("REQUIRE_CLIENT_API_KEY", False) or auth_mode == AuthMode.API_KEY
    request_timeout_seconds = _get_float_env("REQUEST_TIMEOUT_SECONDS", 15.0)
    if request_timeout_seconds <= 0:
        raise ConfigError("REQUEST_TIMEOUT_SECONDS must be greater than 0")
    retry_count = _get_int_env("REQUEST_RETRY_COUNT", 2)
    if retry_count < 0:
        raise ConfigError("REQUEST_RETRY_COUNT must be 0 or greater")
    rate_limit_requests_per_second = _get_float_env("MCP_RATE_LIMIT_REQUESTS_PER_SECOND", 10.0)
    if rate_limit_requests_per_second <= 0:
        raise ConfigError("MCP_RATE_LIMIT_REQUESTS_PER_SECOND must be greater than 0")
    rate_limit_burst_capacity = _get_int_env("MCP_RATE_LIMIT_BURST_CAPACITY", 20)
    if rate_limit_burst_capacity <= 0:
        raise ConfigError("MCP_RATE_LIMIT_BURST_CAPACITY must be greater than 0")
    max_concurrent_tool_calls = _get_int_env("MCP_MAX_CONCURRENT_TOOL_CALLS", 10)
    if max_concurrent_tool_calls <= 0:
        raise ConfigError("MCP_MAX_CONCURRENT_TOOL_CALLS must be greater than 0")

    return Settings(
        kubecost_base_url=kubecost_base_url,
        kubecost_api_base_path=os.getenv("KUBECOST_API_BASE_PATH", "/model").strip().rstrip("/"),
        KUBECOST_API_KEY=os.getenv("KUBECOST_API_KEY", "").strip() or None,
        require_client_api_key=require_client_api_key,
        ssl_verify=_get_ssl_verify_env(),
        request_timeout_seconds=request_timeout_seconds,
        retry_count=retry_count,
        default_window=os.getenv("DEFAULT_WINDOW", "15d").strip(),
        log_level=os.getenv("FASTMCP_LOG_LEVEL", "INFO").upper(),
        rate_limit_requests_per_second=rate_limit_requests_per_second,
        rate_limit_burst_capacity=rate_limit_burst_capacity,
        max_concurrent_tool_calls=max_concurrent_tool_calls,
        use_cac_views=_get_bool_env("USE_CAC_VIEWS", False),
        auth_mode=auth_mode,
        oidc_issuer_url=oidc_issuer_url,
        oidc_client_id=oidc_client_id,
        oidc_client_secret=oidc_client_secret,
        oidc_audience=oidc_audience,
        oidc_base_url=oidc_base_url,
        oidc_redirect_path=_get_oidc_redirect_path(),
        oidc_required_scopes=_get_oidc_scopes(),
        oidc_storage_path=_get_oidc_storage_path(),
        oidc_jwt_signing_key=oidc_jwt_signing_key,
        oidc_storage_encryption_key=oidc_storage_encryption_key,
    )
