"""Structural audit and conservative recovery (paper Alg. 1 lines 6-9, B.6).

The audit parser does NOT see the original skill or the expected contract. It
independently re-parses the rendered compressed skill; a deterministic diff then
checks trigger polarity, guards, modality, workflow reachability, tool
arguments, and output fields against the selected contract. For each missing
element, recovery restores the shortest original source span that covers it and
marks it locked=true. The intended failure mode is under-compression, never
silent deletion.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from .contract import Contract, Library, Unit
from . import extract, relations, render


def _signature_present(want: Unit, parsed: List[Unit]) -> bool:
    """Is the required unit `want` represented among the independently parsed
    units of the compressed skill? Type-sensitive coverage check (Def. 1)."""
    cands = [p for p in parsed if p.type == want.type]
    for p in cands:
        if want.type == "rule":
            if p.modality != want.modality:
                continue
            if relations.jaccard(p.content, want.content) >= 0.55:
                return True
        elif want.type == "tool":
            if p.tool and want.tool and p.tool != want.tool:
                continue
            if set(want.args) <= set(p.args) and \
               relations.jaccard(p.content, want.content) >= 0.4:
                return True
        elif want.type == "output":
            if set(want.fields) <= set(p.fields) and \
               relations.jaccard(p.content, want.content) >= 0.45:
                return True
        elif want.type == "workflow":
            if relations.jaccard(p.content, want.content) >= 0.5:
                return True
        elif want.type == "interface":
            if relations.jaccard(p.content, want.content) >= 0.4:
                return True
    # a merged/lifted unit may also be represented via an exception line;
    # fall back to a global content search across all parsed units
    for p in parsed:
        if relations.jaccard(p.content, want.content) >= 0.7:
            return True
    return False


def _verbatim_in_body(want: Unit, body: str) -> bool:
    """Deterministic presence witness: the unit's content (or its canonical
    rendered line) literally appears in the artifact. Reparse classification
    noise (e.g. an output line reparsed as a rule) must not trigger a restore
    when the text itself is demonstrably carried by the body (Def. 1)."""
    lines = [l.strip().lstrip("-*+ ").strip() for l in body.splitlines()]
    txt = (want.content or "").strip()
    if not txt:
        return True
    if txt in body:
        return True
    return any(relations.jaccard(txt, l) >= 0.7 for l in lines if l)


def contract_diff(selected: Library, parsed: Contract,
                  body: str = "") -> List[Unit]:
    """Required units of the selected contract missing from the reparse."""
    parsed_units = parsed.all_units()
    missing: List[Unit] = []
    # include exceptions as first-class checks: a guarded delta is required too
    for u in selected.units:
        if not u.is_required():
            continue
        if body and _verbatim_in_body(u, body):
            continue
        if not _signature_present(u, parsed_units):
            missing.append(u)
    return missing


def audit_and_restore(body: str, selected: Library, source: Contract,
                      cli=None, cfg: Dict = None) -> Tuple[str, List[str]]:
    """Alg. 1 lines 7-9. Reparse the compressed text, diff against the selected
    contract, and restore the shortest covering source span (locked) for any
    missing required element. Returns (possibly-updated body, restored ids)."""
    cfg = cfg or {}
    parsed = extract.extract_contract(body, cli=cli, use_llm=cfg.get("audit_llm", True))
    missing = contract_diff(selected, parsed, body=body)
    restored_ids: List[str] = []
    if not missing:
        return body, restored_ids

    existing_res = {u.id for u in selected.residual}
    for u in missing:
        # shortest source span covering the requirement = the original unit text
        orig = source.get(u.id) or u
        if orig.id in existing_res:
            continue
        span = Unit.from_json(orig.to_json())
        span.locked = True
        span.exceptions = []
        selected.residual.append(span)
        restored_ids.append(orig.id)

    new_body = render.render(selected, name=cfg.get("name", "skill"))
    return new_body, restored_ids
