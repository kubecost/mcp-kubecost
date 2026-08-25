"""Tests for the HTTP entrypoint's argv construction."""

from __future__ import annotations

import pytest

from mcp_kubecost.config.settings import is_http_mode
from mcp_kubecost.otel_entrypoint import _FASTMCP_ARGS, _fastmcp_args, _load_env_file, main


class TestFastmcpArgs:
    def test_unset_leaves_argv_untouched(self, monkeypatch):
        monkeypatch.delenv("MCP_HTTP_PATH", raising=False)
        assert _fastmcp_args() == _FASTMCP_ARGS

    def test_blank_leaves_argv_untouched(self, monkeypatch):
        monkeypatch.setenv("MCP_HTTP_PATH", "   ")
        assert _fastmcp_args() == _FASTMCP_ARGS

    def test_root_path_appends_override(self, monkeypatch):
        monkeypatch.setenv("MCP_HTTP_PATH", "/")
        assert _fastmcp_args() == (*_FASTMCP_ARGS, "--path", "/")

    def test_custom_path_appends_override(self, monkeypatch):
        monkeypatch.setenv("MCP_HTTP_PATH", "/mcp")
        assert _fastmcp_args() == (*_FASTMCP_ARGS, "--path", "/mcp")

    def test_override_is_still_detected_as_http_mode(self, monkeypatch):
        # is_http_mode() inspects argv; the extra flags must not hide the
        # HTTP config file and downgrade logging to stdio defaults.
        monkeypatch.setenv("MCP_HTTP_PATH", "/")
        assert is_http_mode(list(_fastmcp_args())) is True


class TestHttpPathNormalization:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("mcp", "/mcp"),
            ("/mcp/", "/mcp"),
            ("mcp/", "/mcp"),
            ("/", "/"),
            ("//", "/"),
            ("/mcp/v1", "/mcp/v1"),
            ("  /mcp  ", "/mcp"),
        ],
    )
    def test_normalized(self, monkeypatch, raw, expected):
        monkeypatch.setenv("MCP_HTTP_PATH", raw)
        assert _fastmcp_args() == (*_FASTMCP_ARGS, "--path", expected)

    @pytest.mark.parametrize(
        ("raw", "reason"),
        [
            ("https://kubecost.example.com/mcp", "not a URL"),
            ("/mcp?a=b", "not a URL"),
            ("/mcp#frag", "not a URL"),
            ("/../mcp", "'..'"),
            ("-p", "'-'"),
        ],
    )
    def test_rejected(self, monkeypatch, raw, reason):
        monkeypatch.setenv("MCP_HTTP_PATH", raw)
        with pytest.raises(ValueError, match=reason):
            _fastmcp_args()


class TestMain:
    def test_invalid_path_exits_non_zero_before_exec(self, monkeypatch, capsys):
        def fail_exec(*args, **kwargs):  # pragma: no cover - must not be reached
            raise AssertionError("execvp must not run with an invalid MCP_HTTP_PATH")

        monkeypatch.setattr("os.execvp", fail_exec)
        monkeypatch.setenv("MCP_HTTP_PATH", "https://kubecost.example.com/mcp")

        with pytest.raises(SystemExit) as exc:
            main()

        assert exc.value.code == 1
        assert "Invalid MCP_HTTP_PATH" in capsys.readouterr().err


class TestLoadEnvFile:
    def test_dotenv_value_reaches_the_argv(self, monkeypatch, tmp_path):
        # MCP_HTTP_PATH is read before execvp, so server.py's own load_dotenv()
        # runs too late; the entrypoint must read .env itself.
        (tmp_path / ".env").write_text("MCP_HTTP_PATH=/\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("MCP_HTTP_PATH", raising=False)

        _load_env_file()

        assert _fastmcp_args() == (*_FASTMCP_ARGS, "--path", "/")

    def test_process_env_wins_over_dotenv(self, monkeypatch, tmp_path):
        (tmp_path / ".env").write_text("MCP_HTTP_PATH=/from-dotenv\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("MCP_HTTP_PATH", "/from-process-env")

        _load_env_file()

        assert _fastmcp_args() == (*_FASTMCP_ARGS, "--path", "/from-process-env")
