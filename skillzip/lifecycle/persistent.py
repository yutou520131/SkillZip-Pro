"""Persistent bundle compression: rewrite and republish the bundle on disk.

Persistent mode runs the compression kernel on the canonical bundle and ships
the smaller bundle *in its place*.  Because the shipped files change, this is the
mode that lowers both stored bytes and the context of every future run -- and the
mode that can silently break a directly callable entry.

Two failure modes matter, and they are different:

* **Content loss.** A statement is deleted from an entry because the *root*
  already says it.  Fine for a ``private`` file (the root is always loaded first);
  fatal for a ``public`` one (a direct call never loads the root).
* **Discoverability loss.** The entry file is renamed (typically
  ``sub/SKILL.md`` -> ``sub/SUBSKILL.md``).  The bundle stays internally
  consistent and every byte of the text survives, yet the host's discovery scan
  can no longer find the entry, so the call cannot start at all.

The **multi-entry audit** in :func:`compress_persistent` closes both.  After the
kernel publishes a candidate it re-reads the published directory from *every*
declared entry, restores verbatim anything a direct call would need, and undoes
any rename of a public entry.  It is on by default; turning it off reproduces the
unaudited ablation reported in the paper.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Dict, List, Optional

from ..bundle_api import compress_bundle
from ._text import is_present, norm, read_markdown_text, units_of_file
from .contracts import (CONDITIONAL, PRIVATE, PUBLIC, entry_contracts,
                        independence, reachable_from, resolve_entry)

#: Deterministic defaults: no model calls, so a publish is byte-reproducible.
DETERMINISTIC_CFG = {"extract_llm": False, "audit_llm": False}

#: Heading used for restored statements, so a reviewer can see the repair.
RESTORE_HEADING = "## Requirements for direct use"


def _run_kernel(source: str, output: str, env: Optional[str],
                cfg_overrides: Optional[dict], cli) -> Dict:
    """Invoke the shared compression kernel (Pillars 1-2) in-process.

    Both lifecycles funnel through here, which is what makes them *the same
    compressor with different output boundaries* rather than two algorithms.
    """
    out_path = Path(output)
    if out_path.exists():
        shutil.rmtree(out_path)

    cfg = dict(DETERMINISTIC_CFG)
    if cli is not None:
        # A client was supplied, so restore the paper's default model-assisted
        # extraction and audit.  The kernel still reads only skill text: it never
        # runs a task or looks at a score, so the method stays evaluation-free.
        cfg.update({"extract_llm": True, "audit_llm": True})
    cfg.update(cfg_overrides or {})

    _, report = compress_bundle(source, output, cli=cli, cfg=cfg,
                                environment_contract=env, overwrite=True)
    return {
        "ratios": report.get("ratios", {}),
        "audit_ok": bool(report.get("audit", {}).get("ok", False)),
        "selected_verbatim": report.get("selected_verbatim_bundle_baseline", False),
        "capsules": len(report.get("capsules", []) or []),
        "promotions": len(report.get("promotions", []) or []),
        "environment_drops": len(report.get("environment_drops", []) or []),
        "kernel_calls": report.get("model_calls", 0),
    }


def _restore_units(target: Path, missing: List[str]) -> int:
    """Append missing statements to an entry document, verbatim.

    Restoration is append-only and never re-words what it puts back, and it goes
    into one clearly labelled block so a reviewer can see exactly what
    independence cost.  Returns how many statements were restored.
    """
    if not missing:
        return 0
    body = target.read_text(encoding="utf-8").rstrip("\n")
    block = ["", "", RESTORE_HEADING,
             "<!-- restored by standalone-preserving compression: these lines are",
             "     reachable from the root, but a direct call never loads the root -->"]
    block += [f"- {unit}" for unit in missing]
    target.write_text(body + "\n".join(block) + "\n", encoding="utf-8")
    return len(missing)


def _restore_entry_name(published: Path, entry: str) -> bool:
    """Undo a ``SKILL.md`` -> ``SUBSKILL.md`` rename and repair inbound links.

    A public entry is only public while the host's discovery scan can find it,
    and that scan looks for ``SKILL.md``.  Returns ``True`` when a rename was
    undone.
    """
    wanted = published / entry
    if wanted.is_file():
        return False
    alt_rel = re.sub(r"SKILL\.md$", "SUBSKILL.md", entry)
    alt = published / alt_rel
    if not alt.is_file():
        return False

    shutil.move(str(alt), str(wanted))
    old_name, new_name = Path(alt_rel).name, Path(entry).name
    for markdown in published.rglob("*.md"):
        text = markdown.read_text(encoding="utf-8")
        if old_name in text:
            markdown.write_text(text.replace(old_name, new_name), encoding="utf-8")
    return True


def _missing_for_entry(source: str, published: str, entry: str,
                       env_contract: Optional[str]) -> List[str]:
    """Statements the source entry owns that a direct call can no longer see."""
    published_entry = resolve_entry(published, entry)
    if published_entry is None:
        return []
    source_units = units_of_file(Path(source).resolve() / entry)
    closure = reachable_from(published, published_entry)
    haystack = read_markdown_text(Path(published).resolve(), closure)

    missing = [u for u in source_units if not is_present(norm(u), haystack)]
    if env_contract:
        # A conditional entry may rely on its declared host context, so a
        # statement the contract guarantees is not restored.
        from ._text import entailed_units
        guaranteed = entailed_units(env_contract)
        missing = [u for u in missing
                   if not any(is_present(norm(u), g) for g in guaranteed)]
    return missing


def compress_persistent(source: str, output: str,
                        environment_contract: Optional[str] = None,
                        audit_entries: bool = True,
                        cli=None,
                        cfg: Optional[dict] = None) -> Dict:
    """Compress ``source`` and publish the smaller bundle to ``output``.

    Args:
        source: Canonical bundle directory (never modified).
        output: Directory to publish; replaced if it exists.
        environment_contract: Optional environment contract JSON.  Statements it
            guarantees may stay removed from ``conditional`` entries.
        audit_entries: Run the multi-entry audit (default, recommended).  When
            ``False``, entry contracts are ignored entirely -- this reproduces the
            paper's unaudited ablation and *can* leave a public entry uncallable.
        cli: Optional OpenAI-compatible client to enable the model-assisted
            extraction/audit stages.  ``None`` keeps the run fully deterministic.
        cfg: Extra kernel config overrides.

    Returns:
        A report with the kernel's ratios plus, when auditing, how many entries
        were renamed back and how many statements were restored.
    """
    report = _run_kernel(source, output, environment_contract, cfg, cli)
    report["lifecycle"] = "persistent"
    report["entry_labels_used"] = bool(audit_entries)

    if not audit_entries:
        # Unaudited: exactly the earlier behaviour, kept for ablation studies.
        return report

    labels = entry_contracts(source)
    published_root = Path(output).resolve()
    renamed_back, restored_total, entries_repaired = 0, 0, 0

    for entry, contract in sorted(labels.items()):
        if contract == PRIVATE:
            continue                       # root always precedes it; nothing owed

        # 1) Discoverability: a public entry must keep its indexed filename.
        if contract == PUBLIC and _restore_entry_name(published_root, entry):
            renamed_back += 1

        # 2) Content: restore whatever a direct call would no longer see.
        env_for_entry = environment_contract if contract == CONDITIONAL else None
        if independence(source, output, entry, env_for_entry).get("lost", 0) == 0:
            continue
        published_entry = resolve_entry(output, entry)
        if published_entry is None:
            continue
        missing = _missing_for_entry(source, output, entry, env_for_entry)
        restored = _restore_units(published_root / published_entry, missing)
        restored_total += restored
        entries_repaired += 1 if restored else 0

    report["entries_renamed_back"] = renamed_back
    report["units_restored"] = restored_total
    report["entries_repaired"] = entries_repaired
    return report


def audit_public_entries(source: str, published: str,
                         environment_contract: Optional[str] = None) -> Dict:
    """Re-read ``published`` from every declared entry and grade independence.

    Run this before shipping.  ``ok`` is ``True`` only when every ``public`` entry
    is still discoverable *and* keeps full content independence; the per-entry
    rows show which contract each path holds and where any loss occurred.
    """
    labels = entry_contracts(source)
    rows, failures = [], []
    for entry, contract in sorted(labels.items()):
        if contract == PRIVATE:
            continue
        env_for_entry = environment_contract if contract == CONDITIONAL else None
        row = independence(source, published, entry, env_for_entry)
        row["contract"] = contract
        row["effective"] = round(
            float(row["independence"]) * (1.0 if row["discoverable"] else 0.0), 4)
        rows.append(row)
        if contract == PUBLIC and row["effective"] < 1.0:
            failures.append(entry)

    public_rows = [r for r in rows if r["contract"] == PUBLIC]
    return {
        "ok": not failures,
        "failures": failures,
        "entries": rows,
        "public_count": len(public_rows),
        "worst_public_effective": min((r["effective"] for r in public_rows), default=1.0),
    }
