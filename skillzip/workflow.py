"""Workflow graph + repeated-sequence mining (paper Alg. 1 step 3/4, B.5).

Workflow paths are represented by *action identifiers* that include action
type, tool name, required arguments, and guard class (B.5). Re-Pair proposes
repeated adjacent pairs and prefix/suffix scans identify shared branch
segments. A shared-procedure candidate must have at least two non-overlapping
occurrences, compatible entry/exit behavior, and positive saving under Eq. (10)
(the saving test itself lives in cost.py / optimize.py).
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple

from .contract import Unit, Procedure


def action_id(u: Unit) -> str:
    """Stable action identifier for a workflow unit (B.5). Uses a coarse verb +
    salient noun signature plus tool/guard class so genuinely identical steps
    align while argument/guard differences keep steps distinct."""
    words = re.findall(r"[a-z0-9]+", u.content.lower())
    verb = words[0] if words else "act"
    salient = "-".join(w for w in words[1:5] if len(w) > 2)
    guard_class = re.sub(r"\s+", "-", (u.guard or "").lower())[:16]
    return f"{verb}:{salient}|{u.tool}|{guard_class}"


def ordered_actions(units: List[Unit]) -> List[Tuple[str, Unit]]:
    wf = sorted([u for u in units if u.type == "workflow"], key=lambda u: u.order)
    return [(action_id(u), u) for u in wf]


def _nonoverlapping_occurrences(seq: List[str], gram: Tuple[str, ...]) -> List[int]:
    """Start indices of non-overlapping occurrences of `gram` in `seq`."""
    out: List[int] = []
    i, g = 0, len(gram)
    while i + g <= len(seq):
        if tuple(seq[i:i + g]) == gram:
            out.append(i)
            i += g
        else:
            i += 1
    return out


def mine_repeats(units: List[Unit], min_len: int = 2,
                 min_occ: int = 2) -> List[Dict]:
    """Mine repeated contiguous workflow fragments (Re-Pair-style). Returns
    candidate dicts with the action gram, its non-overlapping occurrence start
    indices, and the covered unit ids for each occurrence."""
    actions = ordered_actions(units)
    seq = [a for a, _ in actions]
    uref = [u for _, u in actions]
    n = len(seq)
    candidates: List[Dict] = []
    seen = set()
    # longest grams first so maximal fragments win the greedy packer later
    for glen in range(min(n // 2, 6), min_len - 1, -1):
        for start in range(0, n - glen + 1):
            gram = tuple(seq[start:start + glen])
            if gram in seen:
                continue
            # a degenerate gram of identical no-content ids is not useful
            if all(not g.split(":")[1].strip("|") for g in gram):
                continue
            occ = _nonoverlapping_occurrences(seq, gram)
            if len(occ) >= min_occ:
                seen.add(gram)
                occ_units = [[uref[i + k].id for k in range(glen)] for i in occ]
                steps = [uref[occ[0] + k].content for k in range(glen)]
                candidates.append({
                    "gram": gram,
                    "occurrences": occ,
                    "occ_units": occ_units,
                    "steps": steps,
                    "length": glen,
                })
    return candidates


def make_procedure(name: str, cand: Dict) -> Procedure:
    flat = [uid for group in cand["occ_units"] for uid in group]
    return Procedure(name=name, steps=list(cand["steps"]), unit_ids=flat)


def action_ngrams(units: List[Unit], n: int = 2) -> List[str]:
    """n-grams of action ids, for Zip-on-Write reuse statistics (paper Sec 5.2:
    'approximate counts for rule families and action n-grams')."""
    seq = [a for a, _ in ordered_actions(units)]
    return ["\u2192".join(seq[i:i + n]) for i in range(len(seq) - n + 1)]
