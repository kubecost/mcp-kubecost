"""Tests for operator-facing OpenTelemetry status logs."""

from __future__ import annotations

import logging
import sys
import types
from pathlib import Path

from mcp_kubecost.telemetry import (
    _auto_instrumentation_loaded,
    _redact_endpoint,
    log_telemetry_status,
)

_SRC = Path(__file__).resolve().parents[1] / "src" / "mcp_kubecost"


def _clear_otel_env(monkeypatch) -> None:
    for name in (
        "FASTMCP_TELEMETRY_MODE",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_SERVICE_NAME",
        "OTEL_TRACES_EXPORTER",
        "OTEL_METRICS_EXPORTER",
        "OTEL_LOGS_EXPORTER",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
    ):
        monkeypatch.delenv(name, raising=False)


class TestAutoInstrumentationLoaded:
    def test_false_when_sitecustomize_is_unrelated(self, monkeypatch):
        mod = types.ModuleType("sitecustomize")
        mod.__file__ = "/usr/lib/python3.12/sitecustomize.py"
        monkeypatch.setitem(sys.modules, "sitecustomize", mod)
        for name in [k for k in sys.modules if k.startswith("opentelemetry.instrumentation.auto_instrumentation")]:
            monkeypatch.delitem(sys.modules, name, raising=False)
        assert _auto_instrumentation_loaded() is False

    def test_true_when_sitecustomize_path_is_opentelemetry(self, monkeypatch):
        mod = types.ModuleType("sitecustomize")
        mod.__file__ = (
            "/app/.venv/lib/python3.12/site-packages/"
            "opentelemetry/instrumentation/auto_instrumentation/sitecustomize.py"
        )
        monkeypatch.setitem(sys.modules, "sitecustomize", mod)
        assert _auto_instrumentation_loaded() is True

    def test_true_when_auto_instrumentation_package_loaded(self, monkeypatch):
        monkeypatch.delitem(sys.modules, "sitecustomize", raising=False)
        monkeypatch.setitem(
            sys.modules,
            "opentelemetry.instrumentation.auto_instrumentation.sitecustomize",
            types.ModuleType("opentelemetry.instrumentation.auto_instrumentation.sitecustomize"),
        )
        assert _auto_instrumentation_loaded() is True


class TestRedactEndpoint:
    def test_leaves_plain_urls_unchanged(self):
        assert _redact_endpoint("http://collector:4317") == "http://collector:4317"

    def test_strips_userinfo(self):
        assert _redact_endpoint("https://api:s3cret@collector:4318/v1/traces") == "https://collector:4318/v1/traces"


