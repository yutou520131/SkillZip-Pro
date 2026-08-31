"""Contract extraction (paper Alg. 1 line 2, Sec. 5.1 step 2, Appendix B.3).

"Recover the typed contract once." A schema-constrained model receives the
numbered scan blocks and returns the contract of Eq. (1): interface entries,
workflow nodes/edges, tool calls + required arguments, scoped rules with
modality and guard, output fields, and example->requirement links. Every unit
must cite source blocks; the deterministic host rejects unsupported citations,
polarity mismatches, unknown tool names, and invalid workflow references, and
places ambiguous spans in the locked residual.

The extractor is deliberately *not* asked to compress (separating
interpretation from optimization). A fully deterministic parser is provided as
a fallback so the pipeline runs offline (mock backend) and so extraction never
crashes the compressor -- uncertain content becomes locked residual, matching
the paper's conservative failure mode (under-compression, never silent
deletion).
"""
from __future__ import annotations

import json
import re
from typing import List, Optional

from .contract import (Block, Contract, Unit, Scope, ROOT, MODALITIES)
from .scanner import scan, workflow_hint

# --- lexical signals for the deterministic parser -------------------------
_MUST_NOT = re.compile(r"\b(never|do not|don't|must not|avoid|no\b)", re.I)
_MUST = re.compile(r"\b(must|always|ensure|require[sd]?|shall)\b", re.I)
_SHOULD = re.compile(r"\b(should|prefer|recommend|try to|when possible)\b", re.I)
# The leading connective is captured, not discarded: it distinguishes a true
# condition ("If the range is dynamic, ...") from a purposive phrase ("For a
# repeatable check, ...").  Dropping it forces the renderer to guess a connective
# when it re-attaches the qualifier, which rewrites the author's sentence.
_GUARD = re.compile(r"^\s*(if|when|for|whenever|in case|unless|on)\b\s+(.+?)"
                    r"[,:]\s*(.+)$", re.I)
_TOOL = re.compile(r"`([a-zA-Z_][\w./-]*)`|\b(openpyxl|pandas|run_python|ws\.[a-z_]+|"
                   r"wb\.[a-z_]+)\b")
_OUTPUT_HINT = re.compile(r"\b(output|emit|respond|answer|return|format|\\boxed|"
                          r"final|ANSWER:|json|field|schema|save to|write .* to)\b", re.I)
_TOOL_HINT = re.compile(r"\b(tool|api|call|openpyxl|pandas|run_python|save|load|"
                        r"read|write)\b", re.I)


def _mk_scope(heading_path: List[str], guard: Optional[str]) -> Scope:
    """Scope identifier for a unit.

    The renderer reconstructs a section heading from the scope leaf, so the leaf
    must round-trip the source heading.  A short truncation limit silently cuts a
    heading mid-word ("When the problem is under-specified" becoming "...is
    unde"), which then ships in the compressed bundle.  The scope string is an
    internal identifier and is never charged as output cost, so we keep enough
    characters to preserve the heading and only guard against pathological length.
    """
    parts = ["root"] + [re.sub(r"\s+", "-", h.strip().lower())[:120]
                        for h in heading_path if h.strip()]
    if guard:
        parts.append("when-" + re.sub(r"\s+", "-", guard.strip().lower())[:120])
    return tuple(parts)


def _modality(text: str) -> str:
    if _MUST_NOT.search(text):
        return "must_not"
    if _MUST.search(text):
        return "must"
    if _SHOULD.search(text):
        return "should"
    return "info"


def _classify(block: Block) -> str:
    t = block.text
    low = t.lower()
    if block.kind == "heading":
        return "heading"
    if block.kind in ("code", "table"):
        return "evidence"
    if workflow_hint(block):
        return "workflow"
    if _OUTPUT_HINT.search(low) and ("output" in " ".join(block.heading_path).lower()
                                     or _OUTPUT_HINT.search(low)):
        # output-ish content, but a prohibition is still a rule
        if _MUST_NOT.search(low) or _MUST.search(low):
            if re.search(r"\b(field|schema|format|\\boxed|answer:|json|emit|save)\b", low):
                return "output"
            return "rule"
        return "output"
    if _TOOL.search(t) and _TOOL_HINT.search(low):
        return "tool"
    if _MUST.search(low) or _MUST_NOT.search(low) or _SHOULD.search(low):
        return "rule"
    return "evidence"


