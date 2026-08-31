"""Continual compression: Zip-on-Write (paper Alg. 2, Sec. 5.2, Appendix B.7).

A self-evolving agent usually produces a small patch rather than a rewrite.
SkillZip stores a sidecar (ZipState) with the current library and reuse
statistics; the rendered SKILL.md remains the only artifact the agent loads.
For every patch unit the updater compares four interpretations under the same
objective and picks the feasible one with the smallest increase in Eq. (4):

  ABSORB   - patch restates an existing requirement, adds no new contract unit
  REFINE   - patch adds a guard / tool argument / validation / exception
  EXTEND   - patch introduces a genuinely new requirement
  REFACTOR - patch makes a shared rule/workflow newly worthwhile

Candidate search is restricted to the matching type, current + ancestor scopes,
and adjacent workflow nodes (O(d k), not the full history). A write-ahead log
gives atomic updates; occasional global repacking recovers cross-patch reuse.
Delta_t is frozen before compression: the evolver decides *what* is learned,
SkillZip decides only *how* it is represented -- never using a task score.
"""
from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Tuple

from .contract import Contract, Library, Unit, ZipState, Exception_
from . import extract, relations, optimize, render, cost, workflow
from .prompts import PATCH_PROMPT

ABSORB, REFINE, EXTEND, REFACTOR = "ABSORB", "REFINE", "EXTEND", "REFACTOR"


def zip_init(initial_text: str, name: str, cli=None,
             cfg: Optional[dict] = None) -> ZipState:
    """Z_0: one-shot compress the initial (warm-start) skill into the sidecar.
    A leading YAML front-matter block (the skill-retrieval contract) is kept
    verbatim on the state and excluded from compression."""
    import re as _re
    cfg = cfg or {}
    fm = ""
    text = initial_text
    m = _re.search(r"^---\s*\n.*?\n---\s*\n", initial_text.lstrip(), _re.S) \
        if initial_text.lstrip().startswith("---") else None
    if m:
        fm = m.group(0)
        text = initial_text.lstrip()[m.end():]
    contract = extract.extract_contract(text, cli=cli,
                                        use_llm=cfg.get("extract_llm", True))
    from . import refs
    refs.attach_anchors(contract.all_units(), text)   # keep anchors resolvable
    lib = optimize.min_cost_cover(contract, cli=cli, cfg=cfg)
    st = ZipState(name=name, library=lib, frontmatter=fm)
    st.units_at_last_repack = len(lib.units)
    _refresh_stats(st)
    return st


def _with_fm(state: ZipState, body: str) -> str:
    if state.frontmatter:
        return state.frontmatter.rstrip("\n") + "\n\n" + body.lstrip("\n")
    return body


def _rule_family(u: Unit) -> str:
    return f"{u.type}|{u.modality}|{u.norm[:40]}"


def _refresh_stats(st: ZipState) -> None:
    fam: Dict[str, int] = {}
    for u in st.library.units:
        fam[_rule_family(u)] = fam.get(_rule_family(u), 0) + 1
    st.rule_family_counts = fam
    grams: Dict[str, int] = {}
    for g in workflow.action_ngrams(st.library.units, n=2):
        grams[g] = grams.get(g, 0) + 1
    st.ngram_counts = grams


def _retrieve_compatible(lib: Library, p: Unit) -> List[Unit]:
    """RetrieveCompatible: units of matching type in the current, ancestor, or
    sibling scope (paper: matching type, current scope, ancestor scopes, and
    adjacent workflow nodes)."""
    out = []
    for u in lib.units:
        if u.type != p.type:
            continue
        if u.blocking_key()[:1] != p.blocking_key()[:1]:
            continue
        # scope compatibility: same, ancestor, or descendant
        a, b = u.scope, p.scope
        if a == b or a[:len(b)] == b or b[:len(a)] == a or len(a) <= 2:
            out.append(u)
    return out


