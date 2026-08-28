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


class ConfigError(Exception):
    """Configuration or environment error."""