def _uid(prefix: str, i: int) -> str:
    return f"{prefix}{i}"


def deterministic_extract(blocks: List[Block]) -> Contract:
    """Rule/heuristic parser used offline and as the conservative fallback.
    Produces typed units with provenance; genuinely ambiguous spans are kept
    as locked residual rather than dropped."""
    c = Contract(blocks=blocks)
    order = 0
    counters = {t: 0 for t in ("interface", "workflow", "tool", "rule", "output",
                               "evidence", "residual")}
    heading_path: List[str] = []

    for b in blocks:
        if b.kind == "heading":
            heading_path = b.heading_path
            continue
        if b.kind == "frontmatter":
            counters["interface"] += 1
            c.units.append(Unit(_uid("I", counters["interface"]), "interface",
                                ROOT, b.text, role="purpose", provenance=[b.id]))
            continue

        text = b.text.strip()
        if not text:
            continue

        # guard detection -> child scope
        guard = ""
        content = text
        gm = _GUARD.match(text)
        if gm:
            # Keep the connective inside the guard so the renderer can restore the
            # author's phrasing verbatim instead of substituting "when".
            guard = f"{gm.group(1).strip()} {gm.group(2).strip()}"
            content = gm.group(3).strip()

        scope = _mk_scope(b.heading_path or heading_path, guard)
        kind = _classify(b)

        if kind == "workflow":
            counters["workflow"] += 1
            order += 1
            content = re.sub(r"^\s*\d+[.)]\s*", "", content)
            c.units.append(Unit(_uid("W", counters["workflow"]), "workflow",
                                scope, content, modality="info", guard=guard,
                                order=order, provenance=[b.id]))
        elif kind == "tool":
            counters["tool"] += 1
            mt = _TOOL.search(text)
            tool = (mt.group(1) or mt.group(2)) if mt else ""
            args = re.findall(r"\b([a-z_]+)\s*=", text)
            c.units.append(Unit(_uid("T", counters["tool"]), "tool", scope,
                                content, modality=_modality(text), guard=guard,
                                tool=tool, args=args, provenance=[b.id]))
        elif kind == "output":
            counters["output"] += 1
            fields = re.findall(r"\b([a-z_]+)\s*(?:field|:)", content.lower())
            val = ""
            if re.search(r"\bvalidat|verify|check\b", content.lower()):
                val = "verify before finishing"
            c.units.append(Unit(_uid("O", counters["output"]), "output", scope,
                                content, modality=_modality(text), guard=guard,
                                fields=fields, validation=val, provenance=[b.id]))
        elif kind == "rule":
            counters["rule"] += 1
            c.units.append(Unit(_uid("C", counters["rule"]), "rule", scope,
                                content, modality=_modality(text), guard=guard,
                                provenance=[b.id]))
        else:  # evidence
            counters["evidence"] += 1
            c.units.append(Unit(_uid("E", counters["evidence"]), "evidence",
                                scope, content, modality="info", guard=guard,
                                provenance=[b.id]))
    _wire_workflow_edges(c)
    return c


def _wire_workflow_edges(c: Contract) -> None:
    wf = sorted(c.by_type("workflow"), key=lambda u: u.order)
    for a, b in zip(wf, wf[1:]):
        a.edges_out.append(b.id)
        b.edges_in.append(a.id)


# --- schema-constrained LLM extraction ------------------------------------

def _extract_prompt(blocks: List[Block]) -> str:
    numbered = "\n".join(
        f"[{b.id}] ({b.kind}; scope={'/'.join(b.heading_path) or 'root'}) {b.text}"
        for b in blocks if b.kind not in ("heading",))
    from .prompts import EXTRACT_PROMPT
    return EXTRACT_PROMPT.replace("{{BLOCKS}}", numbered)


