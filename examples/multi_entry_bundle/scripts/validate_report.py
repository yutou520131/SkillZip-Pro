#!/usr/bin/env python3
"""Validate a release report against the bundle's output conventions.

A locked, non-text resource: bundle compression copies scripts byte-for-byte and
never rewrites them, so this file is identical before and after compression.
"""
from __future__ import annotations

import re
import sys

DURATION = re.compile(r"\b\d+\.\d s\b|\b\d+\.\d+s\b")


def validate(text: str) -> list:
    """Return a list of convention violations (empty means the report passes)."""
    problems = []
    if text.count("\n# ") + int(text.startswith("# ")) != 1:
        problems.append("report must have exactly one H1 title")
    if "## Summary" not in text:
        problems.append("missing '## Summary' section")
    if "## Sign-off" not in text:
        problems.append("missing '## Sign-off' section")
    if "<" in text and ">" in text:
        problems.append("raw HTML is not allowed")
    return problems


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: validate_report.py REPORT.md")
        return 2
    with open(sys.argv[1], encoding="utf-8") as handle:
        problems = validate(handle.read())
    for problem in problems:
        print(f"FAILED: {problem}")
    print("OK" if not problems else f"{len(problems)} problem(s)")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
