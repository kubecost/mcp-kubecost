#!/usr/bin/env python3
"""Check for large files being added to the repository."""

from __future__ import annotations

import os
import sys


def check_file(filename: str, max_kb: int) -> tuple[bool, int]:
    """Check file size. Returns (is_too_large, size_kb)."""
    try:
        size_bytes = os.path.getsize(filename)
        size_kb = size_bytes // 1024
        return size_kb > max_kb, size_kb
    except Exception:
        return False, 0


def main() -> int:
    """Main entry point."""
    args = sys.argv[1:]

    # Parse --maxkb argument
    max_kb = 500  # default
    filenames = []

    for i, arg in enumerate(args):
        if arg.startswith("--maxkb="):
            max_kb = int(arg.split("=")[1])
        elif arg.startswith("--maxkb"):
            # Next arg is the value
            if i + 1 < len(args):
                max_kb = int(args[i + 1])
        elif not arg.isdigit():  # Skip the maxkb value itself
            filenames.append(arg)

    if not filenames:
        return 0

    large_files = []

    for filename in filenames:
        is_large, size_kb = check_file(filename, max_kb)
        if is_large:
            large_files.append((filename, size_kb))

    if large_files:
        print(f"❌ Large files detected (max: {max_kb}KB):")
        for filename, size_kb in large_files:
            print(f"   {filename}: {size_kb}KB")
        print("\nConsider:")
        print("  - Using Git LFS for large binary files")
        print("  - Compressing the file")
        print("  - Storing it externally")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# Made with Bob
