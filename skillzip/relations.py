"""Typed relation checking pipeline (paper Alg. 1 step 3, Appendix B.3).

Hard compatibility keys (Eq. 12) are applied BEFORE any similarity:
    (type, modality, tool/output namespace, scope family).
Exact normalized units merge by hash. Remaining units in a key are retrieved by
a lightweight token index (a deterministic stand-in for the paper's embedding
index) and verified by a relation checker that returns equivalence / left- or
right-implication / conflict / unrelated. Only equivalence and implication
create sharing candidates; conflict creates an exception edge; low-confidence
pairs stay separate.

The relation checker uses the LLM when available and a conservative
deterministic classifier otherwise. Cache keys already come from the client's
disk cache, giving deterministic reruns.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from .contract import Unit
from .prompts import RELATION_PROMPT

_STOP = set("a an the to of and or for with in on at be is are do does not no "
            "you your it its this that as by from into when if then use using".split())

EQUIV = "equivalence"
LIMPL = "left_implication"
RIMPL = "right_implication"
CONFLICT = "conflict"
UNREL = "unrelated"


def tokens(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if w not in _STOP and len(w) > 1}


def jaccard(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def group_by_key(units: List[Unit]) -> Dict[tuple, List[Unit]]:
    groups: Dict[tuple, List[Unit]] = {}
    for u in units:
        groups.setdefault(u.blocking_key(), []).append(u)
    return groups


def retrieval_key(u: Unit) -> tuple:
    """Looser key for *candidate retrieval* only: (type, namespace, scope
    family) WITHOUT modality, so a positive rule and its negated paraphrase are
    still proposed as a candidate pair. The relation checker (not the key) then
    decides equivalence vs conflict -- this mirrors the paper's semantic
    embedding retrieval feeding a frozen relation checker (B.3)."""
    namespace = u.tool or (u.role if u.type == "interface" else "")
    if u.type == "output":
        namespace = "output"
    return (u.type, namespace, u.scope[:2])


def _polarity_conflict(a: Unit, b: Unit) -> bool:
    """must vs must_not on similar content = conflict."""
    pos = {"must", "should"}
    return ((a.modality in pos and b.modality == "must_not") or
            (b.modality in pos and a.modality == "must_not"))


def _guard_compatible(a: Unit, b: Unit) -> bool:
    ga, gb = (a.guard or "").strip().lower(), (b.guard or "").strip().lower()
    if ga == gb:
        return True
    return jaccard(ga, gb) >= 0.5 if (ga or gb) else True


def deterministic_relation(a: Unit, b: Unit) -> Tuple[str, float]:
    """Conservative relation classifier (fallback / offline). Returns
    (label, confidence)."""
    sim = jaccard(a.content, b.content)
    if _polarity_conflict(a, b) and sim >= 0.4:
        return CONFLICT, 0.7
    if not _guard_compatible(a, b):
        # same core under different guards -> potential exception, treat as
        # conflict candidate so the optimizer builds a guarded delta
        if sim >= 0.5:
            return CONFLICT, 0.6
        return UNREL, 0.5
    if sim >= 0.82:
        return EQUIV, min(0.99, sim)
    if sim >= 0.6:
        # containment heuristic -> implication
        ta, tb = tokens(a.content), tokens(b.content)
        if ta and ta <= tb:
            return RIMPL, 0.65
        if tb and tb <= ta:
            return LIMPL, 0.65
        return EQUIV, 0.6
    return UNREL, 0.5


def llm_relation(cli, a: Unit, b: Unit) -> Optional[str]:
    prompt = RELATION_PROMPT.format(
        a_mod=a.modality, a_guard=a.guard or "-", a_text=a.content,
        b_mod=b.modality, b_guard=b.guard or "-", b_text=b.content)
    try:
        raw = (cli.chat(prompt, temperature=0.0) or "").strip().lower()
    except Exception:
        return None
    for label in (EQUIV, LIMPL, RIMPL, CONFLICT, UNREL):
        if label in raw:
            return label
    return None


def relation(a: Unit, b: Unit, cli=None) -> Tuple[str, float]:
    """Relation with hard-key precheck. Units in different retrieval keys
    (different type/namespace/scope-family) are unrelated by construction; a
    modality difference is left for the checker to classify (equivalence via
    double negation vs conflict)."""
    if retrieval_key(a) != retrieval_key(b):
        return UNREL, 1.0
    if a.hash == b.hash:
        return EQUIV, 1.0
    if cli is not None and getattr(cli, "backend", "mock") != "mock":
        lbl = llm_relation(cli, a, b)
        if lbl is not None:
            return lbl, 0.9
    return deterministic_relation(a, b)


def retrieve_pairs(units: List[Unit], top_k: int = 4,
                   min_sim: float = 0.45,
                   max_pairs: int = 10 ** 9) -> List[Tuple[Unit, Unit]]:
    """Within each blocking key, propose near-duplicate candidate pairs by token
    similarity (top-k per unit). With a low `min_sim` this catches semantic
    paraphrases (later adjudicated by the relation checker); `max_pairs` caps the
    global number of pairs (highest-similarity first) so a downstream LLM checker
    stays bounded."""
    scored_pairs: List[Tuple[float, Unit, Unit]] = []
    seen = set()
    groups: Dict[tuple, List[Unit]] = {}
    for u in units:
        groups.setdefault(retrieval_key(u), []).append(u)
    for key, group in groups.items():
        if len(group) < 2:
            continue
        for i, u in enumerate(group):
            scored = sorted(
                ((jaccard(u.content, v.content), j, v)
                 for j, v in enumerate(group) if j != i),
                key=lambda x: (-x[0], x[1]))
            for sim, j, v in scored[:top_k]:
                if sim < min_sim:
                    break
                a, b = (u, v) if u.id <= v.id else (v, u)
                pk = (a.id, b.id)
                if pk in seen:
                    continue
                seen.add(pk)
                scored_pairs.append((sim, a, b))
    # global cap: keep the highest-similarity candidate pairs (deterministic)
    scored_pairs.sort(key=lambda x: (-x[0], x[1].id, x[2].id))
    return [(a, b) for _, a, b in scored_pairs[:max_pairs]]