def _propose_op(p: Unit, cands: List[Unit], cli=None,
                cfg: Optional[dict] = None) -> Tuple[str, Optional[Unit]]:
    """Choose the feasible operation with the smallest increase in Eq. (4).
    Uses the LLM patch classifier when available, else a deterministic rule."""
    cfg = cfg or {}
    if not cands:
        return EXTEND, None

    # deterministic scoring of candidates by relation
    best = None
    best_label = relations.UNREL
    best_conf = 0.0
    for c in cands:
        label, conf = relations.relation(p, c, cli=None)  # cheap det. pre-rank
        sim = relations.jaccard(p.content, c.content)
        score = conf * (1.0 if label != relations.UNREL else 0.0) + sim
        if score > best_conf:
            best, best_label, best_conf = c, label, score

    # optional LLM adjudication (schema-constrained), cached
    if (cli is not None and getattr(cli, "backend", "mock") != "mock" and cands
            and (cfg or {}).get("patch_llm", True)):
        op = _llm_patch_op(p, cands, cli)
        if op is not None:
            target = None
            if op[1]:
                target = next((c for c in cands if c.id == op[1]), best)
            return op[0], (target or best)

    # deterministic mapping (increase-in-Eq.4 minimization)
    if best is None:
        return EXTEND, None
    if best_label == relations.EQUIV:
        return ABSORB, best                        # +0 tokens
    if best_label in (relations.LIMPL, relations.RIMPL, relations.CONFLICT):
        return REFINE, best                        # + small delta
    sim = relations.jaccard(p.content, best.content)
    if sim >= 0.6:
        return REFINE, best
    return EXTEND, None                            # + full unit


def _llm_patch_op(p: Unit, cands: List[Unit], cli) -> Optional[Tuple[str, str]]:
    cand_txt = "\n".join(f"[{c.id}] ({c.modality}) {c.content}" for c in cands[:6])
    prompt = (PATCH_PROMPT
              .replace("{p_mod}", p.modality).replace("{p_guard}", p.guard or "-")
              .replace("{p_text}", p.content).replace("{{CANDIDATES}}", cand_txt))
    try:
        raw = cli.chat(prompt, temperature=0.0)
        m = re.search(r"\{.*\}", raw or "", re.S)
        if not m:
            return None
        d = json.loads(m.group(0))
        op = (d.get("op") or "").upper()
        if op in (ABSORB, REFINE, EXTEND, REFACTOR):
            return op, d.get("target", "")
    except Exception:
        return None
    return None


def _apply_op(lib: Library, p: Unit, op: str, target: Optional[Unit]) -> None:
    """LocalMinCostUpdate: mutate a (copied) library per the chosen op."""
    if op == ABSORB and target is not None:
        # no new contract unit; just record provenance so audit can trace it
        target.provenance = sorted(set(target.provenance + p.provenance))
        return
    if op == REFINE and target is not None:
        if p.type == "tool":
            target.args = sorted(set(target.args) | set(p.args))
        if p.type == "output":
            target.fields = sorted(set(target.fields) | set(p.fields))
            if p.validation and not target.validation:
                target.validation = p.validation
        # guarded / conflicting delta -> attach as an explicit exception
        if p.guard and p.guard.strip().lower() != (target.guard or "").strip().lower():
            target.exceptions.append(Exception_(guard=p.guard, content=p.content))
        elif relations.jaccard(p.content, target.content) < 0.85:
            target.exceptions.append(Exception_(guard=p.guard or "additionally",
                                                content=p.content))
        target.provenance = sorted(set(target.provenance + p.provenance))
        return
    # EXTEND / REFACTOR-with-new-unit: add the new required unit
    lib.units.append(p)


