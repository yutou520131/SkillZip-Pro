"""Deterministic rendering with fixed templates (paper Alg. 1 line 5, Sec. 5.1
step 5). Produces a normal, human-readable skill body: a concise purpose and
triggers, global rules, a numbered workflow, nested guarded branches, explicit
tool requirements, and an output checklist. A shared workflow is named only
when references save tokens (decided in optimize.py); exceptions are placed
immediately after their base rule; locked residual is emitted verbatim.

Rendering is a pure function of the Library, so the same input always yields the
same text (deterministic optimization + rendering)."""
from __future__ import annotations

from typing import Dict, List

from .contract import Library, Unit, Procedure, ROOT
from .cost import unit_line


def _scope_label(scope: tuple) -> str:
    leaf = scope[-1] if scope else "root"
    if leaf.startswith("when-"):
        return "When " + leaf[len("when-"):].replace("-", " ")
    return leaf.replace("-", " ").title()


def _is_guard_branch(u: Unit) -> bool:
    """Unit lives under a 'when-' guard branch (shared condition rendered once
    as a '### When ...' header instead of inline per line)."""
    return len(u.scope) > 1 and u.scope[-1].startswith("when-")


def _emit_rule(u: Unit, out: List[str]) -> None:
    if u.locked:
        # Locked units carry structural artefacts (code fences, tables, routing
        # edges) that must land as their own block so a ``` marker keeps column
        # zero.  Enclose in blank lines regardless of surrounding bullet context.
        out.append("")
        out.append(unit_line(u))
        out.append("")
        return
    out.append(unit_line(u))
    for e in u.exceptions:              # exception right after its base rule
        out.append(f"    - Exception (when {e.guard}): {e.content}")


def render(lib: Library, name: str) -> str:
    units = lib.units
    by_type: Dict[str, List[Unit]] = {}
    for u in units:
        by_type.setdefault(u.type, []).append(u)

    out: List[str] = []

    # --- interface: purpose + triggers/exclusions --------------------------
    iface = by_type.get("interface", [])
    purpose = [u for u in iface if u.role in ("purpose", "name", "")]
    triggers = [u for u in iface if u.role == "trigger"]
    exclusions = [u for u in iface if u.role == "exclusion"]
    if purpose:
        out.append("## Purpose")
        for u in purpose:
            out.append(u.content.strip())
        out.append("")
    if triggers or exclusions:
        out.append("## When to use")
        for u in triggers:
            out.append(f"- {u.content}")
        for u in exclusions:
            out.append(f"- Do not use when: {u.content}")
        out.append("")

    # --- rules: global first, then nested guarded branches -----------------
    # guard branches collect rule/output/tool units sharing one condition
    branch_units: Dict[tuple, List[Unit]] = {}
    for u in units:
        if u.type in ("rule", "output", "tool") and _is_guard_branch(u):
            branch_units.setdefault(u.scope, []).append(u)
    branched = {id(u) for grp in branch_units.values() for u in grp}

    rules = by_type.get("rule", [])
    root_rules = [u for u in rules if len(u.scope) <= 1]
    branch_rules: Dict[tuple, List[Unit]] = {}
    # a branch whose label collides with the canonical Output section folds
    # into that section: no duplicated header, no ambiguity about which
    # 'Output' the agent should obey
    output_rules: List[Unit] = []
    for u in rules:
        if len(u.scope) > 1 and id(u) not in branched:
            if _scope_label(u.scope).lower() == "output":
                output_rules.append(u)
            else:
                branch_rules.setdefault(u.scope, []).append(u)
    if root_rules:
        out.append("## Rules")
        for u in root_rules:
            _emit_rule(u, out)
        out.append("")

    # --- workflow: numbered, with named procedures ------------------------
    # Steps that came from an anchored source heading ("### 步骤3：...") are
    # emitted under that heading verbatim, so cross-references elsewhere in the
    # skill ("jump to 步骤8") keep resolving to a real target.
    wf = sorted(by_type.get("workflow", []), key=lambda u: u.order)
    if wf or lib.procedures:
        out.append("## Workflow")
        proc_calls = {uid: p for p in lib.procedures for uid in p.unit_ids}
        emitted_proc: set = set()
        step = 0
        cur_anchor = None
        for u in wf:
            if u.id in proc_calls:
                p = proc_calls[u.id]
                if p.name not in emitted_proc:
                    step += 1
                    out.append(f"{step}. Run the **{p.name}** procedure.")
                    emitted_proc.add(p.name)
                continue
            anc = getattr(u, "anchor", "") or ""
            if anc and anc != cur_anchor:
                out.append(f"### {anc}")
                cur_anchor = anc
            step += 1
            # Number the step from the canonical unit line rather than from raw
            # content, so a step that only applies under a condition keeps that
            # condition. Emitting u.content directly dropped the guard and turned a
            # conditional step into an unconditional one.
            out.append(f"{step}. {unit_line(u).lstrip('- ').rstrip()}")
        out.append("")
        if lib.procedures:
            out.append("### Procedures")
            for p in lib.procedures:
                out.append(f"- **{p.name}**:")
                for s in p.steps:
                    out.append(f"    - {s}")
            out.append("")

    # --- guarded branches (rules/workflow/output scoped to a child) --------
    for scope, group in branch_rules.items():
        out.append(f"### {_scope_label(scope)}")
        for u in group:
            _emit_rule(u, out)
        out.append("")
    for scope, group in sorted(branch_units.items()):
        out.append(f"### {_scope_label(scope)}")
        for u in group:
            _emit_rule(u, out)
        out.append("")

    # --- tools -------------------------------------------------------------
    tools = [u for u in by_type.get("tool", []) if id(u) not in branched]
    if tools:
        out.append("## Tools")
        for u in tools:
            out.append(unit_line(u))
        out.append("")

    # --- output contract ---------------------------------------------------
    outputs = [u for u in by_type.get("output", []) if id(u) not in branched] \
        + output_rules
    if outputs:
        out.append("## Output")
        for u in outputs:
            _emit_rule(u, out)
        out.append("")

    # --- supporting evidence (kept only if it survived optimization) -------
    evidence = by_type.get("evidence", [])
    if evidence:
        out.append("## Notes")
        for u in evidence:
            # Locked evidence (fenced code, tables) MUST render verbatim: prefixing
            # a ``` marker with ``- `` moves it off column zero so downstream
            # scanners stop seeing it as a fence, and every path inside the code
            # then leaks out as an apparent broken reference.
            if u.locked:
                out.append("")
                out.append((u.content or "").rstrip())
                out.append("")
            else:
                # Route through unit_line so an evidence unit that carries a
                # condition keeps it.  Emitting a hand-built bullet here bypassed
                # the guard-inlining rule and silently turned a conditional note
                # into an unconditional one, which then shipped in the bundle.
                out.append(unit_line(u))
        out.append("")

    # --- locked residual: verbatim, undeletable ---------------------------
    if lib.residual:
        out.append("## Preserved (verbatim)")
        for u in lib.residual:
            out.append(u.content.strip())
        out.append("")

    body = "\n".join(out).strip()
    body = repair_anchors(body, lib.all_units())
    return body


