"""Tests for domain/kubecost/sizing_guidance.py."""

from __future__ import annotations

import pytest

import mcp_kubecost.domain.kubecost.sizing_guidance as sg

DEFAULT_SIZING_PARAMS = sg.DEFAULT_SIZING_PARAMS
SIZING_PRESETS = sg.SIZING_PRESETS
PresetName = sg.PresetName
build_result_interpretation = sg.build_result_interpretation
format_presets_resource = sg.format_presets_resource
resolve_sizing_params = sg.resolve_sizing_params

# ---------------------------------------------------------------------------
# resolve_sizing_params
# ---------------------------------------------------------------------------


class TestResolveSizingParams:
    def test_defaults_when_no_args(self):
        params = resolve_sizing_params()
        assert params["window"] == DEFAULT_SIZING_PARAMS["window"]
        assert params["q_cpu"] == DEFAULT_SIZING_PARAMS["q_cpu"]
        assert params["q_ram"] == DEFAULT_SIZING_PARAMS["q_ram"]
        assert "preset" not in params

    def test_preset_balanced_is_empty_override(self):
        params = resolve_sizing_params("balanced")
        # balanced applies no overrides — all values should equal defaults
        for key, val in DEFAULT_SIZING_PARAMS.items():
            assert params[key] == val, f"balanced preset changed {key}"
        assert params["preset"] == "balanced"

    def test_preset_conservative_raises_quantiles(self):
        params = resolve_sizing_params("conservative")
        assert params["q_cpu"] == SIZING_PRESETS["conservative"]["q_cpu"]
        assert params["q_ram"] == SIZING_PRESETS["conservative"]["q_ram"]
        assert params["window"] == "30d"
        assert params["include_undersized"] is True

    def test_preset_aggressive_lowers_target_utilization(self):
        params = resolve_sizing_params("aggressive")
        assert params["target_cpu_utilization"] == SIZING_PRESETS["aggressive"]["target_cpu_utilization"]
        assert params["min_monthly_savings"] == SIZING_PRESETS["aggressive"]["min_monthly_savings"]

    def test_explicit_override_beats_preset(self):
        params = resolve_sizing_params("conservative", q_cpu=0.5)
        assert params["q_cpu"] == 0.5  # explicit wins over preset's 0.95

    def test_false_and_zero_are_valid_overrides(self):
        """False and 0.0 must not be skipped (they're falsy but intentional)."""
        params = resolve_sizing_params(include_undersized=False, min_monthly_savings=0.0)
        assert params["include_undersized"] is False
        assert params["min_monthly_savings"] == 0.0

    def test_none_overrides_are_ignored(self):
        params = resolve_sizing_params(window=None, q_cpu=None)
        assert params["window"] == DEFAULT_SIZING_PARAMS["window"]
        assert params["q_cpu"] == DEFAULT_SIZING_PARAMS["q_cpu"]

    @pytest.mark.parametrize("preset", ["conservative", "balanced", "aggressive"])
    def test_all_presets_produce_required_keys(self, preset: PresetName):
        params = resolve_sizing_params(preset)
        required = {
            "window",
            "algorithm_cpu",
            "algorithm_ram",
            "q_cpu",
            "q_ram",
            "target_cpu_utilization",
            "target_ram_utilization",
            "include_undersized",
            "min_monthly_savings",
        }
        assert required.issubset(params.keys())


# ---------------------------------------------------------------------------
# build_result_interpretation
# ---------------------------------------------------------------------------


class TestBuildResultInterpretation:
    def _make_row(self, **kwargs) -> dict:
        defaults = {
            "containerName": "app",
            "monthlySavings_memory": 5.0,
            "AvgUsage_cpu": 100.0,
            "MaxUsage_cpu": 200.0,
            "currentEfficiency_cpu": 0.5,
            "currentEfficiency_memory": 0.5,
        }
        defaults.update(kwargs)
        return defaults

    def test_output_contains_header(self):
        params = resolve_sizing_params()
        row = self._make_row()
        result = build_result_interpretation(params, [row])
        assert "How to read these results" in result

    def test_undersized_memory_warning_appears(self):
        params = resolve_sizing_params()
        undersized = self._make_row(containerName="leaky-app", monthlySavings_memory=-10.0)
        result = build_result_interpretation(params, [undersized])
        assert "leaky-app" in result
        assert "under-provisioned" in result.lower()

    def test_cpu_spike_warning_appears(self):
        params = resolve_sizing_params()
        # Max/Avg > 3x threshold
        spikey = self._make_row(AvgUsage_cpu=10.0, MaxUsage_cpu=40.0)
        result = build_result_interpretation(params, [spikey])
        assert "spike" in result.lower() or "Max/Avg" in result

    def test_preset_label_in_output(self):
        params = resolve_sizing_params("conservative")
        row = self._make_row()
        result = build_result_interpretation(params, [row])
        assert "conservative" in result

    def test_heavily_overprovisioned_entry(self):
        params = resolve_sizing_params()
        over = self._make_row(
            currentEfficiency_cpu=0.1,  # < 0.2 threshold
            currentEfficiency_memory=0.1,  # < 0.3 threshold
            monthlySavings_memory=50.0,
        )
        result = build_result_interpretation(params, [over])
        assert "over-provisioned" in result.lower()


# ---------------------------------------------------------------------------
# format_presets_resource
# ---------------------------------------------------------------------------


class TestFormatPresetsResource:
    def test_all_preset_names_present(self):
        output = format_presets_resource()
        for name in ("conservative", "balanced", "aggressive"):
            assert name in output

    def test_contains_explicit_params_label(self):
        output = format_presets_resource()
        assert "Explicit parameters" in output
