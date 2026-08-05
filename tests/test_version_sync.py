"""manifest.json version must track pyproject.toml (release.yaml syncs them)."""

import json
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_manifest_version_matches_pyproject():
    manifest_path = REPO_ROOT / "manifest.json"
    if not manifest_path.exists():
        return

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    manifest = json.loads(manifest_path.read_text())

    assert manifest["version"] == pyproject["project"]["version"], (
        "manifest.json version is out of sync with pyproject.toml"
    )
