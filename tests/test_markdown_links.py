"""Validate repository-local Markdown links without making network requests."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LINK_RE = re.compile(r"!?\[[^\]]*\]\(\s*<?([^\s)>]+)>?(?:\s+[^)]*)?\)")
HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
EXPLICIT_ANCHOR_RE = re.compile(r"<(?:a\s+(?:name|id)|[^>]+\s+id)=[\"']([^\"']+)[\"']", re.IGNORECASE)


def _tracked_markdown_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.md"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    return [REPO_ROOT / path.decode() for path in result.stdout.split(b"\0") if path]


def _without_fenced_code(text: str) -> str:
    lines: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        marker = line.lstrip()[:3]
        if marker in {"```", "~~~"}:
            fence = None if fence == marker else marker if fence is None else fence
            continue
        if fence is None:
            lines.append(line)
    return "\n".join(lines)


def _github_slug(heading: str) -> str:
    heading = re.sub(r"<[^>]+>", "", heading).strip().lower()
    heading = re.sub(r"[^\w\s-]", "", heading)
    return re.sub(r"\s", "-", heading)


def _anchors(path: Path) -> set[str]:
    text = _without_fenced_code(path.read_text(encoding="utf-8"))
    anchors = set(EXPLICIT_ANCHOR_RE.findall(text))
    occurrences: dict[str, int] = {}
    for line in text.splitlines():
        match = HEADING_RE.match(line)
        if not match:
            continue
        base = _github_slug(match.group(1))
        count = occurrences.get(base, 0)
        occurrences[base] = count + 1
        anchors.add(base if count == 0 else f"{base}-{count}")
    return anchors


@pytest.mark.parametrize("source", _tracked_markdown_files(), ids=lambda path: str(path.relative_to(REPO_ROOT)))
def test_local_markdown_targets_and_anchors(source: Path):
    failures: list[str] = []
    text = _without_fenced_code(source.read_text(encoding="utf-8"))
    for raw_target in LINK_RE.findall(text):
        parsed = urlsplit(raw_target)
        if parsed.scheme or parsed.netloc:
            continue
        relative_path = unquote(parsed.path)
        target = source if not relative_path else (source.parent / relative_path).resolve()
        if not target.exists():
            failures.append(f"missing target {raw_target!r}")
            continue
        if parsed.fragment and target.is_file() and target.suffix.lower() == ".md":
            fragment = unquote(parsed.fragment)
            if fragment not in _anchors(target):
                failures.append(f"missing anchor #{fragment} in {target.relative_to(REPO_ROOT)}")
    assert not failures, "\n".join(failures)
