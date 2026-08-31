"""High-level SkillZip entry points used by the CLI and the evaluation driver.

compress_oneshot  -> Algorithm 1 (One-Shot SkillZip): scan, extract contract,
                     propose type-compatible reuse, select the shortest covering
                     explanation, render, and structurally audit.
zip_on_write      -> Algorithm 2 (continual): initialize on the warm-start skill
                     then integrate each accepted evolution patch, with periodic
                     repacking and auditing.

Both return a plain `Skill` (an ordinary, portable text artifact) plus
a report with token counts, the compression ratio, and the saving log. Neither
path reads tasks, rewards, trajectories, or verifiers (evaluation-free).
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .skill import Skill, approx_tokens
from . import extract, optimize, render, audit, online, refs
from .contract import Contract, Library, ZipState


DEFAULT_CFG = {
    "extract_llm": True,      # use the schema-constrained model for extraction
    "audit_llm": True,        # reparse with the model during the structural audit
    "audit": True,
    "top_k": 6,               # relation retrieval breadth per unit
    "min_sim": 0.12,          # very low lexical gate: within a hard blocking
                              # key, let the (semantic) relation checker decide
                              # equivalence -- lexical Jaccard alone misses
                              # paraphrases with different examples
    "min_conf": 0.55,         # min relation confidence to act on
    "canonicalize": True,     # per-unit minimal faithful phrasing (Eq. 5/13);
                              # host-verified: strictly shorter + literal-safe
    "wf_min_len": 2,          # min workflow fragment length for reuse
    "wf_min_occ": 2,          # min non-overlapping occurrences for reuse
    "repack_every": 4,        # Zip-on-Write: repack after B patches
    "repack_growth": 0.5,     # or when the contract grows by > rho
}


def _cfg(overrides: Optional[dict]) -> dict:
    c = dict(DEFAULT_CFG)
    if overrides:
        c.update(overrides)
    return c


def compress_oneshot(skill: Skill, cli=None, cfg: Optional[dict] = None
                     ) -> Tuple[Skill, Dict]:
    """One-Shot SkillZip (Algorithm 1)."""
    import re as _re
    cfg = _cfg(cfg)
    # YAML front-matter (name/description/...) is the skill-retrieval contract:
    # keep it verbatim on top of the output and exclude it from compression.
    fm = ""
    core = skill.body
    m = _re.match(r"^---\s*\n.*?\n---\s*\n", skill.body.lstrip(), _re.S)
    if m:
        fm = m.group(0)
        core = skill.body.lstrip()[m.end():]
    text = Skill(name=skill.name, body=core).to_markdown()
    log: List[dict] = []

    contract = extract.extract_contract(text, cli=cli, use_llm=cfg["extract_llm"])
    # keep anchored source headings on the units so intra-document
    # cross-references ("jump to 步骤8", "see §9.1") stay resolvable
    refs.attach_anchors(contract.all_units(), text)
    library = optimize.min_cost_cover(contract, cli=cli, cfg=cfg, log=log)
    body = render.render(library, name=skill.name)

    restored: List[str] = []
    if cfg["audit"]:
        source = Contract(units=contract.all_units(), residual=contract.residual)
        body, restored = audit.audit_and_restore(
            body, library, source, cli=cli, cfg={**cfg, "name": skill.name})

    if fm:
        body = fm.rstrip("\n") + "\n\n" + body.lstrip("\n")
    # anchors whose units were merged away are recovered from the source contract
    body = render.repair_anchors(body, contract.all_units())
    # a reference target that was dropped entirely is restored verbatim from the
    # source (the conservative recovery of the structural audit, applied to
    # reference integrity): a control transfer must never point nowhere
    still = refs.dangling_labels(body)
    recovered = []
    for label in still:
        span = refs.source_section(text, label)
        if span:
            body = body.rstrip() + "\n\n" + span + "\n"
            recovered.append(label)
    compressed = Skill(name=skill.name, body=body)
    # MDL feasibility: the all-verbatim representation (the original skill kept
    # as one locked residual) is always in H(S). If the structural rendering is
    # not strictly shorter, argmin selects the verbatim baseline -> never inflate
    # (the paper's conservative failure mode is under-compression).
    inflated = compressed.tokens >= skill.tokens
    if inflated:
        compressed = Skill(name=skill.name, body=skill.body)
    report = _report(skill, compressed, contract, library, log, restored,
                     mode="one_shot")
    report["selected_verbatim_baseline"] = bool(inflated)
    report["dangling_refs"] = refs.dangling_labels(compressed.body)
    report["anchors_restored"] = recovered
    return compressed, report


def zip_on_write(initial_text: str, patches: List[str], name: str, cli=None,
                 cfg: Optional[dict] = None,
                 reference: Optional[Skill] = None) -> Tuple[Skill, Dict, ZipState]:
    """Continual Zip-on-Write (Algorithm 2) over an ordered patch stream.

    `reference` is the uncompressed evolved skill S (seed + all patches); it is
    used only for the no-inflation guarantee and reporting, never during
    compression."""
    cfg = _cfg(cfg)
    state = online.zip_init(initial_text, name=name, cli=cli, cfg=cfg)
    body = online._with_fm(state, render.render(state.library, name=name))
    length_curve = [approx_tokens(body)]
    for patch in patches:
        if not (patch or "").strip():
            continue
        state, body = online.zip_update(state, patch, cli=cli, cfg=cfg)
        length_curve.append(approx_tokens(body))

    # Final checkpoint repack: local updates provide efficiency, periodic global
    # repacking recovers cross-patch reuse (paper Sec. 5.2 / Prop. 3). Evaluating
    # the artifact at a checkpoint naturally includes a repack, so short streams
    # that never hit the periodic trigger still get the long-range savings.
    if cfg.get("final_repack", True) and length_curve:
        state, body = online.zip_repack(state, cli=cli, cfg=cfg)
        length_curve.append(approx_tokens(body))

    compressed = Skill(name=name, body=body)
    verbatim = False
    if reference is not None and compressed.tokens >= reference.tokens:
        compressed = Skill(name=name, body=reference.body)
        verbatim = True
    report = {
        "mode": "zip_on_write",
        "name": name,
        "n_patches": len([p for p in patches if (p or '').strip()]),
        "final_units": len(state.library.units),
        "final_residual": len(state.library.residual),
        "compressed_tokens": compressed.tokens,
        "length_curve": length_curve,
        "selected_verbatim_baseline": verbatim,
        "ops": [entry for entry in state.log],
    }
    return compressed, report, state


def _report(original: Skill, compressed: Skill, contract: Contract,
            library: Library, log: List[dict], restored: List[str],
            mode: str) -> Dict:
    orig_tok = original.tokens
    comp_tok = compressed.tokens
    return {
        "mode": mode,
        "name": original.name,
        "original_tokens": orig_tok,
        "compressed_tokens": comp_tok,
        "remaining_ratio": round(comp_tok / max(1, orig_tok), 4),
        "extracted_units": len(contract.units),
        "required_units": len(contract.required()),
        "library_units": len(library.units),
        "procedures": len(library.procedures),
        "residual_units": len(library.residual),
        "restored_by_audit": restored,
        "savings": log,
    }
