"""The compression *lifecycle* axis: what happens to the result on disk.

SkillZip Pro separates two questions that are easy to conflate:

============================  ==================================================
Axis                          Choice
============================  ==================================================
**When** does compression run  One-Shot (compile a whole directory) or
                               Continual / Zip-on-Write (fold in each patch)
**Where does the result go**   **Persistent** (replace the shipped bundle) or
                               **Transient** (build a throwaway per-run view)
============================  ==================================================

The two axes are *orthogonal*: all four combinations are valid deployments.

======================  ==================================================
Combination             Fits
======================  ==================================================
One-Shot + Persistent   first migration of an evolved library to a shipped bundle
Continual + Persistent  a library that keeps evolving and is re-shipped
One-Shot + Transient    a stable bundle whose entries are called directly, rarely
Continual + Transient   frequent direct calls against a frequently edited bundle
======================  ==================================================

Both lifecycles call the **same compression kernel** (Pillar 1: activation-aware
cross-file compression; Pillar 2: cross-file routing preservation).  They differ
only at the kernel's output boundary:

===================  =============================  =========================
                     Persistent                     Transient
===================  =============================  =========================
May delete           text covered for *every*       text not needed for
                     public entry, permanently      *this* run, temporarily
Audit entry          all declared public entries    the one chosen entry
Publishes to         the shipped bundle on disk     a scratch or cached view
Cache                none (the result *is* it)      keyed by digest/entry/env
On failure           ship the verbatim bundle       load the verbatim closure
===================  =============================  =========================

Because the cost bases differ, **their ratios are never mixed**: persistent
reports disk and per-run cost; transient keeps disk unchanged by construction and
reports per-run context saving net of its build.

Typical use::

    from skillzip.lifecycle import (compress_persistent, audit_public_entries,
                                    build_execution_view, entry_contracts)

    # Persistent: publish a smaller bundle, keeping every public entry callable.
    report = compress_persistent("./my-skill", "./my-skill.compact")
    assert audit_public_entries("./my-skill", "./my-skill.compact")["ok"]

    # Transient: leave the bundle alone, shrink just this run.
    view = build_execution_view("./my-skill", "sub/SKILL.md")
    assert view["canonical_unchanged"]
    run_agent_on(view["view_dir"])
"""
from __future__ import annotations

from .contracts import (CONDITIONAL, PRIVATE, PUBLIC, bundle_bytes,
                        bundle_digest, conditional_entrypoints,
                        effective_independence, entry_contracts,
                        entry_load_tokens, independence, public_entrypoints,
                        reachable_from, resolve_entry, write_entry_manifest)
from .persistent import audit_public_entries, compress_persistent
from .transient import (build_execution_view, clear_view_cache,
                        default_cache_root, materialize_closure,
                        publish_transient, view_key, view_load_tokens)

__all__ = [
    # entry contracts
    "PUBLIC", "PRIVATE", "CONDITIONAL",
    "entry_contracts", "public_entrypoints", "conditional_entrypoints",
    "write_entry_manifest", "reachable_from", "resolve_entry",
    # measurement
    "independence", "effective_independence", "entry_load_tokens",
    "bundle_bytes", "bundle_digest",
    # persistent lifecycle
    "compress_persistent", "audit_public_entries",
    # transient lifecycle
    "build_execution_view", "publish_transient", "materialize_closure",
    "view_key", "view_load_tokens", "default_cache_root", "clear_view_cache",
]