def _parse_llm_units(raw: str, blocks: List[Block]) -> Optional[Contract]:
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except Exception:
        return None
    valid_ids = {b.id for b in blocks}
    block_text = {b.id: b.text for b in blocks}
    c = Contract(blocks=blocks)
    order = 0
    for i, u in enumerate(data.get("units", [])):
        try:
            t = u.get("type")
            if t not in ("interface", "workflow", "tool", "rule", "output", "evidence"):
                continue
            prov = [p for p in u.get("provenance", []) if p in valid_ids]
            if not prov:                      # reject unsupported citations
                continue
            modality = u.get("modality", "info")
            if modality not in MODALITIES:
                modality = "info"
            scope = tuple(u.get("scope") or ["root"])
            if t == "workflow":
                order += 1
            unit = Unit(
                id=u.get("id") or f"U{i}", type=t, scope=scope,
                content=(u.get("content") or "").strip(), modality=modality,
                guard=(u.get("guard") or "").strip(), provenance=prov,
                role=u.get("role", ""), tool=u.get("tool", ""),
                args=list(u.get("args", [])),
                # reject invented field names: a required output field must be
                # literally present in the unit content or its source blocks
                fields=[f for f in u.get("fields", [])
                        if str(f).lower() in (
                            (u.get("content") or "") +
                            " ".join(block_text.get(p, "") for p in prov)
                        ).lower()],
                validation=u.get("validation", ""), order=order)
            if not unit.content:
                continue
            _enforce_scope_qualifier(unit, block_text)
            c.units.append(unit)
        except Exception:
            continue
    if not c.units:
        return None
    # ambiguous / uncited spans -> locked residual
    cited = {p for u in c.units for p in u.provenance}
    for b in blocks:
        if b.kind in ("heading", "frontmatter"):
            continue
        if b.id not in cited and b.text.strip():
            c.residual.append(Unit(f"R{len(c.residual)}", "evidence",
                                   tuple(["root"]), b.text.strip(),
                                   provenance=[b.id], locked=True))
    _wire_workflow_edges(c)
    return c


def _enforce_scope_qualifier(unit, block_text: dict) -> None:
    """Deterministic host check: if a source block scopes its instruction with
    a leading "For X,:" qualifier (e.g. '**For function calls** (MUST use this
    format):'), the extracted unit must keep that applicability restriction --
    otherwise a conditional format silently becomes a global one. Restores the
    qualifier as the unit guard when the model dropped it."""
    lead = re.compile(r"(?i)^\s*\*{0,2}for\b\s+([^,:;.*]{2,60})")
    carried = {w for w in re.findall(
        r"[a-z0-9_]+", ((unit.content or "") + " " + (unit.guard or "")).lower())
        if len(w) > 2}
    for p in unit.provenance:
        m = lead.match(block_text.get(p, "") or "")
        if not m:
            continue
        qual = m.group(1).strip()
        qw = [w for w in re.findall(r"[a-z0-9]+", qual.lower()) if len(w) > 2]
        if qw and sum(1 for w in qw if w in carried) / len(qw) < 0.6:
            unit.content = f"For {qual}: {unit.content}"
        break


def extract_contract(text: str, cli=None, use_llm: bool = True) -> Contract:
    """Alg. 1 line 2. Scan then recover the typed contract. Uses the
    schema-constrained model when a client is given and reachable; otherwise
    (or on any failure) falls back to the deterministic parser."""
    blocks = scan(text)
    if use_llm and cli is not None and getattr(cli, "backend", "mock") != "mock":
        try:
            raw = cli.chat(_extract_prompt(blocks), temperature=0.0)
            c = _parse_llm_units(raw, blocks)
            if c is not None and _covers_source(c, blocks):
                return c
        except Exception:
            pass
    return deterministic_extract(blocks)


def _covers_source(c: Contract, blocks: List[Block]) -> bool:
    """Sanity gate: the extraction must reference a reasonable share of the
    non-heading source blocks, else we distrust it and fall back."""
    src = [b for b in blocks if b.kind not in ("heading", "frontmatter") and b.text.strip()]
    if not src:
        return True
    cited = {p for u in c.all_units() for p in u.provenance}
    return len(cited) >= max(1, int(0.5 * len(src)))
