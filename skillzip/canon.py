"""Canonicalization pass: per-unit minimum-description phrasing (Eq. 5 / 13).

MinCostCover reduces L(K) + L(R|K) structurally (sharing, lifting, packing).
This pass additionally minimizes the *code length of each selected unit's
content*: the compressor model proposes the shortest faithful phrasing p' for
every unit p, and the deterministic host accepts p' only when

    (a) it is strictly shorter under the same token objective (Eq. 5), and
    (b) every protected literal survives verbatim -- backtick/quoted spans,
        output templates (``ANSWER:``, ``\\boxed{...}``), UPPERCASE identifiers
        (IN, OUT), numbers -- and explicit negations keep their polarity.

Rejected proposals leave the unit untouched (the conservative failure mode is
under-compression). Locked residual is never touched: it is verbatim by
contract. The pass reads only the skill representation, never tasks, rewards,
or trajectories (evaluation-free), and the downstream structural audit still
verifies the rendered artifact against the selected contract.
"""
from __future__ import annotations

import json
import re
from typing import Dict, List, Optional

from .contract import Unit
from .cost import token_len

CANON_PROMPT = """You compress the WORDING of typed agent-skill units. Rewrite
each numbered unit into its shortest faithful imperative phrasing.
STRICT constraints:
- Preserve every requirement, condition, threshold, and behavioral detail.
- Copy EXACT literal strings unchanged: anything in backticks or quotes, output
  templates (e.g. ANSWER: <short answer>, \\boxed{...}), tool/API/variable
  names (IN, OUT, openpyxl, web_search), and every number.
- Keep every conditional clause (if/when/unless/whenever ...) with its meaning;
  dropping a condition changes the rule. Pure rationale tails ("to prevent
  ...", "so that ...") MAY be dropped.
- Keep negations ("never", "do not") explicit.
- Keep each unit's ORIGINAL language (never translate).
- Do not add information; do not merge or split units.
Return STRICT JSON mapping each id (string) to its rewritten text. If a unit is
already minimal, return it unchanged. Reply with ONLY the JSON.

## UNITS
{{UNITS}}
"""

CANON_PROMPT3 = """Final tightening pass. Rewrite each unit using AT MOST 60%
of its current words -- telegraphic imperative style. NON-NEGOTIABLE: keep
every conditional clause (if/when/unless ...), every exact literal (backticks,
quotes, templates, IN/OUT, numbers), and every negation, and the unit's
ORIGINAL language (never translate). Prefer dropping
rationale tails, articles, and repeated context. If impossible without losing
meaning, return the unit unchanged. Return STRICT JSON mapping id to text.
Reply with ONLY the JSON.

## UNITS
{{UNITS}}
"""

CANON_PROMPT2 = """These agent-skill units are still verbose. Rewrite each into
minimal telegraphic form (drop filler words, articles, redundant clauses, and
pure rationale tails like "to prevent ..."; keep imperative verbs). The SAME
strict constraints apply: preserve every requirement/condition/threshold, keep
every conditional clause (if/when/unless ...) with its meaning, copy exact
literals (backticks, quotes, templates like ANSWER: <short answer>, variable
names IN/OUT, numbers), keep negations explicit, keep the unit's ORIGINAL
language (never translate), no new information. Return
STRICT JSON mapping id to text.
Reply with ONLY the JSON.

## UNITS
{{UNITS}}
"""

_NEG_RE = re.compile(r"\b(never|do not|don't|must not|avoid|no)\b", re.I)
_GUARD_RE = re.compile(r"\b(unless|if|when|whenever|in case|only if)\b\s+"
                       r"([^,;.]*)", re.I)
# a leading "For X," / "For X:" scopes the whole instruction to X -- losing it
# silently widens the rule (e.g. a function-call-only format becoming global)
_LEAD_FOR_RE = re.compile(r"(?i)^\s*\*{0,2}for\b\s+([^,:;.]{2,60})[,:]")

# Pure-emphasis capitalizations: the *word* must survive, but re-casing is a
# legal rewording ("exactly ONE block" -> "exactly one block"). Everything
# else caught by the uppercase pattern (ANSWER:, IN, OUT, PROJECT_ROOT) is an
# identifier/template and must survive verbatim.
_EMPHASIS = {"EXACTLY", "ONE", "MUST", "ALWAYS", "NEVER", "ALL", "NOT",
             "ONLY", "EACH", "ANY", "NO", "BEFORE", "AFTER", "SHOULD"}


