#!/usr/bin/env python3
"""Minimal, dependency-free demo of SkillZip one-shot compression.

Runs the fully deterministic path (no LLM required) on a skill markdown file and
prints the compression report. This shows SkillZip's core property: compression
is *evaluation-free* — it never reads tasks, rewards, or verifiers, only the
skill text itself.

Usage:
    python compress_demo.py                       # compress examples/sample_skill.md
    python compress_demo.py path/to/SKILL.md      # compress your own skill
    python compress_demo.py path/to/SKILL.md out.md   # also write compressed skill

To enable the model-assisted stages (contract extraction / relation checking /
audit), set DASHSCOPE_API_KEY and use the CLI instead:
    python -m skillzip.cli compress SKILL.md --output SKILL.compact.md
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from skillzip import compress_oneshot          # noqa: E402
from skillzip.skill import Skill                # noqa: E402


def main() -> None:
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "examples", "sample_skill.md")
    out = sys.argv[2] if len(sys.argv) > 2 else ""

    skill = Skill.load(src, os.path.splitext(os.path.basename(src))[0])
    # cli=None -> deterministic parser + relation checker only (no network).
    compressed, report = compress_oneshot(skill, cli=None)

    print("=" * 60)
    print(f"skill               : {skill.name}")
    print(f"original tokens     : {report['original_tokens']}")
    print(f"compressed tokens   : {report['compressed_tokens']}")
    print(f"remaining ratio     : {report['remaining_ratio']}  "
          f"(1.0 = unchanged, lower = more compression)")
    print(f"extracted units     : {report['extracted_units']}")
    print(f"library units       : {report['library_units']}  "
          f"procedures={report['procedures']}  residual={report['residual_units']}")
    print(f"verbatim fallback   : {report['selected_verbatim_baseline']}")
    print("=" * 60)
    if out:
        Skill(name=skill.name, body=compressed.body).save(out)
        print(f"wrote compressed skill -> {out}")
    else:
        print("\n--- COMPRESSED SKILL ---\n")
        print(compressed.to_markdown())


if __name__ == "__main__":
    main()