def zip_update(state: ZipState, patch_text: str, cli=None,
               cfg: Optional[dict] = None) -> Tuple[ZipState, str]:
    """Alg. 2. Ingest one accepted patch; return the updated state and rendered
    skill. Follows the B.7 write-ahead protocol: propose -> apply to a copy ->
    validate coverage -> render -> atomic commit -> log."""
    cfg = cfg or {}
    # (1) parse + validate the patch
    pc = extract.extract_contract(patch_text, cli=cli,
                                  use_llm=cfg.get("extract_llm", True))
    patch_units = [u for u in pc.units if u.is_required() or u.type == "evidence"]

    # work on a copy of the sidecar (B.7 step 3)
    work = Library.from_json(state.library.to_json())
    proposed: List[dict] = []
    for p in patch_units:
        cands = _retrieve_compatible(work, p)
        op, target = _propose_op(p, cands, cli=cli, cfg=cfg)
        # ensure unique id inside the working library
        if any(u.id == p.id for u in work.units):
            p.id = f"{p.id}+{state.patches_since_repack}"
        _apply_op(work, p, op, target)
        proposed.append({"patch_unit": p.id, "op": op,
                         "target": target.id if target else ""})

    # (4) validate coverage: every patch required unit must be represented
    _assert_patch_coverage(work, patch_units)

    new_state = ZipState(name=state.name, library=work,
                         patches_since_repack=state.patches_since_repack + 1,
                         units_at_last_repack=state.units_at_last_repack,
                         log=list(state.log))
    _refresh_stats(new_state)

    # (repack) recover cross-patch reuse when due
    if _repack_due(new_state, cfg):
        merged = Contract(units=list(work.units), residual=list(work.residual))
        repacked = optimize.min_cost_cover(merged, cli=cli, cfg=cfg)
        new_state.library = repacked
        new_state.patches_since_repack = 0
        new_state.units_at_last_repack = len(repacked.units)
        _refresh_stats(new_state)
        proposed.append({"op": "REPACK", "units": len(repacked.units)})

    # (5) render + optional structural audit
    body = render.render(new_state.library, name=state.name)
    if cfg.get("audit", True):
        from . import audit
        source = Contract(units=new_state.library.all_units())
        body, restored = audit.audit_and_restore(body, new_state.library, source,
                                                 cli=cli, cfg={**cfg, "name": state.name})
        if restored:
            proposed.append({"op": "AUDIT_RESTORE", "restored": restored})

    # (7) commit the log entry
    new_state.log.append({"patch_units": len(patch_units), "ops": proposed})
    new_state.frontmatter = state.frontmatter
    return new_state, _with_fm(new_state, body)


def _assert_patch_coverage(lib: Library, patch_units: List[Unit]) -> None:
    present = lib.all_units()
    for p in patch_units:
        if not p.is_required():
            continue
        covered = any(u.id == p.id for u in present) or \
            any(relations.jaccard(u.content, p.content) >= 0.6 and u.type == p.type
                for u in present) or \
            any(relations.jaccard(e.content, p.content) >= 0.6
                for u in present for e in u.exceptions)
        if not covered:
            span = Unit.from_json(p.to_json())
            span.locked = True
            lib.residual.append(span)


def zip_repack(state: ZipState, cli=None,
               cfg: Optional[dict] = None) -> Tuple[ZipState, str]:
    """Global repack over the current compact contract (paper Sec. 5.2): recovers
    cross-patch reuse that local updates missed, then re-render + audit. Operates
    on the compact contract, not the historical prose."""
    cfg = cfg or {}
    merged = Contract(units=list(state.library.units),
                      residual=list(state.library.residual))
    repacked = optimize.min_cost_cover(merged, cli=cli, cfg=cfg)
    new_state = ZipState(name=state.name, library=repacked,
                         frontmatter=state.frontmatter,
                         patches_since_repack=0,
                         units_at_last_repack=len(repacked.units),
                         log=list(state.log))
    _refresh_stats(new_state)
    body = render.render(new_state.library, name=state.name)
    if cfg.get("audit", True):
        from . import audit
        source = Contract(units=new_state.library.all_units())
        body, restored = audit.audit_and_restore(body, new_state.library, source,
                                                 cli=cli, cfg={**cfg, "name": state.name})
        if restored:
            new_state.log.append({"op": "REPACK_AUDIT_RESTORE", "restored": restored})
    new_state.log.append({"op": "CHECKPOINT_REPACK", "units": len(repacked.units)})
    return new_state, _with_fm(new_state, body)


def _repack_due(st: ZipState, cfg: dict) -> bool:
    """RepackDue(Z, eta): B patches arrived, OR the contract grew by more than
    rho since the last repack (paper Sec. 5.2)."""
    B = cfg.get("repack_every", 4)
    rho = cfg.get("repack_growth", 0.5)
    if st.patches_since_repack >= B:
        return True
    base = max(1, st.units_at_last_repack)
    growth = (len(st.library.units) - base) / base
    return growth > rho
