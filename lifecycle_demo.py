#!/usr/bin/env python3
"""Demonstrate the two compression lifecycles on a multi-entry skill bundle.

Run it with no arguments to use the bundled example:

    python lifecycle_demo.py
    python lifecycle_demo.py path/to/my-bundle

The demo needs no third-party packages and makes no model calls, so the numbers
below are reproducible byte-for-byte on any machine.

It prints four things:

1. The **entry contract** derived for every markdown resource.
2. **Persistent, audited** (the recommended default): a smaller bundle on disk
   that still keeps every public entry independently callable.
3. **Persistent, audit disabled** (an ablation): the same kernel without the
   multi-entry audit. Watch the public subskill become undiscoverable -- its text
   survives, but a direct call can no longer start it.
4. **Transient**: the canonical bundle is left byte-identical while a per-run
   execution view carries the context reduction, with a digest-keyed cache.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from skillzip import lifecycle as lc  # noqa: E402  (after sys.path setup)

DEFAULT_BUNDLE = Path(__file__).resolve().parent / "examples" / "multi_entry_bundle"
RULE = "=" * 66


def _fmt(value, width=10):
    return str(value).rjust(width)


def show_contracts(bundle: Path) -> None:
    print(RULE)
    print("1. ENTRY CONTRACTS  (who may call this file directly?)")
    print(RULE)
    for rel, contract in sorted(lc.entry_contracts(str(bundle)).items()):
        note = {
            lc.PUBLIC: "must work alone -> nothing it needs may be deleted",
            lc.CONDITIONAL: "works alone under a declared host context",
            lc.PRIVATE: "only reached via the root -> root-covered text is free",
        }[contract]
        print(f"  {contract:12s} {rel:34s} {note}")
    print(f"\n  on-disk bytes: {lc.bundle_bytes(str(bundle)):,}")
    print(f"  bundle digest: {lc.bundle_digest(str(bundle))}")


def show_persistent(bundle: Path, out: Path, audit: bool, heading: str) -> None:
    print("\n" + RULE)
    print(heading)
    print(RULE)
    report = lc.compress_persistent(str(bundle), str(out), audit_entries=audit)
    ratio = report["ratios"].get("deployment_text_remaining_ratio", 1.0)

    print(f"  deployment text remaining : {_fmt(f'{ratio:.4f}')}   "
          f"(1.0 = unchanged, lower = smaller)")
    print(f"  on-disk bytes             : {_fmt(f'{lc.bundle_bytes(str(out)):,}')}")
    print(f"  cross-file promotions     : {_fmt(report['promotions'])}")
    print(f"  routing audit passed      : {_fmt(str(report['audit_ok']))}")
    if audit:
        print(f"  entry names restored      : {_fmt(report['entries_renamed_back'])}")
        print(f"  statements restored       : {_fmt(report['units_restored'])}")

    verdict = lc.audit_public_entries(str(bundle), str(out))
    print(f"\n  public-entry audit        : {'PASS' if verdict['ok'] else 'FAIL'}")
    print(f"  worst public independence : {_fmt(verdict['worst_public_effective'])}")
    for row in verdict["entries"]:
        if row["contract"] != lc.PUBLIC:
            continue
        flag = "ok " if row["effective"] >= 1.0 else "LOST"
        print(f"    [{flag}] {row['entry']:22s} discoverable={str(row['discoverable']):5s} "
              f"content={row['independence']:<6} effective={row['effective']}")
    if not verdict["ok"]:
        print("\n  ^ The text was not deleted -- the entry file was renamed, so the")
        print("    host's discovery scan can no longer find it. Effective")
        print("    independence is 0: the call cannot start. This is exactly what")
        print("    the multi-entry audit prevents.")


def show_transient(bundle: Path, entry: str) -> None:
    print("\n" + RULE)
    print("4. TRANSIENT EXECUTION VIEW  (bundle untouched, run gets smaller)")
    print(RULE)
    digest_before = lc.bundle_digest(str(bundle))

    cold = lc.build_execution_view(str(bundle), entry)
    warm = lc.build_execution_view(str(bundle), entry)

    saving = 0.0
    if cold["raw_closure_tokens"]:
        saving = 100.0 * (1 - cold["view_tokens"] / cold["raw_closure_tokens"])

    print(f"  entry                     : {entry}")
    print(f"  raw closure tokens        : {_fmt(cold['raw_closure_tokens'])}")
    print(f"  view tokens               : {_fmt(cold['view_tokens'])}")
    print(f"  per-run saving            : {_fmt(f'{saving:.1f}%')}")
    print(f"  safe fallback used        : {_fmt(str(cold['fallback']))}")
    cold_ms = "{:.2f} ms".format(cold["build_ms"])
    warm_ms = "{:.2f} ms".format(warm["build_ms"])
    print(f"  cold build                : {_fmt(cold_ms)}")
    print(f"  warm cache read           : {_fmt(warm_ms)}")
    print(f"  cache hit on second call  : {_fmt(str(warm['cache_hit']))}")
    print(f"  canonical bundle untouched: {_fmt(str(cold['canonical_unchanged']))}")
    print(f"  digest before / after     : {digest_before} / {lc.bundle_digest(str(bundle))}")
    print("\n  Disk size does not change in this mode by construction, so the")
    print("  saving above is a per-run context number and must be read net of")
    print("  the build cost shown alongside it.")


def main() -> int:
    bundle = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_BUNDLE
    if not (bundle / "SKILL.md").is_file():
        print(f"error: {bundle} does not contain a SKILL.md")
        return 2

    workspace = Path(tempfile.mkdtemp(prefix="skillzip_pro_demo_"))
    os.environ.setdefault("SKILLZIP_VIEW_CACHE", str(workspace / "view_cache"))
    try:
        print(f"\nbundle: {bundle.name}\n")
        show_contracts(bundle)
        show_persistent(bundle, workspace / "audited", True,
                        "2. PERSISTENT, AUDITED  (recommended default)")
        show_persistent(bundle, workspace / "unaudited", False,
                        "3. PERSISTENT, AUDIT DISABLED  (ablation)")

        public = lc.public_entrypoints(str(bundle))
        entry = next((p for p in public if p != "SKILL.md"), "SKILL.md")
        show_transient(bundle, entry)

        print("\n" + RULE)
        print("The canonical bundle was never modified by this demo.")
        print(RULE + "\n")
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
