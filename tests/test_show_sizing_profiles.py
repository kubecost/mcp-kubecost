"""Tests for scripts/show_sizing_profiles.py."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
import scripts.show_sizing_profiles as sps

PROFILE_NAMES = ("production", "high-availability", "development")


class TestCheckMode:
    def test_shipped_profiles_pass(self, capsys: pytest.CaptureFixture[str]):
        assert sps.main(["--check"]) == 0
        assert "All 9 invariants hold" in capsys.readouterr().out

    def test_check_json_reports_ok(self, capsys: pytest.CaptureFixture[str]):
        assert sps.main(["--check", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload.keys() == {"invariants", "ok"}

    def test_detects_ram_squeezed_harder_than_cpu(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        """The checker must actually fail on a bad profile, not vacuously pass.

        This is the exact inversion that shipped once: RAM sized more aggressively
        than CPU, on the axis that OOM-kills rather than throttles.
        """
        broken = _mutated_profiles(development={"target_cpu_utilization": 0.50, "target_ram_utilization": 0.90})
        monkeypatch.setattr(sps, "SIZING_PROFILES", broken)

        assert sps.main(["--check", "--json"]) == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        failed = {r["name"] for r in payload["invariants"] if not r["ok"]}
        assert "RAM target never exceeds CPU target" in failed
        assert "target ladder (target_cpu_utilization)" in failed

    def test_detects_window_below_quantile_minimum(self, monkeypatch: pytest.MonkeyPatch):
        broken = _mutated_profiles(production={"window": "7d"})
        monkeypatch.setattr(sps, "SIZING_PROFILES", broken)
        assert sps.main(["--check"]) == 1

    def test_detects_profile_applying_a_savings_filter(self, monkeypatch: pytest.MonkeyPatch):
        broken = _mutated_profiles(development={"min_monthly_savings": 5.0})
        monkeypatch.setattr(sps, "SIZING_PROFILES", broken)
        assert sps.main(["--check"]) == 1

    def test_detects_zero_target_divisor(self, monkeypatch: pytest.MonkeyPatch):
        broken = _mutated_profiles(development={"target_cpu_utilization": 0.0})
        monkeypatch.setattr(sps, "SIZING_PROFILES", broken)
        assert sps.main(["--check"]) == 1


class TestJsonMode:
    def test_payload_covers_every_profile(self, capsys: pytest.CaptureFixture[str]):
        assert sps.main(["--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        for section in ("profiles", "descriptions", "derived", "api_params"):
            assert set(payload[section]) == set(PROFILE_NAMES), section

    def test_derived_matches_the_kubecost_formula(self, capsys: pytest.CaptureFixture[str]):
        """recommended = usage / target — the relationship the script exists to make visible."""
        assert sps.main(["--json", "--usage-cpu", "100", "--usage-ram", "512"]) == 0
        derived = json.loads(capsys.readouterr().out)["derived"]
        assert derived["high-availability"]["example_cpu_millicores"] == 200.0  # 100 / 0.50
        assert derived["high-availability"]["cpu_headroom_multiplier"] == 2.0
        assert derived["development"]["example_ram_mib"] == 640.0  # 512 / 0.80
        # Lower target must yield the larger request, on both axes.
        for axis in ("example_cpu_millicores", "example_ram_mib"):
            assert derived["high-availability"][axis] > derived["production"][axis] > derived["development"][axis]

    def test_api_params_use_kubecost_wire_names(self, capsys: pytest.CaptureFixture[str]):
        assert sps.main(["--json"]) == 0
        params = json.loads(capsys.readouterr().out)["api_params"]["high-availability"]
        assert params == {
            "algorithmCPU": "quantileOfAverages",
            "algorithmRAM": "quantileOfMaxes",
            "qCPU": 0.95,
            "qRAM": 0.99,
            "targetCPUUtilization": 0.50,
            "targetRAMUtilization": 0.50,
            "window": "30d",
        }

    def test_single_profile_filter(self, capsys: pytest.CaptureFixture[str]):
        assert sps.main(["--json", "--profile", "development"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert set(payload["profiles"]) == {"development"}


class TestRendering:
    def test_full_report_renders_every_section(self, capsys: pytest.CaptureFixture[str]):
        assert sps.main(["--no-color"]) == 0
        out = capsys.readouterr().out
        for heading in (
            "Sizing profile parameters",
            "What those targets imply",
            "Kubecost API parameters sent",
            "Invariants",
            "Where these profile names apply",
        ):
            assert heading in out, heading

    def test_rejects_non_positive_usage(self, capsys: pytest.CaptureFixture[str]):
        assert sps.main(["--usage-cpu", "0"]) == 2
        assert "greater than 0" in capsys.readouterr().err


def _mutated_profiles(**overrides: dict[str, Any]) -> dict[sps.ProfileName, dict[str, Any]]:
    """Deep-copy SIZING_PROFILES and apply per-profile key overrides."""
    profiles: dict[sps.ProfileName, dict[str, Any]] = {n: dict(v) for n, v in sps.SIZING_PROFILES.items()}
    for name, changes in overrides.items():
        # Keyword names are profile names; cast because **kwargs keys are plain str.
        profiles[cast(sps.ProfileName, name)].update(changes)
    return profiles
