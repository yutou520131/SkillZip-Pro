"""Cross-file structural and reference-integrity audit for Phase A."""
from __future__ import annotations

from typing import Dict, List, Set

from . import audit, extract
from .bundle import BundleGraph
from .contract import Contract, Unit


def _covered(unit: Unit, parsed: Contract, body: str) -> bool:
    return audit._verbatim_in_body(unit, body) or \
        audit._signature_present(unit, parsed.all_units())


def _reachable_from_entry(graph: BundleGraph) -> Set[str]:
    """Statically reachable resources, following resolved local reference edges."""
    adjacency: Dict[str, List[str]] = {}
    for edge in graph.local_edges():
        adjacency.setdefault(edge.source, []).append(edge.target)
    seen = {graph.entry}
    queue = [graph.entry]
    while queue:
        current = queue.pop(0)
        for nxt in adjacency.get(current, []):
            if nxt and nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def _unreachable_generated(graph: BundleGraph, generated: List[str]) -> List[str]:
    """Generated modules that no longer sit on any progressive-loading path.

    Rewriting a document can remove the only link that pointed at a module the
    compressor itself created (shared requirement, conditional capsule).  Such a
    module is not a dangling reference -- the link is gone rather than broken --
    so reference-integrity checks cannot see it, yet its content has become
    unloadable.  Treating this as a hard audit failure makes the transformation
    reachability-preserving by construction: on violation the caller falls back to
    the verbatim bundle instead of shipping unreachable knowledge.
    """
    reachable = _reachable_from_entry(graph)
    return sorted(path for path in generated
                  if path in graph.nodes and path not in reachable)


def audit_bundle(
    original_graph: BundleGraph,
    output_graph: BundleGraph,
    original_contracts: Dict[str, Contract],
    coverage_targets: Dict[str, List[str]],
    environment_drops: List[dict],
    promotions: List[dict],
    generated_resources: List[str] = None,
    allowed_unsafe: set = None,
) -> dict:
    parsed: Dict[str, Contract] = {}
    bodies: Dict[str, str] = {}
    for path, node in output_graph.nodes.items():
        if node.is_markdown and node.text is not None:
            bodies[path] = node.text
            parsed[path] = extract.extract_contract(node.text, cli=None, use_llm=False)

    env_keys = {(d["resource"], d["unit_id"]) for d in environment_drops}
    promoted_keys = {(p["resource"], p["unit_id"]): p["witness_path"]
                     for p in promotions}
    root_target = coverage_targets.get(original_graph.entry, [output_graph.entry])[0]
    missing: List[dict] = []
    witnesses: List[dict] = []

    for resource, contract in original_contracts.items():
        targets = coverage_targets.get(resource, [])
        for unit in contract.required():
            key = (resource, unit.id)
            if key in env_keys:
                witnesses.append({"resource": resource, "unit_id": unit.id,
                                  "witness": "environment"})
                continue
            search = list(targets)
            if key in promoted_keys and promoted_keys[key] not in search:
                search.append(promoted_keys[key])
            found = next((path for path in search
                          if path in parsed and _covered(unit, parsed[path], bodies[path])), None)
            # A promotion transaction may conservatively fall back to the leaf's
            # verbatim source.  Search the root after the leaf, but accept either.
            if found is None and key in promoted_keys:
                witness_path = promoted_keys[key]
                if witness_path in parsed and _covered(
                        unit, parsed[witness_path], bodies[witness_path]):
                    found = witness_path
            if found:
                witnesses.append({"resource": resource, "unit_id": unit.id,
                                  "witness": found})
            else:
                missing.append({
                    "resource": resource,
                    "unit_id": unit.id,
                    "type": unit.type,
                    "modality": unit.modality,
                    "content": unit.content,
                    "searched": search,
                })

    nested_skill_files = sorted(
        path for path in output_graph.nodes
        if path.rsplit("/", 1)[-1].lower() == "skill.md" and path != output_graph.entry)
    # Only *newly* unsafe references are a compression failure.  A stale or
    # out-of-root link the author already shipped is preserved verbatim and is not
    # attributable to the compressor; it is reported separately so it stays visible.
    # Phase-A may rename a nested SKILL.md to SUBSKILL.md, so compare on canonical
    # source paths (drop the ``SUBSKILL.md`` alias) before matching against the
    # recorded source baseline; otherwise the source-baseline lookup misses.
    def _canon(path: str) -> str:
        return path.replace("/SUBSKILL.md", "/SKILL.md")
    canonical_allowed = {(_canon(s), t, st) for (s, t, st) in (allowed_unsafe or set())}
    unsafe_edges = [edge for edge in output_graph.edges
                    if edge.status in ("missing", "path_escape", "directory")]
    dangling = [edge.to_json() for edge in unsafe_edges
                if (_canon(edge.source), edge.raw_target, edge.status)
                not in canonical_allowed]
    preexisting = [edge.to_json() for edge in unsafe_edges
                   if (_canon(edge.source), edge.raw_target, edge.status)
                   in canonical_allowed]
    missing_output_files = sorted(
        set(original_graph.nodes) - set(coverage_targets)
    )
    unreachable_generated = _unreachable_generated(
        output_graph, list(generated_resources or []))
    # A generic industrial harness may intentionally keep nested SKILL.md
    # files.  Phase A normally renames them to SUBSKILL.md, but the verbatim
    # feasibility baseline must remain valid too; therefore this is reported as
    # a portability warning rather than a behavioral-audit failure.
    ok = not (missing or dangling or missing_output_files or unreachable_generated)
    return {
        "ok": ok,
        "required_units": sum(len(c.required()) for c in original_contracts.values()),
        "covered_units": len(witnesses),
        "missing_requirements": missing,
        "dangling_or_unsafe_references": dangling,
        "preexisting_source_reference_defects": preexisting,
        "missing_output_mappings": missing_output_files,
        "unreachable_generated_modules": unreachable_generated,
        "nested_skill_md_files": nested_skill_files,
        "reference_cycles": output_graph.cycles,
        "environment_witnesses": len(env_keys),
        "promotion_witnesses": len(promoted_keys),
    }