class TestLogTelemetryStatus:
    def test_off_is_debug_only(self, monkeypatch, caplog):
        _clear_otel_env(monkeypatch)
        monkeypatch.setenv("FASTMCP_TELEMETRY_MODE", "off")
        monkeypatch.setattr("mcp_kubecost.telemetry._auto_instrumentation_loaded", lambda: False)

        with caplog.at_level(logging.DEBUG, logger="mcp_kubecost.telemetry"):
            log_telemetry_status()

        assert "OpenTelemetry wrapper is off" in caplog.text
        assert not any(r.levelno >= logging.INFO for r in caplog.records)

    def test_requested_but_unwrapped_is_warning(self, monkeypatch, caplog):
        _clear_otel_env(monkeypatch)
        monkeypatch.setenv("FASTMCP_TELEMETRY_MODE", "native")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
        monkeypatch.setattr("mcp_kubecost.telemetry._auto_instrumentation_loaded", lambda: False)

        with caplog.at_level(logging.INFO, logger="mcp_kubecost.telemetry"):
            log_telemetry_status()

        assert any(r.levelno == logging.WARNING for r in caplog.records)
        assert "auto-instrumentation is not loaded" in caplog.text
        assert "http://collector:4317" not in caplog.text

    def test_wrapped_without_endpoint_is_warning(self, monkeypatch, caplog):
        _clear_otel_env(monkeypatch)
        monkeypatch.setenv("FASTMCP_TELEMETRY_MODE", "native")
        monkeypatch.setattr("mcp_kubecost.telemetry._auto_instrumentation_loaded", lambda: True)

        with caplog.at_level(logging.INFO, logger="mcp_kubecost.telemetry"):
            log_telemetry_status()

        assert "OTEL_EXPORTER_OTLP_ENDPOINT is unset" in caplog.text
        assert "OpenTelemetry is active" not in caplog.text

    def test_wrapped_with_endpoint_is_info(self, monkeypatch, caplog):
        _clear_otel_env(monkeypatch)
        monkeypatch.setenv("FASTMCP_TELEMETRY_MODE", "native")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
        monkeypatch.setenv("OTEL_SERVICE_NAME", "mcp-kubecost")
        monkeypatch.setenv("OTEL_METRICS_EXPORTER", "none")
        monkeypatch.setenv("OTEL_LOGS_EXPORTER", "none")
        monkeypatch.setattr("mcp_kubecost.telemetry._auto_instrumentation_loaded", lambda: True)

        with caplog.at_level(logging.INFO, logger="mcp_kubecost.telemetry"):
            log_telemetry_status()

        assert "OpenTelemetry is active" in caplog.text
        assert "service=mcp-kubecost" in caplog.text
        assert "endpoint=http://collector:4317" in caplog.text
        assert "metrics=none" in caplog.text
        assert "logs=none" in caplog.text

    def test_does_not_log_exporter_headers(self, monkeypatch, caplog):
        _clear_otel_env(monkeypatch)
        monkeypatch.setenv("FASTMCP_TELEMETRY_MODE", "native")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Bearer super-secret-token")
        monkeypatch.setattr("mcp_kubecost.telemetry._auto_instrumentation_loaded", lambda: True)

        with caplog.at_level(logging.DEBUG, logger="mcp_kubecost.telemetry"):
            log_telemetry_status()

        assert "super-secret-token" not in caplog.text
        assert "Bearer" not in caplog.text

    def test_redacts_endpoint_userinfo_in_info_line(self, monkeypatch, caplog):
        _clear_otel_env(monkeypatch)
        monkeypatch.setenv("FASTMCP_TELEMETRY_MODE", "native")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://api:s3cret@collector:4318")
        monkeypatch.setattr("mcp_kubecost.telemetry._auto_instrumentation_loaded", lambda: True)

        with caplog.at_level(logging.INFO, logger="mcp_kubecost.telemetry"):
            log_telemetry_status()

        assert "s3cret" not in caplog.text
        assert "endpoint=https://collector:4318" in caplog.text

    def test_quiets_sdk_logger_when_server_is_not_debug(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        monkeypatch.setenv("FASTMCP_TELEMETRY_MODE", "off")
        mcp_logger = logging.getLogger("mcp_kubecost")
        otel_logger = logging.getLogger("opentelemetry")
        previous_mcp = mcp_logger.level
        previous_otel = otel_logger.level
        try:
            mcp_logger.setLevel(logging.INFO)
            otel_logger.setLevel(logging.NOTSET)
            log_telemetry_status()
            assert otel_logger.level == logging.WARNING
        finally:
            mcp_logger.setLevel(previous_mcp)
            otel_logger.setLevel(previous_otel)

    def test_leaves_sdk_logger_alone_when_server_is_debug(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        monkeypatch.setenv("FASTMCP_TELEMETRY_MODE", "off")
        mcp_logger = logging.getLogger("mcp_kubecost")
        otel_logger = logging.getLogger("opentelemetry")
        previous_mcp = mcp_logger.level
        previous_otel = otel_logger.level
        try:
            mcp_logger.setLevel(logging.DEBUG)
            otel_logger.setLevel(logging.NOTSET)
            log_telemetry_status()
            assert otel_logger.level == logging.NOTSET
        finally:
            mcp_logger.setLevel(previous_mcp)
            otel_logger.setLevel(previous_otel)


class TestNoSdkImport:
    def test_src_does_not_import_opentelemetry(self):
        hits: list[str] = []
        for path in _SRC.rglob("*.py"):
            for lineno, line in enumerate(path.read_text().splitlines(), start=1):
                stripped = line.lstrip()
                if stripped.startswith("import opentelemetry") or stripped.startswith("from opentelemetry"):
                    hits.append(f"{path.relative_to(_SRC.parent.parent)}:{lineno}:{stripped}")
        assert hits == []