def protected_literals(text: str) -> List[str]:
    """Deterministic set of spans that must survive rewording verbatim."""
    t = text or ""
    lits: List[str] = []
    lits += re.findall(r"`[^`]+`", t)                    # inline code
    lits += re.findall(r"'[^'\n]{2,}'", t)               # quoted templates
    lits += re.findall(r'"[^"\n]{2,}"', t)
    lits += re.findall(r"\\boxed\{[^}]*\}", t)           # math output template
    lits += re.findall(r"\b[A-Z]{2,}[A-Z0-9_]*:?", t)    # ANSWER:, IN, OUT, ...
    lits += re.findall(r"\d+(?:\.\d+)?", t)              # thresholds, counts
    return lits


def _words(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 2}


def _guards_preserved(old: str, new: str) -> bool:
    """Every conditional clause of the original must survive the rewrite: same
    subordinating conjunction present, and most of the clause's content words
    still carried. A dropped 'unless a decimal is requested' silently widens
    the rule -- that is a semantic change, not a rewording."""
    low = (new or "").lower()
    for conj, clause in _GUARD_RE.findall(old or ""):
        if conj.lower() not in low:
            return False
        cw = _words(clause)
        if cw and len(cw & _words(new)) / len(cw) < 0.6:
            return False
    m = _LEAD_FOR_RE.match(old or "")
    if m:
        cw = _words(m.group(1))
        if "for" not in low.split() and not low.startswith("for"):
            return False
        if cw and len(cw & _words(new)) / len(cw) < 0.6:
            return False
    return True


def faithful_shorter(old: str, new: str) -> bool:
    """Host-side acceptance test: strictly shorter AND literal/polarity safe."""
    new = (new or "").strip()
    if not new or token_len(new) >= token_len(old):
        return False
    low = new.lower()
    for lit in protected_literals(old):
        if lit.rstrip(":") in _EMPHASIS:
            if lit.rstrip(":").lower() not in low:
                return False
        elif lit not in new:
            return False
    if _NEG_RE.search(old) and not _NEG_RE.search(new):
        return False
    if not _guards_preserved(old, new):
        return False
    return True


def _propose_round(cand: List[Unit], prompt_tpl: str, cli,
                   log: Optional[List[dict]]) -> int:
    numbered = "\n".join(f"[{u.id}] {u.content}" for u in cand)
    try:
        raw = cli.chat(prompt_tpl.replace("{{UNITS}}", numbered),
                       temperature=0.0)
    except Exception:
        return 0
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return 0
    try:
        proposals: Dict = json.loads(m.group(0))
    except Exception:
        return 0
    saved = 0
    for u in cand:
        new = proposals.get(u.id)
        if not isinstance(new, str):
            continue
        new = new.strip()
        if not faithful_shorter(u.content, new):
            continue
        gain = token_len(u.content) - token_len(new)
        saved += gain
        if log is not None:
            log.append({"candidate": f"canon:{u.id}", "op": "canon",
                        "saving_tokens": gain})
        u.content = new
    return saved


def canonicalize_units(units: List[Unit], cli=None,
                       log: Optional[List[dict]] = None,
                       round2_min_tokens: int = 8,
                       round3_min_tokens: int = 15) -> int:
    """Propose + accept minimal phrasings for non-locked units in place.
    Three proposal rounds under the SAME host-side acceptance test: a faithful
    shrink, a telegraphic pass over units that stayed verbose, and a final
    word-budget pass over the longest survivors. Returns total tokens saved.
    No-op without a real model client."""
    if cli is None or getattr(cli, "backend", "mock") == "mock":
        return 0
    cand = [u for u in units if not u.locked and (u.content or "").strip()]
    if not cand:
        return 0
    saved = _propose_round(cand, CANON_PROMPT, cli, log)
    verbose = [u for u in cand if token_len(u.content) >= round2_min_tokens]
    if verbose:
        saved += _propose_round(verbose, CANON_PROMPT2, cli, log)
    longest = [u for u in cand if token_len(u.content) >= round3_min_tokens]
    if longest:
        saved += _propose_round(longest, CANON_PROMPT3, cli, log)
    return saved
