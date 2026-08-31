#!/usr/bin/env python3
"""SkillZip command-line interface (paper Appendix B.1).

    skillzip compress SKILL.md --state skillzip.json --output SKILL.compact.md
    skillzip update  skillzip.json PATCH.md --output SKILL.md
    skillzip audit   SKILL.compact.md skillzip.json
    skillzip inspect skillzip.json --show-savings
    skillzip compress-bundle ./my-skill --output ./my-skill.compact

The CLI writes through temporary files and replaces persistent state only after
schema/coverage validation and rendering succeed (B.1). Compression never reads
tasks, rewards, or verifiers.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Make the package importable when this file is run as a plain script.
# (this module lives at <root>/skillzip/cli.py -> add <root> to sys.path)
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

from skillzip.skill import Skill                      # noqa: E402
from skillzip import (compress_oneshot, zip_on_write, DEFAULT_CFG,
                      compress_bundle)  # noqa: E402
from skillzip.contract import ZipState                # noqa: E402
from skillzip import online, render, extract, optimize, audit as auditmod  # noqa: E402


#: Default OpenAI-compatible endpoint for the optional model-assisted stages.
#: Override with the ``SKILLZIP_BASE_URL`` environment variable; credentials come
#: from ``DASHSCOPE_API_KEY`` and are never stored in the repository.
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


def _client(args):
    """Build a model client, or return None to stay fully deterministic."""
    if args.no_llm:
        return None
    try:
        from skillzip.llm import LLMClient
        key = os.environ.get("DASHSCOPE_API_KEY", "")
        if not key:
            return None
        return LLMClient(model=args.model, backend="real",
                         base_url=os.environ.get("SKILLZIP_BASE_URL", DEFAULT_BASE_URL),
                         cache_dir=args.cache, timeout_s=90, enable_thinking=False)
    except Exception:
        return None


def _atomic_write(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def cmd_compress(args):
    skill = Skill.load(args.skill, os.path.splitext(os.path.basename(args.skill))[0])
    cli = _client(args)
    comp, report = compress_oneshot(skill, cli=cli)
    _atomic_write(args.output, comp.to_markdown())
    if args.state:
        contract = extract.extract_contract(comp.to_markdown(), cli=cli)
        lib = optimize.min_cost_cover(contract, cli=cli)
        ZipState(name=skill.name, library=lib).save(args.state)
    print(json.dumps({k: v for k, v in report.items() if k != "savings"}, indent=2))


def cmd_update(args):
    cli = _client(args)
    state = ZipState.load(args.state)
    patch_text = open(args.patch, encoding="utf-8").read()
    new_state, body = online.zip_update(state, patch_text, cli=cli)
    _atomic_write(args.output, Skill(name=state.name, body=body).to_markdown())
    new_state.save(args.state)                          # atomic commit last
    print(json.dumps({"name": state.name,
                      "units": len(new_state.library.units),
                      "residual": len(new_state.library.residual),
                      "compressed_tokens": Skill(name=state.name, body=body).tokens},
                     indent=2))


def cmd_audit(args):
    cli = _client(args)
    body = Skill.load(args.skill, "skill").body
    state = ZipState.load(args.state)
    from skillzip.contract import Contract
    source = Contract(units=state.library.all_units())
    _, restored = auditmod.audit_and_restore(body, state.library, source, cli=cli)
    print(json.dumps({"missing_restored": restored,
                      "ok": len(restored) == 0}, indent=2))


def cmd_inspect(args):
    state = ZipState.load(args.state)
    print(f"skill: {state.name}")
    print(f"units: {len(state.library.units)}  residual: {len(state.library.residual)}"
          f"  procedures: {len(state.library.procedures)}")
    if args.show_savings:
        for entry in state.log:
            print(json.dumps(entry))


def cmd_compress_bundle(args):
    cli = _client(args)
    _, report = compress_bundle(
        args.bundle,
        args.output,
        cli=cli,
        environment_contract=args.environment_contract or None,
        overwrite=args.overwrite,
        cfg={
            "capsules": not args.no_capsules,
            "promote_cross_file": not args.no_promote,
            "extract_llm": not args.no_llm,
            "audit_llm": not args.no_llm,
        },
    )
    report_path = args.report or (str(args.output).rstrip(os.sep) + ".phase-a.json")
    _atomic_write(report_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    summary = {
        "output": report["output"],
        "report": os.path.abspath(report_path),
        "selected_verbatim_bundle_baseline": report["selected_verbatim_bundle_baseline"],
        "ratios": report["ratios"],
        "audit_ok": report["audit"]["ok"],
        "capsules": len(report["capsules"]),
        "promotions": len(report["promotions"]),
        "environment_drops": len(report["environment_drops"]),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def cmd_entry_contracts(args):
    """Print the entry contract derived for every markdown resource."""
    from skillzip.lifecycle import entry_contracts, write_entry_manifest
    labels = entry_contracts(args.bundle)
    if args.write:
        print(f"wrote {write_entry_manifest(args.bundle)}")
    print(json.dumps(labels, indent=2, sort_keys=True))


def cmd_compress_persistent(args):
    """Persistent lifecycle: publish a smaller bundle in place of the original."""
    from skillzip.lifecycle import audit_public_entries, compress_persistent
    report = compress_persistent(
        args.bundle, args.output,
        environment_contract=args.environment_contract or None,
        audit_entries=not args.no_entry_audit,
        cli=_client(args),
    )
    verdict = audit_public_entries(args.bundle, args.output,
                                   args.environment_contract or None)
    report["public_entry_audit"] = {
        "ok": verdict["ok"],
        "failures": verdict["failures"],
        "worst_public_effective": verdict["worst_public_effective"],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not verdict["ok"]:
        raise SystemExit(1)   # a lost public entry is a deployment blocker


def cmd_build_view(args):
    """Transient lifecycle: build one per-run view; the bundle is untouched."""
    from skillzip.lifecycle import build_execution_view
    meta = build_execution_view(
        args.bundle, args.entry,
        environment_contract=args.environment_contract or None,
        cache=not args.no_cache,
        cache_root=args.cache_root or None,
        cli=_client(args),
    )
    print(json.dumps(meta, ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser(prog="skillzip")
    ap.add_argument("--model", default="qwen3.7-max")
    ap.add_argument("--cache", default=".skillzip_cache")
    ap.add_argument("--no-llm", action="store_true",
                    help="use the deterministic parser/relation checker only")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("compress"); c.add_argument("skill")
    c.add_argument("--state", default=""); c.add_argument("--output", required=True)
    c.set_defaults(fn=cmd_compress)

    u = sub.add_parser("update"); u.add_argument("state"); u.add_argument("patch")
    u.add_argument("--output", required=True); u.set_defaults(fn=cmd_update)

    a = sub.add_parser("audit"); a.add_argument("skill"); a.add_argument("state")
    a.set_defaults(fn=cmd_audit)

    i = sub.add_parser("inspect"); i.add_argument("state")
    i.add_argument("--show-savings", action="store_true"); i.set_defaults(fn=cmd_inspect)

    b = sub.add_parser("compress-bundle")
    b.add_argument("bundle", help="bundle directory or its root SKILL.md")
    b.add_argument("--output", required=True, help="new bundle directory")
    b.add_argument("--report", default="", help="Phase-A JSON report path")
    b.add_argument("--environment-contract", default="",
                   help="optional strict host/tool guarantee JSON")
    b.add_argument("--no-capsules", action="store_true")
    b.add_argument("--no-promote", action="store_true")
    b.add_argument("--overwrite", action="store_true")
    b.set_defaults(fn=cmd_compress_bundle)

    # ---- lifecycle: where the compression result goes -------------------------
    e = sub.add_parser("entry-contracts",
                       help="show which resources are public/conditional/private")
    e.add_argument("bundle")
    e.add_argument("--write", action="store_true",
                   help="freeze the labels into entrypoints.json")
    e.set_defaults(fn=cmd_entry_contracts)

    p = sub.add_parser("compress-persistent",
                       help="publish a smaller bundle, keeping public entries callable")
    p.add_argument("bundle")
    p.add_argument("--output", required=True, help="new bundle directory")
    p.add_argument("--environment-contract", default="")
    p.add_argument("--no-entry-audit", action="store_true",
                   help="disable the multi-entry audit (ablation; may break a public entry)")
    p.set_defaults(fn=cmd_compress_persistent)

    v = sub.add_parser("build-view",
                       help="build a transient per-run execution view for one entry")
    v.add_argument("bundle")
    v.add_argument("entry", help="bundle-relative entry, e.g. sub/SKILL.md")
    v.add_argument("--environment-contract", default="")
    v.add_argument("--cache-root", default="", help="where to cache views")
    v.add_argument("--no-cache", action="store_true")
    v.set_defaults(fn=cmd_build_view)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
