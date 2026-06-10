"""Structured error types for MCP tool responses.

Following the MCP Engineering Guide error response contract:
every error must be structured, typed, and actionable.
"""

from enum import Enum

from pydantic import BaseModel


class ErrorCode(Enum):
    """Standard error codes for tool responses."""

    NOT_FOUND = "not_found"
    INVALID_INPUT = "invalid_input"
    PERMISSION_DENIED = "permission_denied"
    UPSTREAM_TIMEOUT = "upstream_timeout"
    RATE_LIMITED = "rate_limited"
    DATA_UNAVAILABLE = "data_unavailable"
    AUTHENTICATION_FAILED = "authentication_failed"
    CONFIGURATION_ERROR = "configuration_error"


class ToolError(BaseModel):
    """Structured error response for MCP tools.

    Provides the LLM with enough information to decide what to do next.
    """

    code: ErrorCode
    message: str
    retryable: bool
    suggested_action: str
    context: dict = {}


class FinOpsError(Exception):
    """Base class for known business/domain errors."""

    error_code = "finops_error"
    retryable = False

    def to_payload(self) -> dict[str, object]:
        return {
            "error_code": self.error_code,
            "retryable": self.retryable,
            "message": str(self),
        }


class ConfigError(FinOpsError):
    """Configuration or environment error."""

    error_code = "config_error"


class ValidationError(FinOpsError):
    """Input validation error for query parameters."""

    error_code = "validation_error"


class ApiTransportError(FinOpsError):
    """Network and transport-layer API failures."""

    error_code = "api_transport_error"
    retryable = True


class ApiResponseError(FinOpsError):
    """Unexpected/malformed API responses or status errors."""

    error_code = "api_response_error"
