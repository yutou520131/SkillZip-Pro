"""Practical length model (paper Eq. 7 / 13) and decision-specific cost tests
(Eq. 8-11). Everything here is deterministic given the extracted units; no LLM
and no task access. This is the objective L(.) that MinCostCover minimizes.

    L^(x) = |Render(x)|_tok + lambda_d * n_def(x) + lambda_r * n_ref(x) + lambda_s * d(x)   (Eq. 13)

The lambda charges equal the *actual* token cost of the template delimiters
used by the renderer (a named-procedure definition header, a reference call,
and one level of scope nesting). They are not fitted on any downstream task.
"""
from __future__ import annotations

import re
from typing import List

from .skill import approx_tokens
from .contract import Unit, Procedure, Exception_

# lambda charges = token cost of the corresponding render-template delimiters.
LAMBDA_DEF = 6     # "### Procedure: <name>" header + list framing
LAMBDA_REF = 4     # "- Run <name> procedure." call line framing
LAMBDA_SCOPE = 3   # one nested "when <guard>:" branch header per depth level

_MODALITY_PREFIX = {"must": "MUST ", "must_not": "MUST NOT ",
                    "should": "SHOULD ", "info": ""}


def token_len(text: str) -> int:
    return approx_tokens(text or "")


def _inline_guard(u: Unit, line: str) -> str:
    """Keep a unit's condition attached to the unit unless the rendered layout
    already states it, otherwise the conditional silently widens into an
    unconditional rule.

    Which units get their condition from the layout is decided by ``render``, not
    by the scope string.  ``render`` groups ``rule``/``output``/``tool`` units that
    share a guard scope under a single ``### When ...`` heading, so those units may
    safely omit it.  ``evidence`` and ``workflow`` units are emitted in flat
    sections (``## Notes``, the numbered workflow) with no such heading, so their
    condition must travel inline.  Getting this wrong is silent: the guard simply
    disappears, and it disappears permanently once the section is lifted into a
    capsule file where no heading remains at all.
    """
    g = (u.guard or "").strip()
    if not g:
        return line
    gw = [w for w in re.findall(r"[a-z0-9]+", g.lower()) if len(w) > 2]
    low = (u.content or "").lower()
    if gw and sum(1 for w in gw if w in low) / len(gw) >= 0.6:
        return line                      # content already states the condition
    if u.type in ("rule", "output", "tool") and \
            len(u.scope) > 1 and u.scope[-1].startswith("when-"):
        return line                      # covered by the emitted '### When ...' heading
    # The guard carries its original connective, so restoring it in front of the
    # content reproduces the author's sentence rather than inventing a connective.
    match = re.match(r"(- |\d+\. )(.*)$", line, re.S)
    if match:
        head, rest = match.group(1), match.group(2)
        return f"{head}{g[0].upper()}{g[1:]}, {rest[0].lower()}{rest[1:]}" if rest \
            else f"{head}{g}"
    return f"{g[0].upper()}{g[1:]}, {line}"


def unit_line(u: Unit) -> str:
    """Canonical one-line rendering of a unit; the atom counted by L(.).
    render.py reuses this so the cost model and the renderer never diverge.
    Rendering is lean: the original phrasing already encodes modality
    ("always"/"never"), so no redundant MUST/SHOULD prefix is added."""
    if u.locked:
        # Verbatim reproduction with no bullet prefix.  A locked unit typically
        # carries structural content -- a code fence, a table row, or an in-bundle
        # navigation link -- and prefixing it with ``-`` would move a fence marker
        # off column zero (so scanner stops recognising it and treats every path
        # inside the fence as a live reference) or fold multiple routing edges
        # into one bullet.  We preserve the original block boundary and rely on
        # render.py to insert a preceding blank line.
        return (u.content or "").rstrip()
    if u.type == "rule":
        return _inline_guard(u, f"- {u.content}".rstrip())
    if u.type == "tool":
        args = f" (args: {', '.join(u.args)})" if u.args else ""
        return _inline_guard(u, f"- Tool `{u.tool}`{args}: {u.content}".rstrip())
    if u.type == "output":
        # charge the fields suffix only for names not already carried by the
        # content itself (a redundant suffix is pure template overhead)
        flds = [f for f in u.fields if f.lower() not in (u.content or "").lower()]
        suffix = f" [fields: {', '.join(flds)}]" if flds else ""
        return _inline_guard(u, f"- {u.content}{suffix}".rstrip())
    if u.type == "workflow":
        return _inline_guard(u, f"- {u.content}")
    if u.type == "interface":
        return u.content
    return _inline_guard(u, f"- {u.content}")  # evidence