def _section_of(units, label: str) -> tuple:
    """(original heading title, canonical sections) for an anchored source
    section, i.e. where the units of that section ended up after re-rendering."""
    from . import refs
    sect = {"rule": "Rules", "workflow": "Workflow", "tool": "Tools",
            "output": "Output", "evidence": "Notes", "interface": "Purpose"}
    title, where = "", []
    for u in units:
        anc = getattr(u, "anchor", "") or ""
        if refs.anchor_label(anc) != label:
            continue
        title = title or anc
        s = sect.get(u.type, "")
        if s and s not in where:
            where.append(s)
    return title, where


def repair_anchors(body: str, units) -> str:
    """Reference integrity (deterministic): every anchor label still referenced
    by the rendered text must resolve to a real target. Type-based re-rendering
    dissolves the source's section headings, so an anchored section that was
    absorbed elsewhere ('步骤8：输出结果' -> '## Output') is re-declared here with
    its ORIGINAL heading text plus a pointer to the section that now carries its
    requirements. Cross-references such as 'jump to 步骤8' therefore keep
    resolving instead of dangling. `units` may be the selected library or (as a
    fallback for anchors whose units were merged away) the source contract."""
    from . import refs
    missing = refs.dangling_labels(body)
    if not missing:
        return body
    lines = []
    for label in missing:
        title, where = _section_of(units, label)
        if not (title and where):
            continue
        targets = " / ".join(f"**{w}**" for w in where)
        lines.append(f"### {title}")
        lines.append(f"- Requirements of this step are stated under {targets} "
                     f"in this document.")
    if not lines:
        return body
    head = "" if "\n## Anchors" in body else "\n\n## Anchors\n"
    return body + (head or "\n") + "\n".join(lines)
