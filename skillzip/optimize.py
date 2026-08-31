"""Minimum-cost covering selection (paper Alg. 1 line 4, Sec. 4-5, Appendix A/B.5).

Given the extracted units, deterministically select the shortest representation
that still covers every required contract unit (Eq. 4):

    (K*, R*) = argmin  L(K) + L(R|K)   s.t.  a <= (K,R)  for all a in A_req(S).

Four type-specific decisions, all compared under the same length objective:
  * equivalent requirements  -> merge to one shared unit (Eq. 8)
  * repeated rules across scopes -> lift to nearest common ancestor (Eq. 9)
  * repeated workflows -> shared procedure via weighted set packing (Eq. 10)
  * guarded variants -> one common rule + guarded exceptions (Eq. 11)

Coverage is enforced after every selection (Proposition 1): a required unit is
never discarded, only re-expressed. Uncertain units fall to the locked residual.
No task access anywhere in this module.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from .contract import Contract, Library, Unit, Procedure, Exception_
from . import cost, relations, workflow, canon


class _DSU:
    def __init__(self, ids: List[str]):
        self.p = {i: i for i in ids}

    def find(self, x: str) -> str:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _shortest(units: List[Unit]) -> Unit:
    return min(units, key=lambda u: (cost.L_unit(u), u.id))


def _lexically_covers(rep: Unit, member: Unit) -> bool:
    """Deterministic witness required before a merge may DROP `member`: the
    representative must carry at least half of the member's content words.
    An LLM 'equivalence' verdict alone is a candidate signal, not a licence
    to delete text with disjoint content."""
    return _word_cover(rep.content, member.content) >= 0.5


def _word_cover(container: str, contained: str) -> float:
    cw = {w for w in re.findall(r"[a-z0-9]+", (contained or "").lower())
          if len(w) > 2}
    if not cw:
        return 1.0
    rw = {w for w in re.findall(r"[a-z0-9]+", (container or "").lower())
          if len(w) > 2}
    return len(cw & rw) / len(cw)


def _synthesize_merge(members: List[Unit], cli) -> Optional[str]:
    """Eq. (8) with a composed shared unit z: when no member can stand in for
    the whole cluster, ask the compressor for ONE minimal phrasing covering
    every member, and accept it only under deterministic witnesses -- >=60%
    word coverage of EACH member, union of protected literals preserved, and
    negation polarity kept. Rejection keeps the cluster unmerged."""
    from .canon import protected_literals, _NEG_RE
    if cli is None or getattr(cli, "backend", "mock") == "mock":
        return None
    joined = "\n".join(f"- {m.content}" for m in members)
    try:
        z = (cli.chat(
            "Merge these overlapping requirement variants of an agent skill "
            "into ONE minimal instruction that preserves EVERY distinct "
            "requirement, condition, and exact literal (backticks, quotes, "
            "templates, variable names, numbers). No new information. Keep the "
            "variants' original language (never translate). "
            "Return only the merged instruction text.\n\n" + joined,
            temperature=0.0) or "").strip().lstrip("-* ").strip()
    except Exception:
        return None
    if not z or "\n" in z:
        return None
    if cost.token_len(z) >= sum(cost.token_len(m.content) for m in members):
        return None
    for m in members:
        if _word_cover(z, m.content) < 0.6:
            return None
        for lit in protected_literals(m.content):
            if lit not in z:
                return None
        if _NEG_RE.search(m.content) and not _NEG_RE.search(z):
            return None
    return z


def _fold_root_guards(units: List[Unit]) -> None:
    """Root-level guards must not be re-emitted verbatim per line at render
    time. A guard shared by >=2 units becomes a 'when-' child scope (rendered
    ONCE as a '### When ...' branch header); a unique guard is folded into the
    content so canonicalization can compress the condition with the rule.
    Branch-scoped guards stay untouched."""
    foldable = [u for u in units
                if (u.guard or "").strip()
                and not any(s.startswith("when-") for s in u.scope)]
    by_guard: Dict[str, List[Unit]] = {}
    for u in foldable:
        by_guard.setdefault(" ".join((u.guard or "").lower().split()), []).append(u)
    for g, group in by_guard.items():
        # shared condition -> ONE guarded branch header (paper's scope tree)
        # instead of re-stating the guard inline on every line; the renderer
        # emits '### When ...' branches for rule/output/tool units
        branchable = [u for u in group if u.type == "rule"]
        if len(branchable) >= 2:
            # drop a leading conjunction: the renderer's branch header already
            # says 'When ...'
            gg = re.sub(r"(?i)^\s*(if|when|whenever|in case|only if)\b\s*", "", g)
            slug = "when-" + re.sub(r"[^a-z0-9]+", "-", gg or g).strip("-")
            for u in branchable:
                u.scope = tuple(u.scope) + (slug,)
                u.guard = ""
        for u in group:
            if not (u.guard or "").strip():
                continue
            cw = {w for w in re.findall(r"[a-z0-9_]+", (u.content or "").lower())
                  if len(w) > 2}
            gw = [w for w in re.findall(r"[a-z0-9_]+", g) if len(w) > 2]
            if gw and sum(1 for w in gw if w in cw) / len(gw) >= 0.6:
                u.guard = ""             # condition already stated in content
                continue
            prefix = "" if re.match(
                r"(?i)\s*(if|when|unless|whenever|in case|only if|for)\b", g) \
                else "when "
            u.content = f"{u.content} ({prefix}{u.guard.strip()})"
            u.guard = ""


def min_cost_cover(contract: Contract, cli=None, cfg: Optional[dict] = None,
                   log: Optional[List[dict]] = None) -> Library:
    cfg = cfg or {}
    lib = Library()
    log = log if log is not None else []
    # optional per-stage LLM gating (default on): allows a fast, cheap path that
    # keeps schema-constrained extraction but runs relation/merge/canonicalize
    # deterministically -- essential for many repeated compressions (Zip-on-Write).
    rel_cli = cli if cfg.get("relation_llm", True) else None
    merge_cli = cli if cfg.get("merge_llm", True) else None
    canon_cli = cli if cfg.get("canon_llm", True) else None

    units = [u for u in contract.units]
    # locked residual passes through untouched (verbatim, undeletable)
    lib.residual = [u for u in contract.residual]

    # A unit flagged ``locked`` is verbatim and undeletable by definition, so it
    # must bypass every lossy stage (hash dedup, equivalence/implication
    # clustering, evidence folding, canonicalization, scope lifting).  Previously
    # only ``contract.residual`` honoured the flag, so a locked *unit* could still
    # be merged away.  That is unsound for units whose surface form carries
    # structure rather than prose -- most importantly a control transfer such as a
    # progressive-loading link, where two near-identical sentences point at
    # DIFFERENT targets and merging them silently destroys reachability.
    locked_units = [u for u in units if u.locked]
    units = [u for u in units if not u.locked]

    # index for coverage bookkeeping: every required unit must end covered
    required_ids = {u.id for u in units if u.is_required()}
    covered_by: Dict[str, str] = {}      # dropped unit id -> surviving unit id
    survivors: Dict[str, Unit] = {u.id: u for u in units}

    # ---- 1. exact-hash dedup (Eq. 12 hash merge) --------------------------
    by_hash: Dict[str, List[Unit]] = {}
    for u in units:
        by_hash.setdefault(u.hash, []).append(u)
    for h, group in by_hash.items():
        if len(group) > 1:
            keep = _shortest(group)
            for u in group:
                if u.id != keep.id and u.id in survivors:
                    del survivors[u.id]
                    covered_by[u.id] = keep.id
                    log.append({"candidate": f"exact:{keep.id}", "op": "dedup",
                                "dropped": u.id, "saving_tokens": cost.L_unit(u)})

    # ---- 2. equivalence / implication clustering (Eq. 8) ------------------
    live = list(survivors.values())
    pairs = relations.retrieve_pairs(live, top_k=cfg.get("top_k", 4),
                                     min_sim=cfg.get("min_sim", 0.45),
                                     max_pairs=cfg.get("max_pairs", 10 ** 9))
    dsu = _DSU([u.id for u in live])
    exceptions: List[Tuple[str, Unit]] = []      # (core-id, variant unit -> delta)
    for a, b in pairs:
        if a.id not in survivors or b.id not in survivors:
            continue
        label, conf = relations.relation(a, b, cli=rel_cli)
        if conf < cfg.get("min_conf", 0.55):
            continue
        if label == relations.EQUIV:
            dsu.union(a.id, b.id)
        elif label == relations.LIMPL:      # a stronger -> a covers b
            dsu.union(a.id, b.id)
        elif label == relations.RIMPL:      # b stronger -> b covers a
            dsu.union(b.id, a.id)
        elif label == relations.CONFLICT:
            # guarded variants: fold into common rule + exception (Eq. 11)
            exceptions.append((a.id, b))

    clusters: Dict[str, List[Unit]] = {}
    for u in live:
        clusters.setdefault(dsu.find(u.id), []).append(u)
    for root, members in clusters.items():
        if len(members) < 2:
            continue
        # representative = the member able to stand in for the most others
        # under the deterministic lexical-cover witness; ties broken by Eq. 8's
        # argmin (shorter first). Uncoverable members always survive.
        def _covered(rep: Unit) -> List[Unit]:
            return [m for m in members if m.id != rep.id
                    and _lexically_covers(rep, m)]
        rep = max(members,
                  key=lambda u: (len(_covered(u)), -cost.L_unit(u), u.id))
        others = _covered(rep)
        if len(others) < len(members) - 1:
            # no member stands in for the whole cluster: try a composed shared
            # unit z (Eq. 8) under deterministic coverage/literal witnesses
            z = _synthesize_merge(members, merge_cli)
            if z is not None:
                rep.content = z
                others = [m for m in members if m.id != rep.id]
                log.append({"candidate": f"synth:{rep.id}", "op": "merge-synth",
                            "covered_units": [m.id for m in others],
                            "saving_tokens": 0})
        if not others:
            continue
        saving = cost.test_equivalent(rep, [rep] + others, residuals=[])
        if saving > 0:
            for m in others:
                if m.id in survivors:
                    del survivors[m.id]
                    covered_by[m.id] = rep.id
            # rep absorbs the union of provenance so audit can trace it
            rep.provenance = sorted(set(sum((m.provenance for m in [rep] + others), [])))
            log.append({"candidate": f"equiv:{rep.id}", "op": "merge",
                        "covered_units": [m.id for m in others],
                        "saving_tokens": saving})

    # ---- 3. common rule + guarded exceptions (Eq. 11) ---------------------
    for core_id, variant in exceptions:
        core = survivors.get(core_id)
        if core is None or variant.id not in survivors:
            continue
        delta = Exception_(guard=variant.guard or "exception", content=variant.content)
        saving = cost.test_common_rule_exceptions(core, [delta], [core, variant])
        if saving > 0:
            core.exceptions.append(delta)
            del survivors[variant.id]
            covered_by[variant.id] = core.id
            log.append({"candidate": f"exception:{core.id}", "op": "exception",
                        "covered_units": [variant.id], "saving_tokens": saving})

    # ---- 3b. evidence pruning: evidence is removable exactly when the
    # requirement it expresses is represented elsewhere (Eq. 3) -------------
    kept_req = [u for u in survivors.values() if u.type != "evidence"]
    for u in [x for x in survivors.values() if x.type == "evidence"]:
        cover = next((k for k in kept_req
                      if relations.jaccard(u.content, k.content) >= 0.7), None)
        if cover is not None:
            del survivors[u.id]
            covered_by[u.id] = cover.id
            log.append({"candidate": f"evidence:{u.id}", "op": "evidence-drop",
                        "saving_tokens": cost.L_unit(u)})

    # ---- 3c. canonical minimal phrasing per selected unit (Eq. 5/13):
    # shrink the code length of each survivor's content; the host accepts a
    # rewrite only if strictly shorter and literal/polarity/guard-safe --------
    if cfg.get("canonicalize", True):
        _fold_root_guards(list(survivors.values()))
        canon.canonicalize_units(list(survivors.values()), cli=canon_cli, log=log)

    # ---- 4. scope lifting via bottom-up DP (Eq. 9, B.5) -------------------
    _scope_lift(survivors, covered_by, log)

    # ---- 5. workflow reuse: weighted set packing (Eq. 10, B.5) ------------
    procs = _pack_workflows(list(survivors.values()), log, cfg)
    lib.procedures = procs

    # ---- 6. assemble + assert coverage (Proposition 1) --------------------
    lib.units = list(survivors.values())
    _assert_coverage(required_ids, survivors, covered_by, contract, lib, log)
    # locked units re-enter verbatim after every lossy stage
    lib.units.extend(locked_units)
    return lib


def _common_ancestor(scopes: List[tuple]) -> tuple:
    if not scopes:
        return ("root",)
    common = list(scopes[0])
    for s in scopes[1:]:
        i = 0
        while i < len(common) and i < len(s) and common[i] == s[i]:
            i += 1
        common = common[:i]
    return tuple(common) if common else ("root",)


def _scope_lift(survivors: Dict[str, Unit], covered_by: Dict[str, str],
                log: List[dict]) -> None:
    """A repeated rule occurring in >=2 sibling child scopes is lifted to their
    closest common ancestor when Eq. (9) holds and every occurrence agrees
    (no unencoded conflict). Lifting keeps one copy at the ancestor."""
    rules = [u for u in survivors.values() if u.type == "rule"]
    fam: Dict[Tuple[str, str, str], List[Unit]] = {}
    for u in rules:
        fam.setdefault((u.norm, u.modality, u.guard.strip().lower()), []).append(u)
    for key, copies in fam.items():
        if len(copies) < 2:
            continue
        scopes = [c.scope for c in copies]
        if len(set(scopes)) < 2:
            continue                      # same scope -> handled by dedup/equiv
        anc = _common_ancestor(scopes)
        keep = _shortest(copies)
        lifted = Unit(id=keep.id, type="rule", scope=anc, content=keep.content,
                      modality=keep.modality, guard=keep.guard,
                      provenance=sorted(set(sum((c.provenance for c in copies), []))))
        saving = cost.test_scope_lift(lifted, copies)
        if saving > 0:
            for c in copies:
                if c.id in survivors:
                    del survivors[c.id]
                    if c.id != keep.id:
                        covered_by[c.id] = keep.id
            survivors[keep.id] = lifted
            log.append({"candidate": f"scope-lift:{keep.id}", "op": "lift",
                        "scope": list(anc),
                        "covered_units": [c.id for c in copies if c.id != keep.id],
                        "saving_tokens": saving})


def _pack_workflows(units: List[Unit], log: List[dict],
                    cfg: dict) -> List[Procedure]:
    """Weighted set packing over repeated workflow fragments: greedily select by
    saving-per-covered-token, keeping occurrences non-overlapping, then a
    pairwise-exchange refinement (B.5)."""
    cands = workflow.mine_repeats(units, min_len=cfg.get("wf_min_len", 2),
                                  min_occ=cfg.get("wf_min_occ", 2))
    scored = []
    for i, c in enumerate(cands):
        proc = workflow.make_procedure(f"proc{i+1}", c)
        saving = cost.test_workflow_reuse(proc, len(c["occurrences"]))
        if saving <= 0:
            continue
        covered = set(uid for grp in c["occ_units"] for uid in grp)
        per_tok = saving / max(1, len(covered))
        scored.append((per_tok, saving, covered, proc, c))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    used: set = set()
    chosen: List[Procedure] = []
    for per_tok, saving, covered, proc, c in scored:
        if covered & used:
            continue                      # keep occurrences non-overlapping
        used |= covered
        chosen.append(proc)
        log.append({"candidate": f"workflow:{proc.name}",
                    "op": "procedure", "covered_units": sorted(covered),
                    "saving_tokens": saving})
    return chosen


def _assert_coverage(required_ids, survivors, covered_by, contract: Contract,
                     lib: Library, log: List[dict]) -> None:
    """Every required unit must be directly present, structurally covered by a
    survivor/procedure, or preserved verbatim; otherwise restore it (locked)."""
    proc_covered = {uid for p in lib.procedures for uid in p.unit_ids}
    for rid in required_ids:
        if rid in survivors:
            continue
        # follow the covered_by chain to a survivor
        seen = set()
        cur = rid
        ok = False
        while cur in covered_by and cur not in seen:
            seen.add(cur)
            cur = covered_by[cur]
            if cur in survivors:
                ok = True
                break
        if ok or rid in proc_covered:
            continue
        # conservative recovery: restore the original unit as locked residual
        orig = contract.get(rid)
        if orig is not None:
            restored = Unit.from_json(orig.to_json())
            restored.locked = True
            restored.exceptions = []
            lib.residual.append(restored)
            log.append({"candidate": f"restore:{rid}", "op": "restore-coverage",
                        "saving_tokens": 0})
