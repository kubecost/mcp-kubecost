#!/usr/bin/env python3
"""Ensure files end with a newline."""

from __future__ import annotations

import sys


def check_file(filename: str) -> bool:
    """Check and fix file ending. Returns True if file was modified."""
    try:
        with open(filename, "rb") as f:
            contents = f.read()
    except Exception:
        return False

    if not contents:
        return False

    # Check if file ends with newline
    if not contents.endswith(b"\n"):
        with open(filename, "ab") as f:
            f.write(b"\n")
        return True

    return False


def main() -> int:
    """Main entry point."""
    filenames = sys.argv[1:]

    if not filenames:
        return 0

    fixed_files = []

    for filename in filenames:
        if check_file(filename):
            fixed_files.append(filename)

    if fixed_files:
        print(f"Fixed end-of-file in {len(fixed_files)} file(s):")
        for f in fixed_files:
            print(f"  - {f}")
        return 1  # Return 1 to indicate files were modified

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# Made with Bob
