#!/usr/bin/env python3
"""Generate the README tools and prompts tables from FastMCP list JSON.

Reads the JSON output of ``fastmcp list --prompts --json`` (stdin or --input),
extracts each tool/prompt name and the first line of its description, then
rewrites everything after ``## Tools`` up to ``## Quick Start`` in README.md.

Skills in this repo are registered as MCP prompts, so they appear in the
prompts table — there is no separate skills list from FastMCP.

Usage:

    uv run fastmcp list .bob/mcp.json --prompts --json 2>/dev/null \\
      | uv run scripts/generate_tools_readme.py

    uv run scripts/generate_tools_readme.py --input temp.json

    uv run scripts/generate_tools_readme.py --input temp.json --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

TOOLS_HEADING = "## Tools"
QUICK_START_HEADING = "## Quick Start"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate README tools and prompts tables from FastMCP list JSON.",
    )
    parser.add_argument(
        "--input",
        "-i",
        type=Path,
        help="Path to fastmcp list --json output (default: stdin)",
    )
    parser.add_argument(
        "--readme",
        type=Path,
        default=Path("README.md"),
        help="README to update (default: README.md)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the generated block without writing README",
    )
    return parser.parse_args()


def load_list_json(path: Path | None) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8") if path is not None else sys.stdin.read()
    if not raw.strip():
        raise SystemExit("No JSON input provided (empty stdin or file)")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON: {exc}") from exc
    if not isinstance(data, dict) or "tools" not in data:
        raise SystemExit("JSON must be an object with a 'tools' array")
    if not isinstance(data["tools"], list):
        raise SystemExit("'tools' must be an array")
    if "prompts" not in data or not isinstance(data["prompts"], list):
        raise SystemExit("JSON must include a 'prompts' array (use fastmcp list --prompts --json)")
    return data


def first_line_description(description: str) -> str:
    line = description.split("\n", 1)[0].strip()
    return line.replace("|", "\\|")


def build_named_table(
    items: list[dict[str, Any]],
    *,
    col_name: str,
    kind: str,
) -> list[str]:
    lines = [
        f"| {col_name} | Description |",
        f"|{'-' * (len(col_name) + 2)}|-------------|",
    ]
    for item in items:
        name = item.get("name")
        if not name:
            raise SystemExit(f"{kind} entry missing 'name'")
        desc = first_line_description(str(item.get("description") or ""))
        lines.append(f"| `{name}` | {desc} |")
    return lines


def build_section_block(tools: list[dict[str, Any]], prompts: list[dict[str, Any]]) -> str:
    lines = [
        f"**{len(tools)} tools** — all read-only, all structured for LLM consumption:",
        "",
        *build_named_table(tools, col_name="Tool", kind="Tool"),
        "",
        f"**{len(prompts)} prompts** — step-by-step workflows your assistant can follow:",
        "",
        *build_named_table(prompts, col_name="Prompt", kind="Prompt"),
        "",
    ]
    return "\n".join(lines)


def replace_tools_section(readme: str, section_block: str) -> str:
    if TOOLS_HEADING not in readme:
        raise SystemExit(f"README missing '{TOOLS_HEADING}' heading")
    if QUICK_START_HEADING not in readme:
        raise SystemExit(f"README missing '{QUICK_START_HEADING}' heading")

    pattern = re.compile(
        rf"({re.escape(TOOLS_HEADING)}\n)(.*?)(\n{re.escape(QUICK_START_HEADING)})",
        re.DOTALL,
    )
    replacement = rf"\1\n{section_block}\3"
    updated, n = pattern.subn(replacement, readme, count=1)
    if n != 1:
        raise SystemExit("Failed to locate section between ## Tools and ## Quick Start")
    return updated


def main() -> None:
    args = parse_args()
    data = load_list_json(args.input)
    section_block = build_section_block(data["tools"], data["prompts"])

    if args.dry_run:
        print(section_block, end="")
        return

    readme_path = args.readme
    if not readme_path.is_file():
        raise SystemExit(f"README not found: {readme_path}")

    original = readme_path.read_text(encoding="utf-8")
    updated = replace_tools_section(original, section_block)
    readme_path.write_text(updated, encoding="utf-8")
    print(
        f"Updated {readme_path} with {len(data['tools'])} tools and {len(data['prompts'])} prompts",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
