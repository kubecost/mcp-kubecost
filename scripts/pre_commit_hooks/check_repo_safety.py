"""Basic repository guardrails for secrets and large files."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

MAX_FILE_BYTES = 5 * 1024 * 1024
SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"-----BEGIN (?:RSA|EC|OPENSSH|PRIVATE) KEY-----"),
    re.compile(r"(?i)(api[_-]?key|token|secret)\s*[:=]\s*['\"][^'\"]{8,}['\"]"),
]
ALLOWED_SECRET_FILES = {".env.example"}

# Values that are clearly placeholders or shell variable references, not real secrets.
# A match whose captured value portion matches any of these is skipped.
_SAFE_VALUE_RE = re.compile(
    r"""
    ^\$\{?[A-Z_][A-Z0-9_]*\}?$   # shell variable: $VAR or ${VAR}
    | ^<[^>]+>$                    # angle-bracket placeholder: <your-secret>
    | ^\$\([^)]+\)$                # command substitution: $(cmd)
    | ^your[_-]                    # obvious example text: your_secret_here
    | ^REPLACE_ME$
    | ^xxx+$                       # redacted stand-in
    """,
    re.VERBOSE | re.IGNORECASE,
)


def _redact(value: str, max_len: int = 6) -> str:
    """Return a redacted snippet: first few chars then ****."""
    if len(value) <= max_len:
        return "****"
    return value[:max_len] + "****"


def _find_secret_hits(content: str) -> list[tuple[int, str]]:
    """
    Return (line_number, redacted_snippet) for every line that contains a
    pattern match that is not a known-safe placeholder.
    """
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(content.splitlines(), start=1):
        for pattern in SECRET_PATTERNS:
            m = pattern.search(line)
            if not m:
                continue
            matched_text = m.group(0)
            # For the key=value pattern, extract just the value portion to
            # check whether it looks like a placeholder.
            value_match = re.search(r"""['"]([^'"]{8,})['"]""", matched_text)
            if value_match:
                raw_value = value_match.group(1)
                if _SAFE_VALUE_RE.match(raw_value):
                    continue  # recognised placeholder — skip
                redacted = _redact(raw_value)
                hits.append((lineno, f"… {matched_text[: matched_text.index(raw_value)]}{redacted}…"))
            else:
                hits.append((lineno, _redact(matched_text)))
            break  # one hit per line is enough
    return hits


def tracked_files() -> list[Path]:
    output = subprocess.check_output(["git", "ls-files"], text=True)
    return [Path(line.strip()) for line in output.splitlines() if line.strip()]


def main() -> int:
    bad_size: list[str] = []
    bad_secret: list[tuple[str, list[tuple[int, str]]]] = []

    for file_path in tracked_files():
        if not file_path.exists() or file_path.is_dir():
            continue
        if file_path.stat().st_size > MAX_FILE_BYTES:
            bad_size.append(str(file_path))

        if file_path.name in ALLOWED_SECRET_FILES:
            continue
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        hits = _find_secret_hits(content)
        if hits:
            bad_secret.append((str(file_path), hits))

    if bad_size or bad_secret:
        if bad_size:
            print("Large files detected (>5MB):")
            for item in bad_size:
                print(f" - {item}")
        if bad_secret:
            print("Potential secrets detected:")
            for file_str, hits in bad_secret:
                for lineno, snippet in hits:
                    print(f" - {file_str}:{lineno}  {snippet}")
        return 1

    print("Repository safety checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
