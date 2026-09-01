"""Tests for the HTTP entrypoint's argv construction."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mcp_kubecost.config.settings import is_http_mode
from mcp_kubecost.otel_entrypoint import (
    _FASTMCP_ARGS,
    _fastmcp_args,
    _load_env_file,
    main,
)


class TestFastmcpArgs:
    def test_http_config_lives_under_config(self):
        assert _FASTMCP_ARGS[2] == "config/fastmcp-http.json"
        assert Path(_FASTMCP_ARGS[2]).is_file()

    def test_mcp_path_is_pinned(self):
        assert _fastmcp_args() == _FASTMCP_ARGS
        assert _FASTMCP_ARGS[-2:] == ("--path", "/mcp")

    def test_pinned_path_is_still_detected_as_http_mode(self):
        assert is_http_mode(list(_fastmcp_args())) is True


class TestMain:
    def test_configures_plain_logging_before_exec(self, monkeypatch):
        monkeypatch.setenv("FASTMCP_ENABLE_RICH_LOGGING", "true")
        monkeypatch.setenv("FASTMCP_SHOW_SERVER_BANNER", "true")
        monkeypatch.setenv("FASTMCP_TELEMETRY_MODE", "off")

        def inspect_environment_then_stop(*_args):
            assert os.environ["FASTMCP_ENABLE_RICH_LOGGING"] == "false"
            assert os.environ["FASTMCP_SHOW_SERVER_BANNER"] == "false"
            raise RuntimeError("stop before replacing the test process")

        monkeypatch.setattr("os.execvp", inspect_environment_then_stop)

        with pytest.raises(RuntimeError, match="stop before replacing"):
            main()


class TestLoadEnvFile:
    def test_dotenv_is_loaded_before_exec(self, monkeypatch, tmp_path):
        (tmp_path / ".env").write_text("FASTMCP_TELEMETRY_MODE=off\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("FASTMCP_TELEMETRY_MODE", raising=False)

        _load_env_file()

        assert os.environ["FASTMCP_TELEMETRY_MODE"] == "off"

    def test_process_env_wins_over_dotenv(self, monkeypatch, tmp_path):
        (tmp_path / ".env").write_text("FASTMCP_TELEMETRY_MODE=native\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("FASTMCP_TELEMETRY_MODE", "off")

        _load_env_file()

        assert os.environ["FASTMCP_TELEMETRY_MODE"] == "off"
