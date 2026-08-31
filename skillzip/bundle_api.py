"""Phase-A public API: harness-compatible skill bundle compression."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import posixpath
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Tuple

from . import audit, extract, optimize, refs, render
from .bundle import (BundleGraph, BundleNode, output_path_map, resolve_bundle,
                     source_path, split_frontmatter)
from .bundle_audit import audit_bundle
from .bundle_cost import compare_costs, measure_bundle_cost
from .capsules import CapsulePlan, plan_capsules
from .contract import Contract, Unit
from .cost import unit_line
from .environment import (EnvironmentContract, apply_environment_contract,
                          load_environment_contract)
from .scanner import reference_occurrences
from .skill import approx_tokens


DEFAULT_BUNDLE_CFG = {
    "extract_llm": True,
    "audit_llm": True,
    "audit": True,
    "top_k": 6,
    "min_sim": 0.12,
    "min_conf": 0.55,
    "canonicalize": True,
    "wf_min_len": 2,
    "wf_min_occ": 2,
    "include_unreferenced": True,
    "strict_references": True,
    "normalize_nested_skills": True,
    "capsules": True,
    "capsule_min_tokens": 220,
    "capsule_min_sections": 2,
    "promote_cross_file": True,
    # Shortest repeated statement worth factoring. Sweeping this value showed the
    # reclaimed text saturates at 8 tokens and shorter thresholds change nothing,
    # while the two-sided gate and the never-inflate check still reject any group
    # whose navigation cost would exceed its saving.
    "promotion_min_tokens": 8,
    "deployment_weight": 0.05,
    # Navigation edges (condition -> target) are structural, not prose: keep them
    # verbatim so the emitted bundle stays routable under progressive loading.
    "lock_navigation": True,
    # Pre-existing out-of-root / stale references in the SOURCE are preserved
    # verbatim and never followed, instead of refusing the whole bundle.  Newly
    # introduced unsafe references remain a hard audit failure.
    "tolerate_source_reference_defects": True,
    # Fenced code / tables are literal artifacts (commands, schemas, templates):
    # reproduce them exactly instead of re-rendering them as prose.
    "lock_literal_blocks": True,
    # Optional: split a long routing table into groups that are themselves loaded
    # on demand, preserving every condition and target one hop deeper. Measured on
    # the bundles in this study it does not pay off -- moving routing lines out of
    # the root removes them from activation but adds a hop to every path -- so the
    # never-inflate check rejects the candidate and the source is republished. Kept
    # off by default and available for workloads with far more branches than files.
    "group_routing_index": False,
    "router_max_root_edges": 8,
    "router_group_size": 5,
}


class BundleCompressionError(RuntimeError):
    pass


def _cfg(overrides: Optional[dict]) -> dict:
    config = dict(DEFAULT_BUNDLE_CFG)
    if overrides:
        config.update(overrides)
    return config


_LOCAL_LINK = re.compile(r"\]\(\s*(?!https?:|mailto:|data:)([^)]+)\)")


def _lock_navigation_units(contract: Contract) -> int:
    """Keep every (condition, target) navigation pair verbatim in one locked unit.

    A line such as ``When processing CSV, read [CSV workflow](references/csv.md).``
    is not prose about the task -- it is the *routing table* of a progressively
    loaded skill.  Two things damage it if it is treated as ordinary content:

    * extraction lifts the leading ``When ...`` into a separate ``guard``/scope
      field, after which the renderer may emit that guard as a section heading for
      one link and silently drop it for the rest, and
    * several such lines look like near-duplicate paraphrases and get merged.

    Both destroy the mapping from situation to resource, leaving a bundle whose
    files are still present but no longer routable.  We therefore fold the guard
    back into the sentence, detach it from the scope tree, and lock the unit, so the
    condition and its target always travel together.  Ordinary prose in the same
    file is still compressed normally.
    """
    locked = 0
    for unit in contract.all_units():
        if not _LOCAL_LINK.search(unit.content or ""):
            continue
        if unit.guard:
            phrase = unit.guard.strip().rstrip(",;:")
            body = (unit.content or "").strip()
            if not re.match(r"(?i)^(?:when|if|for|whenever|on|in case of|unless)\b",
                            body):
                # The guard already carries its original connective, so re-adding
                # one would produce "When When the question is about sports, ..."
                # and the routing line would no longer match its source form.
                if re.match(r"(?i)^(?:when|if|for|whenever|on|in case of|unless)\b",
                            phrase):
                    lead = phrase[0].upper() + phrase[1:]
                else:
                    lead = f"When {phrase}"
                unit.content = f"{lead}, {body[0].lower() + body[1:]}" \
                    if body else f"{lead}."
            unit.guard = ""
            unit.scope = ("root",)
        if not unit.locked:
            unit.locked = True
            locked += 1
    return locked


def _group_routing_index(contract: Contract, root_path: str,
                         max_root_edges: int, group_size: int
                         ) -> Tuple[List[dict], Dict[str, str], Dict[str, List[Unit]]]:
    """Keep a long routing table out of the always-loaded root.

    The root of a progressively loaded skill must state, for every branch, when to
    open it.  With a handful of branches that table is small.  As a self-evolving
    library keeps adding branches the table grows one line per branch, and because
    the root is loaded on every single run its cost is paid by every task -- the
    very thing progressive disclosure exists to avoid.  Yet the table cannot simply
    be shortened: deleting a condition makes its branch unroutable.

    So we apply the same principle to the router itself.  The routing lines are
    split into groups, each group is written to its own file, and the root keeps one
    short line per group naming the situations that group covers.  Every original
    (condition, target) pair still exists verbatim, one level deeper, so routing is
    preserved exactly while the always-loaded layer stops growing with the library.
    A run now follows two hops instead of one, which is why this only triggers once
    the table is long enough for the saving to outweigh that hop.
    """
    nav_units = [u for u in contract.all_units()
                 if u.locked and _LOCAL_LINK.search(u.content or "")
                 and re.match(r"(?i)^\s*(?:when|if|for|whenever|unless)\b", u.content or "")]
    if len(nav_units) <= max_root_edges:
        return [], {}, {}

    groups: List[List[Unit]] = [nav_units[i:i + group_size]
                               for i in range(0, len(nav_units), group_size)]
    if len(groups) < 2:
        return [], {}, {}

    actions: List[dict] = []
    sources: Dict[str, str] = {}
    group_units: Dict[str, List[Unit]] = {}
    root_dir = str(PurePosixPath(root_path).parent)
    keep: List[Unit] = []
    for index, group in enumerate(groups, 1):
        group_path = f"references/.skillzip_router/{index:02d}.md"
        lines = [f"# Routing group {index}", ""]
        labels: List[str] = []
        for unit in group:
            lines.append((unit.content or "").rstrip())
            match = re.match(r"(?i)^\s*(?:when|if|for|whenever|unless)\s+(.{0,48}?)[,(]",
                             unit.content or "")
            if match:
                labels.append(match.group(1).strip())
        sources[group_path] = "\n".join(lines) + "\n"
        group_units[group_path] = [copy.deepcopy(u) for u in group]
        rel = posixpath.relpath(group_path, root_dir) if root_dir not in ("", ".") \
            else group_path
        summary = "; ".join(labels[:3]) if labels else f"group {index}"
        keep.append(Unit(
            id="G" + hashlib.sha1(group_path.encode()).hexdigest()[:10],
            type="workflow",
            scope=("root",),
            content=f"When your task involves {summary}, read "
                    f"[routing group {index}]({rel}) and follow the entry that matches.",
            order=0,
            provenance=[f"generated:router-group:{group_path}"],
            locked=True,
        ))
        actions.append({
            "op": "GROUP_ROUTING_INDEX",
            "resource": root_path,
            "witness_path": group_path,
            "edges": len(group),
            "conditions": labels,
        })

    moved = {id(u) for group in groups for u in group}
    contract.units = [u for u in contract.units if id(u) not in moved] + keep
    contract.residual = [u for u in contract.residual if id(u) not in moved]
    return actions, sources, group_units


def _lock_literal_blocks(contract: Contract) -> int:
    """Keep fenced code and tables verbatim.

    The scanner already treats a fence or table as one atomic block precisely
    because "splitting them can destroy a template or schema".  Rendering then
    undoes that: the block is re-emitted as ordinary bullet prose, so the fence
    markers are lost, runnable commands merge into surrounding text, and file names
    that were only ever example arguments start to look like broken links.  A code
    block or table is a literal artifact, not a paraphrasable requirement, so it is
    locked and reproduced exactly.
    """
    literal_blocks = {block.id for block in contract.blocks
                      if block.kind in ("code", "table")}
    if not literal_blocks:
        return 0
    locked = 0
    for unit in contract.all_units():
        if unit.locked:
            continue
        if any(prov in literal_blocks for prov in unit.provenance):
            unit.locked = True
            locked += 1
    return locked


def _extract_markdown(text: str, cli, config: dict) -> Contract:
    _, core = split_frontmatter(text)
    contract = extract.extract_contract(
        core, cli=cli, use_llm=config.get("extract_llm", True))
    refs.attach_anchors(contract.all_units(), core)
    if config.get("lock_navigation", True):
        _lock_navigation_units(contract)
    if config.get("lock_literal_blocks", True):
        _lock_literal_blocks(contract)
    return contract


def _preserve_reference_lines(source: str, output: str) -> Tuple[str, List[str]]:
    """Restore reference-bearing source lines that a semantic render removed."""
    restored: List[str] = []
    output_targets = {ref.target for ref in reference_occurrences(output)}
    source_lines = source.splitlines()
    for ref in reference_occurrences(source):
        if ref.target in output_targets or not ref.target:
            continue
        if 0 < ref.line <= len(source_lines):
            line = source_lines[ref.line - 1].strip()
            if line and line not in restored:
                restored.append(line)
    if restored:
        output = output.rstrip() + "\n\n## Resources\n" + "\n".join(restored) + "\n"
    return output, restored


def _compile_markdown(
    text: str,
    name: str,
    contract: Contract,
    cli,
    config: dict,
    baseline_extra: str = "",
) -> Tuple[str, dict]:
    frontmatter, core = split_frontmatter(text)
    log: List[dict] = []
    library = optimize.min_cost_cover(contract, cli=cli, cfg=config, log=log)
    body = render.render(library, name=name)
    restored: List[str] = []
    if config.get("audit", True):
        source_contract = copy.deepcopy(contract)
        body, restored = audit.audit_and_restore(
            body, library, source_contract, cli=cli,
            cfg={**config, "name": name},
        )
    body = render.repair_anchors(body, contract.all_units())
    for label in refs.dangling_labels(body):
        span = refs.source_section(core, label)
        if span:
            body = body.rstrip() + "\n\n" + span + "\n"
    if frontmatter:
        body = frontmatter.rstrip("\n") + "\n\n" + body.lstrip("\n")
    body, restored_refs = _preserve_reference_lines(text, body)
    baseline = text.rstrip() + (("\n\n" + baseline_extra.strip())
                               if baseline_extra.strip() else "") + "\n"
    verbatim = approx_tokens(body) >= approx_tokens(baseline)
    if verbatim:
        body = baseline
    body = body.rstrip() + "\n"
    return body, {
        "original_tokens": approx_tokens(text),
        "compressed_tokens": approx_tokens(body),
        "remaining_ratio": round(approx_tokens(body) / max(1, approx_tokens(text)), 4),
        "selected_verbatim_baseline": verbatim,
        "extracted_units": len(contract.units),
        "required_units": len(contract.required()),
        "library_units": len(library.units),
        "restored_by_audit": restored,
        "reference_lines_restored": restored_refs,
        "savings": log,
    }


def _unit_key(unit: Unit) -> tuple:
    return (
        unit.type, unit.modality, unit.norm,
        " ".join((unit.guard or "").lower().split()),
        unit.tool, tuple(sorted(unit.args)), tuple(sorted(unit.fields)),
        " ".join((unit.validation or "").lower().split()),
    )


def _promote_cross_file(
    contracts: Dict[str, Contract],
    eligible: List[str],
    config: dict,
    root_path: str = "",
) -> Tuple[List[dict], Dict[str, str]]:
    """Factor exact cross-file duplicates into shared on-demand modules.

    Three soundness/efficiency properties are enforced explicitly:

    1. A navigation edge is *structural*, not prose.  The link that tells a module
       to read a shared requirement is a control transfer: two links with different
       targets are never interchangeable, so the generated link units are emitted
       ``locked`` and are therefore never merged, canonicalized, or dropped by the
       single-document optimizer.  Without this, several near-identical link lines
       (differing only in the module digest) are treated as redundant paraphrases
       and collapsed into one, silently orphaning the other shared modules.
    2. Requirements with the same *activation signature* live in one module.  If a
       set of factored requirements is always co-loaded by exactly the same set of
       resources, splitting them across several files buys nothing and costs one
       file header plus one link per module per resource.  Grouping them by
       signature keeps "explain once, reference many" while paying the navigation
       overhead a single time.
    3. Per-resource link overhead is paid once: one grouped navigation unit lists
       whatever modules that resource must load.
    """
    groups: Dict[tuple, List[Tuple[str, Unit]]] = {}
    for resource in eligible:
        for unit in contracts[resource].units:
            if unit.locked:
                continue
            # Which kinds of statement may be factored out of several files.
            # ``rule``, ``tool`` and ``output`` are plain obligations and are always
            # eligible.  ``evidence`` units are notes and checks with no ordering, so
            # they are eligible too; excluding them left a large part of the repeated
            # text unreclaimable for no safety benefit.  A ``workflow`` unit is only
            # eligible when the extractor found no ordering relation for it
            # (no predecessor and no successor): such a step states an obligation
            # rather than a position, and the branch is told to read the shared module
            # before executing, so the obligation still holds.  A workflow step that
            # *is* part of a sequence stays where it is, because moving it would change
            # the order the author specified.
            if unit.type in ("rule", "tool", "output", "evidence"):
                pass
            elif unit.type == "workflow" and not unit.edges_in and not unit.edges_out:
                pass
            else:
                continue
            if approx_tokens(unit.content) < config.get("promotion_min_tokens", 12):
                continue
            groups.setdefault(_unit_key(unit), []).append((resource, unit))

    # ---- select factorable groups and bucket them by activation signature ----
    # signature = the exact set of resources that must load the requirement.
    buckets: Dict[frozenset, List[Tuple[Unit, List[Tuple[str, Unit]], int]]] = {}
    for key, copies in sorted(groups.items(), key=lambda item: repr(item[0])):
        resources = sorted({resource for resource, _ in copies})
        if len(resources) < 2:
            continue
        representative = min((unit for _, unit in copies),
                             key=lambda unit: (approx_tokens(unit.content), unit.id))
        original_cost = sum(approx_tokens(unit_line(unit)) for _, unit in copies)
        buckets.setdefault(frozenset(resources), []).append(
            (representative, copies, original_cost))

    promotions: List[dict] = []
    shared_sources: Dict[str, str] = {}
    pending: Dict[str, List[str]] = {}     # resource -> [relative link target]

    # The sparse-root proposition cuts both ways.  It forbids moving a requirement
    # into the always-loaded root when only some branches need it (access
    # probability p < 1), because the root then charges every run for text most runs
    # never use.  As p approaches 1 the accounting reverses: a shared module would
    # be loaded on almost every path anyway, and it additionally costs one
    # navigation line inside each branch that uses it.  We therefore price both
    # placements for every factorable group and take the cheaper one.
    for signature in sorted(buckets, key=lambda s: sorted(s)):
        entries = buckets[signature]
        resources = sorted(signature)
        digest = hashlib.sha1(
            ("|".join(sorted(u.hash for u, _, _ in entries))).encode()).hexdigest()[:10]
        shared_path = f"references/.skillzip_shared/{digest}.md"

        original_cost = sum(cost for _, _, cost in entries)
        body_cost = sum(approx_tokens(unit_line(u)) for u, _, _ in entries)
        nav_cost = sum(
            approx_tokens(posixpath.relpath(shared_path,
                                            str(PurePosixPath(resource).parent)))
            for resource in resources)

        # Placement note.  Hoisting a shared requirement into the always-loaded root
        # would remove the per-branch navigation line, but the root is charged once
        # in activation and again on every execution path, so on every bundle
        # measured here the module placement is cheaper and hoisting was rejected by
        # the never-inflate check.  We therefore keep the activation-scoped module,
        # which is also what the sparse-root proposition prescribes for p < 1.
        factored_cost = body_cost + nav_cost + 6
        # Two-sided gate.  The package saving must be real, and the navigation
        # hop this operator adds to every execution path through the affected
        # resources must stay small relative to that saving.  The outer
        # compress_bundle acceptance test then re-checks the full joint objective
        # on the assembled candidate, so a locally attractive rewrite can still be
        # rejected globally; this gate only prevents obviously bad trades.
        deploy_delta = factored_cost - original_cost          # negative = saving
        listing_preview = "; ".join(
            f"[shared requirements]({posixpath.relpath(shared_path, str(PurePosixPath(r).parent))})"
            for r in resources[:1])
        exec_delta = approx_tokens(
            f"Before executing this module, read {listing_preview}.")
        if deploy_delta >= 0 or exec_delta >= -deploy_delta:
            continue

        promoted_units: List[Unit] = []
        lines: List[str] = [f"# Shared requirements {digest}", ""]
        for representative, copies, _ in entries:
            promoted = copy.deepcopy(representative)
            promoted.id = "P" + hashlib.sha1(
                representative.hash.encode()).hexdigest()[:10]
            promoted.scope = ("root",)
            promoted.provenance = sorted(
                {f"{resource}:{prov}"
                 for resource, unit in copies for prov in unit.provenance})
            promoted_units.append(promoted)
            lines.append(unit_line(promoted))
            for resource, unit in copies:
                contracts[resource].units = [u for u in contracts[resource].units
                                             if u is not unit]
                promotions.append({
                    "op": "FACTOR_SHARED_REFERENCE",
                    "resource": resource,
                    "unit_id": unit.id,
                    "unit_hash": unit.hash,
                    "shared_unit_id": promoted.id,
                    "witness_path": shared_path,
                    "activation_signature": resources,
                    "estimated_package_saving_tokens": original_cost - factored_cost,
                })
        contracts[shared_path] = Contract(units=promoted_units)
        shared_sources[shared_path] = "\n".join(lines) + "\n"
        for resource in resources:
            rel = posixpath.relpath(shared_path, str(PurePosixPath(resource).parent))
            pending.setdefault(resource, []).append(rel)

    # Emit exactly one locked navigation unit per resource, listing every shared
    # module that resource must load.  Locked => never merged or rewritten.
    for resource, targets in sorted(pending.items()):
        listing = "; ".join(f"[shared requirements]({rel})"
                            for rel in sorted(set(targets)))
        nav = Unit(
            id="L" + hashlib.sha1(resource.encode()).hexdigest()[:10],
            type="workflow",
            scope=("root",),
            content=f"Before executing this module, read {listing}.",
            order=0,
            provenance=[f"generated:shared-nav:{resource}"],
            locked=True,
        )
        contracts[resource].units.append(nav)

    return promotions, shared_sources


def _rewrite_original_references(
    text: str,
    source: str,
    graph: BundleGraph,
    path_map: Dict[str, str],
) -> str:
    source_out = PurePosixPath(path_map[source])
    for edge in [e for e in graph.local_edges() if e.source == source]:
        target_out = PurePosixPath(path_map[edge.target])
        replacement = posixpath.relpath(str(target_out), str(source_out.parent))
        if edge.fragment:
            replacement += "#" + edge.fragment
        text = text.replace(edge.raw_target, replacement)
    return text


def _write_candidate(
    temp_root: Path,
    graph: BundleGraph,
    path_map: Dict[str, str],
    compiled_docs: Dict[str, str],
    generated_docs: Dict[str, str],
) -> None:
    for resource, node in graph.nodes.items():
        destination = temp_root / path_map[resource]
        destination.parent.mkdir(parents=True, exist_ok=True)
        if resource in compiled_docs:
            destination.write_text(compiled_docs[resource], encoding="utf-8")
        else:
            shutil.copy2(source_path(graph, resource), destination)
    for resource, text in generated_docs.items():
        destination = temp_root / resource
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")


def _write_verbatim_baseline(temp_root: Path, graph: BundleGraph) -> None:
    for resource in graph.nodes:
        destination = temp_root / resource
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path(graph, resource), destination)


def _objective(cost, deployment_weight: float) -> float:
    return cost.expected_execution_tokens + \
        deployment_weight * cost.deployment_text_tokens


def compress_bundle(
    bundle: str,
    output: str,
    cli=None,
    cfg: Optional[dict] = None,
    environment_contract: Optional[str] = None,
    overwrite: bool = False,
) -> Tuple[str, dict]:
    """Compress a complete bundle without changing the agent-harness protocol.

    The returned directory contains only ordinary files.  ``SKILL.md`` remains
    the activation entry, referenced files retain ordinary relative links, and
    conditional capsules are themselves Markdown references.
    """
    config = _cfg(cfg)
    graph = resolve_bundle(bundle, include_unreferenced=config["include_unreferenced"])
    # Distinguish a defect the compressor would *introduce* from one the author
    # already shipped.  Real skill bundles routinely point outside their own
    # directory (package-level docs, sibling task files) and sometimes contain a
    # stale link.  Refusing those inputs outright makes the compressor unusable on
    # real skills while buying no safety: an out-of-root target is never resolved,
    # read, or copied -- the link text is simply preserved verbatim.  The compressor
    # is still held to "introduce no new unsafe or dangling reference", which the
    # bundle audit enforces against this recorded baseline.
    source_unsafe = {
        (edge.source, edge.raw_target, edge.status)
        for edge in graph.edges
        if edge.status in ("missing", "path_escape", "directory")
    }
    if config.get("tolerate_source_reference_defects", True):
        blocking: List[str] = []
    else:
        blocking = [message for message in graph.errors]
        if config.get("strict_references", True):
            blocking += [message for message in graph.warnings
                         if message.startswith(("missing:", "directory:"))]
    if blocking:
        raise BundleCompressionError("unsafe or unresolved source references: " +
                                     "; ".join(blocking))

    output_path = Path(output).expanduser().resolve()
    if output_path.exists() and not overwrite:
        raise FileExistsError(f"output already exists: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    environment = load_environment_contract(environment_contract)
    original_cost = measure_bundle_cost(graph)
    path_map = output_path_map(graph, config["normalize_nested_skills"])

    original_contracts: Dict[str, Contract] = {
        path: _extract_markdown(node.text or "", cli, config)
        for path, node in graph.nodes.items() if node.is_markdown and node.text is not None
    }
    original_env_drops: List[dict] = []
    for resource, contract in original_contracts.items():
        _, drops = apply_environment_contract(copy.deepcopy(contract), resource, environment)
        original_env_drops.extend(drops)

    document_sources = {
        path: node.text or "" for path, node in graph.nodes.items()
        if node.is_markdown and node.text is not None
    }
    coverage_targets = {path: [path_map[path]] for path in graph.nodes}
    capsule_plans: List[CapsulePlan] = []
    generated_sources: Dict[str, str] = {}
    capsuleized = set()
    if config.get("capsules", True):
        for path, node in sorted(graph.nodes.items()):
            if node.kind != "reference" or not node.reachable or node.text is None:
                continue
            plan = plan_capsules(
                path, node.text,
                min_tokens=config["capsule_min_tokens"],
                min_conditional_sections=config["capsule_min_sections"],
            )
            if plan is None:
                continue
            capsule_plans.append(plan)
            capsuleized.add(path)
            document_sources[path] = plan.dispatcher
            generated_sources.update(plan.capsules)
            coverage_targets[path] = [path_map[path]] + sorted(plan.capsules)

    working_sources = dict(document_sources)
    working_sources.update(generated_sources)
    contracts = {path: _extract_markdown(text, cli, config)
                 for path, text in working_sources.items()}
    actual_env_drops: List[dict] = []
    for resource, contract in contracts.items():
        _, drops = apply_environment_contract(contract, resource, environment)
        actual_env_drops.extend(drops)

    eligible_promotions = [
        path for path in document_sources
        if path != graph.entry and path not in capsuleized and path in contracts
    ]
    if config.get("promote_cross_file", True):
        promotions, shared_sources = _promote_cross_file(
            contracts, eligible_promotions, config, root_path=graph.entry)
    else:
        promotions, shared_sources = [], {}
    working_sources.update(shared_sources)
    generated_sources.update(shared_sources)

    # Keep a long routing table out of the always-loaded root. Every
    # (condition, target) pair is preserved, one hop deeper.
    router_groups: List[dict] = []
    if config.get("group_routing_index", True) and graph.entry in contracts:
        router_actions, router_sources, router_units = _group_routing_index(
            contracts[graph.entry], graph.entry,
            int(config.get("router_max_root_edges", 8)),
            int(config.get("router_group_size", 5)))
        if router_sources:
            working_sources.update(router_sources)
            generated_sources.update(router_sources)
            for group_path, units in router_units.items():
                contracts[group_path] = Contract(units=units)
            coverage_targets.setdefault(graph.entry, [path_map[graph.entry]])
            coverage_targets[graph.entry] = list(
                dict.fromkeys(coverage_targets[graph.entry] + sorted(router_sources)))
            # Recorded separately from unit-level promotions: this operator moves a
            # whole routing block, so it has no single source unit id to audit.
            router_groups = router_actions
    for item in promotions:
        targets = coverage_targets.setdefault(item["resource"], [])
        if item["witness_path"] not in targets:
            targets.append(item["witness_path"])

    compiled_all: Dict[str, str] = {}
    file_reports: Dict[str, dict] = {}
    for resource, text in sorted(working_sources.items()):
        name = PurePosixPath(resource).stem
        compiled, file_report = _compile_markdown(
            text, name, contracts[resource], cli, config,
        )
        if resource in graph.nodes:
            compiled = _rewrite_original_references(
                compiled, resource, graph, path_map)
        compiled_all[resource] = compiled
        file_reports[resource] = file_report

    compiled_docs = {path: compiled_all[path] for path in document_sources}
    generated_docs = {path: compiled_all[path] for path in generated_sources}

    temp_root = Path(tempfile.mkdtemp(prefix=".skillzip_tmp-", dir=str(output_path.parent)))
    selected_verbatim = False
    candidate_audit = None
    candidate_cost_report = None
    try:
        _write_candidate(temp_root, graph, path_map, compiled_docs, generated_docs)
        candidate_graph = resolve_bundle(str(temp_root), include_unreferenced=True)
        candidate_cost = measure_bundle_cost(candidate_graph)
        candidate_cost_report = candidate_cost.to_json()
        candidate_audit = audit_bundle(
            graph, candidate_graph, original_contracts, coverage_targets,
            original_env_drops, promotions,
            generated_resources=sorted(generated_sources),
            allowed_unsafe=source_unsafe,
        )
        weight = float(config.get("deployment_weight", 0.05))
        if (not candidate_audit["ok"] or
                _objective(candidate_cost, weight) >= _objective(original_cost, weight)):
            shutil.rmtree(temp_root)
            temp_root = Path(tempfile.mkdtemp(
                prefix=".skillzip_tmp-", dir=str(output_path.parent)))
            _write_verbatim_baseline(temp_root, graph)
            selected_verbatim = True
            coverage_targets = {path: [path] for path in graph.nodes}
            path_map = {path: path for path in graph.nodes}
            generated_sources = {}

        final_graph = resolve_bundle(str(temp_root), include_unreferenced=True)
        final_cost = measure_bundle_cost(final_graph)
        final_audit = audit_bundle(
            graph, final_graph, original_contracts, coverage_targets,
            original_env_drops, promotions if not selected_verbatim else [],
            generated_resources=sorted(generated_sources),
            allowed_unsafe=source_unsafe,
        )
        if not final_audit["ok"]:
            raise BundleCompressionError(
                "final bundle audit failed: " + json.dumps(final_audit, ensure_ascii=False))
        if output_path.exists():
            if output_path.is_dir():
                shutil.rmtree(output_path)
            else:
                output_path.unlink()
        os.replace(temp_root, output_path)
    except Exception:
        if temp_root.exists():
            shutil.rmtree(temp_root)
        raise

    report = {
        "mode": "phase_a_bundle",
        "phase": "A",
        "harness_changes_required": False,
        "source": str(Path(bundle).expanduser().resolve()),
        "output": str(output_path),
        "selected_verbatim_bundle_baseline": selected_verbatim,
        "execution_profile": {
            "entry": "SKILL.md",
            "progressive_loading": "ordinary Markdown references",
            "resolver_service": False,
            "activation_hook": False,
            "nested_skill_normalization": config["normalize_nested_skills"],
        },
        "source_graph": graph.to_json(),
        "environment_contract": environment.to_json(),
        "environment_drops": original_env_drops,
        "actual_environment_operations": actual_env_drops,
        "promotions": promotions,
        "router_groups": router_groups,
        "capsules": [plan.to_json() for plan in capsule_plans],
        "files": file_reports,
        "original_cost": original_cost.to_json(),
        "compressed_cost": final_cost.to_json(),
        "candidate_cost_before_fallback": candidate_cost_report,
        "ratios": compare_costs(original_cost, final_cost),
        "candidate_audit_before_fallback": candidate_audit,
        "audit": final_audit,
    }
    return str(output_path), report
