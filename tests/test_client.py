"""Tests for client.py error mapping."""

from __future__ import annotations

from mcp_kubecost.client import KubecostClientError
from mcp_kubecost.errors import ErrorCode


class TestKubecostClientErrorToToolError:
    def _make(self, status_code: int) -> KubecostClientError:
        return KubecostClientError(
            status_code=status_code, message="err", url="http://x/model/savings", path="/model/savings"
        )

    def test_401_authentication_failed(self):
        te = self._make(401).to_tool_error()
        assert te.code == ErrorCode.AUTHENTICATION_FAILED
        assert te.retryable is False

    def test_403_permission_denied(self):
        te = self._make(403).to_tool_error()
        assert te.code == ErrorCode.PERMISSION_DENIED
        assert te.retryable is False
        assert "http://x" not in te.message
        assert "/model/savings" in te.message

    def test_404_not_found(self):
        te = self._make(404).to_tool_error()
        assert te.code == ErrorCode.NOT_FOUND
        assert te.retryable is False
        assert "http://x" not in te.message
        assert "/model/savings" in te.message

    def test_429_rate_limited(self):
        te = self._make(429).to_tool_error()
        assert te.code == ErrorCode.RATE_LIMITED
        assert te.retryable is True
        assert te.context["retry_after_seconds"] == 30

    def test_500_server_error(self):
        te = self._make(500).to_tool_error()
        assert te.code == ErrorCode.UPSTREAM_TIMEOUT
        assert te.retryable is True

    def test_503_server_error(self):
        te = self._make(503).to_tool_error()
        assert te.code == ErrorCode.UPSTREAM_TIMEOUT

    def test_400_bad_request(self):
        te = self._make(400).to_tool_error()
        assert te.code == ErrorCode.DATA_UNAVAILABLE
        assert te.retryable is False

    def test_str_contains_status_and_url(self):
        exc = self._make(404)
        assert "404" in str(exc)
        assert "http://x" in str(exc)