def guard_text(u: Unit) -> str:
    return (u.guard or "").strip()


def L_unit(u: Unit) -> int:
    """Length of a single rendered unit including its scope-depth charge and
    any attached guarded exceptions (Eq. 13). The guard is part of the rendered
    line (unit_line inlines it), so it is charged there, not separately.
    Locked residual is charged for its verbatim text."""
    base = token_len(unit_line(u))
    depth = max(0, len(u.scope) - 1)
    scope_charge = LAMBDA_SCOPE * depth
    exc = sum(token_len(f"- Exception (when {e.guard}): {e.content}")
              for e in u.exceptions)
    return base + scope_charge + exc


def L_procedure(p: Procedure) -> int:
    """Length of a named shared procedure definition def(q) (Eq. 10): the
    definition header + one line per step."""
    body = token_len("\n".join(f"- {s}" for s in p.steps))
    return LAMBDA_DEF + body


def L_call() -> int:
    """Length of one reference/call to a named abstraction (Eq. 10)."""
    return LAMBDA_REF


def L_units(units: List[Unit]) -> int:
    return sum(L_unit(u) for u in units)


# ---------------------------------------------------------------------------
# Decision-specific cost tests (Appendix A.2). Each returns (selected, saving).
# saving(h) = L(separate form) - L(form using h)  (Eq. 5); non-positive -> reject.
# ---------------------------------------------------------------------------

def test_equivalent(shared: Unit, members: List[Unit],
                    residuals: List[Unit]) -> int:
    """Eq. (8): L(z) + sum L(x_i | z) < sum L(x_i).

    `shared` is the common unit z; `residuals` are the per-member residual
    deltas L(x_i | z) (empty for true paraphrases). Returns the token saving
    (positive => sharing selected)."""
    separate = sum(L_unit(x) for x in members)
    shared_cost = L_unit(shared) + sum(L_unit(r) for r in residuals)
    return separate - shared_cost


def test_scope_lift(rule_at_ancestor: Unit, copies: List[Unit]) -> int:
    """Eq. (9): L(c@u) + r * L(scope-ref) < sum_i L(c@s_i).

    Lifting a rule repeated in r child scopes to the common ancestor. The
    scope-ref charge is the per-child annotation cost avoided."""
    r = len(copies)
    separate = sum(L_unit(c) for c in copies)
    lifted = L_unit(rule_at_ancestor) + r * LAMBDA_SCOPE
    return separate - lifted


def test_workflow_reuse(proc: Procedure, occurrences: int) -> int:
    """Eq. (10): L(def(q)) + r * L(call(q)) < r * L(q)."""
    r = occurrences
    q_len = token_len("\n".join(f"- {s}" for s in proc.steps))
    separate = r * q_len
    shared = L_procedure(proc) + r * L_call()
    return separate - shared


def test_common_rule_exceptions(core: Unit, deltas: List[Exception_],
                                variants: List[Unit]) -> int:
    """Eq. (11): L(c) + sum L(delta_i) < sum L(c_i @ g_i)."""
    separate = sum(L_unit(v) for v in variants)
    exc_cost = sum(token_len(f"- Exception (when {d.guard}): {d.content}")
                   for d in deltas)
    shared = L_unit(core) + exc_cost
    return separate - shared
