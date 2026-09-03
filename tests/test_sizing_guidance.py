"""Tests for domain/kubecost/sizing_guidance.py."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import mcp_kubecost.domain.kubecost.sizing_guidance as sg

DEFAULT_SIZING_PARAMS = sg.DEFAULT_SIZING_PARAMS
PROFILE_DESCRIPTIONS = sg.PROFILE_DESCRIPTIONS
SIZING_PROFILES = sg.SIZING_PROFILES
ProfileName = sg.ProfileName
build_result_interpretation = sg.build_result_interpretation
format_profiles_resource = sg.format_profiles_resource
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
        assert params["min_monthly_savings"] is None
        assert "profile" not in params

    def test_profile_production_is_empty_override(self):
        params = resolve_sizing_params("production")
        for key, val in DEFAULT_SIZING_PARAMS.items():
            assert params[key] == val, f"production profile changed {key}"
        assert params["profile"] == "production"

    def test_profile_high_availability_raises_quantiles_and_headroom(self):
        params = resolve_sizing_params("high-availability")
        assert params["q_cpu"] == 0.95
        assert params["q_ram"] == 0.99
        assert params["window"] == "30d"
        assert params["target_cpu_utilization"] == 0.50
        assert params["target_ram_utilization"] == 0.50

    def test_profile_development_raises_target_utilization(self):
        params = resolve_sizing_params("development")
        assert params["target_cpu_utilization"] == 0.80
        assert params["target_ram_utilization"] == 0.80

    @pytest.mark.parametrize("profile", [None, "high-availability", "production", "development"])
    def test_min_monthly_savings_is_null_for_every_profile(self, profile: ProfileName | None):
        params = resolve_sizing_params(profile)
        assert params["min_monthly_savings"] is None

    def test_target_utilization_orders_ha_below_production_below_development(self):
        ha = resolve_sizing_params("high-availability")
        prod = resolve_sizing_params("production")
        dev = resolve_sizing_params("development")
        assert ha["target_cpu_utilization"] < prod["target_cpu_utilization"] < dev["target_cpu_utilization"]
        assert ha["target_ram_utilization"] < prod["target_ram_utilization"] < dev["target_ram_utilization"]

    def test_higher_target_utilization_produces_smaller_recommended_request(self):
        """Kubecost formula: recommended = usage / targetUtilization."""
        usage = 1000.0
        ha = usage / resolve_sizing_params("high-availability")["target_cpu_utilization"]
        prod = usage / resolve_sizing_params("production")["target_cpu_utilization"]
        dev = usage / resolve_sizing_params("development")["target_cpu_utilization"]
        assert ha > prod > dev

    def test_explicit_override_beats_profile(self):
        params = resolve_sizing_params("high-availability", q_cpu=0.5)
        assert params["q_cpu"] == 0.5  # explicit wins over profile's 0.95

    def test_zero_min_monthly_savings_is_valid_override(self):
        """0.0 must not be skipped (falsy but intentional)."""
        params = resolve_sizing_params(min_monthly_savings=0.0)
        assert params["min_monthly_savings"] == 0.0

    def test_none_overrides_are_ignored(self):
        params = resolve_sizing_params(window=None, q_cpu=None, min_monthly_savings=None)
        assert params["window"] == DEFAULT_SIZING_PARAMS["window"]
        assert params["q_cpu"] == DEFAULT_SIZING_PARAMS["q_cpu"]
        assert params["min_monthly_savings"] is None

    @pytest.mark.parametrize("profile", ["high-availability", "production", "development"])
    def test_all_profiles_produce_required_keys(self, profile: ProfileName):
        params = resolve_sizing_params(profile)
        required = {
            "window",
            "algorithm_cpu",
            "algorithm_ram",
            "q_cpu",
            "q_ram",
            "target_cpu_utilization",
            "target_ram_utilization",
            "min_monthly_savings",
        }
        assert required.issubset(params.keys())
        assert "include_undersized" not in params

    @pytest.mark.parametrize("profile", ["high-availability", "production", "development"])
    def test_profile_pins_every_default_key(self, profile: ProfileName):
        """Profiles spell out the full parameter set so each is readable in isolation."""
        assert set(SIZING_PROFILES[profile]) == set(DEFAULT_SIZING_PARAMS)


class TestProfileDescriptions:
    @pytest.mark.parametrize("profile", ["high-availability", "production", "development"])
    def test_description_reports_the_profiles_actual_values(self, profile: ProfileName):
        """Descriptions are generated from SIZING_PROFILES — they cannot drift from it."""
        values = SIZING_PROFILES[profile]
        description = PROFILE_DESCRIPTIONS[profile]
        assert f"P{round(values['q_cpu'] * 100)} CPU" in description
        assert f"P{round(values['q_ram'] * 100)} RAM" in description
        assert f"over {values['window']}" in description
        assert f"{values['target_cpu_utilization']:.2f}" in description

    def test_ram_target_never_exceeds_cpu_target(self):
        """Memory is not compressible — no profile may squeeze RAM harder than CPU."""
        for profile, values in SIZING_PROFILES.items():
            assert values["target_ram_utilization"] <= values["target_cpu_utilization"], (
                f"{profile} sizes RAM more aggressively than CPU"
            )


# ---------------------------------------------------------------------------
# build_result_interpretation
# ---------------------------------------------------------------------------


class TestBuildResultInterpretation:
    def _make_row(self, **kwargs) -> dict:
        defaults = {
            "containerName": "app",
            "monthlySavings_memory": 5.0,
            "AvgUsage_cpuInMilliCores": 100.0,
            "MaxUsage_cpuInMilliCores": 200.0,
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
        assert "min_monthly_savings" in result

    def test_cpu_spike_warning_appears(self):
        params = resolve_sizing_params()
        # Max/Avg > 3x threshold
        spikey = self._make_row(AvgUsage_cpuInMilliCores=10.0, MaxUsage_cpuInMilliCores=40.0)
        result = build_result_interpretation(params, [spikey])
        assert "spike" in result.lower() or "Max/Avg" in result

    def test_profile_label_in_output(self):
        params = resolve_sizing_params("high-availability")
        row = self._make_row()
        result = build_result_interpretation(params, [row])
        assert "high-availability" in result

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
# format_profiles_resource
# ---------------------------------------------------------------------------


class TestFormatProfilesResource:
    def test_all_profile_names_present(self):
        output = format_profiles_resource()
        for name in ("high-availability", "production", "development"):
            assert name in output

    def test_contains_explicit_params_label(self):
        output = format_profiles_resource()
        assert "Explicit parameters" in output


# ---------------------------------------------------------------------------
# README table
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]

# `| `name` | best for | 15d | P80 CPU / P95 RAM | 0.65 / 0.65 |`
README_ROW_RE = re.compile(
    r"^\|\s*`(?P<name>[a-z-]+)`[^|]*\|[^|]*\|"
    r"\s*(?P<window>\S+)\s*\|"
    r"\s*P(?P<q_cpu>\d+)\s+CPU\s*/\s*P(?P<q_ram>\d+)\s+RAM\s*\|"
    r"\s*(?P<target_cpu>[\d.]+)\s*/\s*(?P<target_ram>[\d.]+)\s*\|",
    re.MULTILINE,
)


def _readme_profile_rows() -> list[re.Match[str]]:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("### Container sizing profiles", 1)[1]
    return list(README_ROW_RE.finditer(section.split("\n##", 1)[0]))


class TestReadmeProfileTable:
    """The README table is the one hand-maintained copy of the profile values.

    Everything else (tool descriptions, the profiles resource, the explore menu) is
    generated from SIZING_PROFILES, so this is the only place that can drift.
    """

    def test_rows_match_shipped_profiles(self):
        rows = _readme_profile_rows()
        documented = {
            row["name"]: {
                "window": row["window"],
                "q_cpu": int(row["q_cpu"]) / 100,
                "q_ram": int(row["q_ram"]) / 100,
                "target_cpu_utilization": float(row["target_cpu"]),
                "target_ram_utilization": float(row["target_ram"]),
            }
            for row in rows
        }
        assert documented.keys() == set(SIZING_PROFILES)
        for name, values in documented.items():
            for key, val in values.items():
                assert val == SIZING_PROFILES[name][key], f"README {name}.{key} does not match SIZING_PROFILES"

    def test_row_order_matches_menu_order(self):
        """README order must match the menu users actually see in the explore prompt."""
        assert [row["name"] for row in _readme_profile_rows()] == list(PROFILE_DESCRIPTIONS)
