"""SkillZip Pro: execution-aware compression of progressively loaded agent skills.

A production agent skill is rarely one prompt.  It is a **bundle**: a root
``SKILL.md`` loaded at activation, plus references, schemas, scripts, assets, and
nested subskills that are disclosed only along the execution path that needs
them.  SkillZip Pro compresses that whole bundle while leaving the agent harness
untouched -- the output is still an ordinary directory with ordinary relative
links, so no resolver, hook, or protocol change is required.

Compression is **evaluation-free**: it reads only the skill text, never tasks,
rewards, trajectories, or verifiers.  It also **never inflates**: the verbatim
bundle is always a candidate, so the conservative failure mode is
under-compression rather than corruption.

Two pillars (the shared compression kernel)
-------------------------------------------
1. **Activation-aware cross-file compression.**  Remove text in a reference or
   subskill that the root or the declared environment already covers; factor text
   repeated across branches into a shared module placed *inside the activation
   scope that loads it*; move long guarded branches into on-demand capsules.
2. **Cross-file routing preservation.**  Treat the routing table as a locked,
   first-class object, keep every reference line through rewriting, and re-read
   the materialized directory to prove every file is still reachable before
   publishing.  If a branch went dark, the candidate is rejected.

Two independent deployment axes
-------------------------------
* *When* to compress -- ``compress_oneshot`` / ``compress_bundle`` (one-shot) or
  ``zip_on_write`` (continual, patch by patch).
* *Where the result goes* -- ``skillzip.lifecycle``: **persistent** (rewrite and
  republish the bundle) or **transient** (leave it byte-identical and build a
  throwaway per-run execution view).

Public entry points
-------------------
::

    from skillzip import compress_oneshot, zip_on_write, compress_bundle

    compress_oneshot(skill)                   # Algorithm 1: one document
    zip_on_write(initial_text, patches, name)  # Algorithm 2: continual
    compress_bundle(root, output)              # bundle compiler (one-shot)

    from skillzip.lifecycle import compress_persistent, build_execution_view

    compress_persistent(root, output)          # persistent + multi-entry audit
    build_execution_view(root, "sub/SKILL.md") # transient per-run view

Module layout
-------------
Single-document core: ``scanner`` (markdown blocks with stable provenance),
``extract`` (schema-constrained contract recovery), ``relations`` (typed
retrieval and relation checks), ``workflow`` (workflow graph + repeated-sequence
mining), ``optimize`` (minimum-cost covering selection), ``cost`` (length model),
``canon`` (minimal faithful phrasing), ``render`` (template rendering), ``audit``
(contract diff + conservative recovery), ``refs`` (anchor integrity), ``online``
(Zip-on-Write + write-ahead log), ``contract`` (typed data model).

Bundle layer: ``bundle`` (safe recursive resource graph), ``bundle_api`` (the
bundle compiler and atomic fallback), ``bundle_audit`` (cross-file coverage and
reference audit), ``bundle_cost`` (catalog/activation/deployment/path costs),
``capsules`` (conditional section splitting), ``environment`` (strict
host-entailment witnesses).

Lifecycle layer: ``lifecycle`` (entry contracts, persistent publishing with the
multi-entry audit, transient execution views with digest-keyed caching).

Supporting: ``skill`` (the Skill artifact), ``llm`` (the only model-backed
dependency), ``prompts``, ``cli``.
"""
from .api import compress_oneshot, zip_on_write, DEFAULT_CFG
from .bundle_api import (compress_bundle, DEFAULT_BUNDLE_CFG,
                         BundleCompressionError)
from .contract import Contract, Library, Unit, ZipState

__version__ = "2.0.0-pro"

__all__ = ["compress_oneshot", "zip_on_write", "DEFAULT_CFG",
           "compress_bundle", "DEFAULT_BUNDLE_CFG", "BundleCompressionError",
           "Contract", "Library", "Unit", "ZipState", "__version__"]
