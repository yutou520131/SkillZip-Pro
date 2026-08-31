"""Transient execution-view compression: keep the bundle, shrink the run.

Transient mode leaves the canonical bundle **byte-for-byte unchanged**.  Just
before a run it builds a small, throwaway *execution view* containing only what
that run needs: the chosen entry, its dependency closure, the root (for the
catalogue and host-level contract), and their shared dependencies -- compressed
together by the same kernel and re-rooted at the entry so the run can start
immediately.  Afterwards the view is discarded, or cached under
``(bundle digest, entry, environment digest)`` and reused until one of those
changes.

What this buys and what it costs:

* It never edits the shipped files, so a directly callable entry keeps its
  independence *for free* -- there is nothing to audit across entries.
* It does not reduce disk size.  The saving is per-run context only, and it must
  be reported net of the build: see ``raw_closure_tokens`` vs ``view_tokens``
  alongside ``build_ms``.
* It always ends in something runnable.  If the compressed view is unusable, not
  smaller, or fails its single-entry audit, the raw closure ships instead
  (``fallback = True``): a transient run can lose the saving, never the ability
  to execute.

This module implements Algorithm 1 of the paper.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Dict, Optional

from ._text import LINK_RE, MANDATORY_RE, MD_SUFFIXES, tokens
from .contracts import bundle_digest, reachable_from

#: Default cache location.  Overridable per call, or via ``SKILLZIP_VIEW_CACHE``.
DEFAULT_CACHE_DIRNAME = ".skillzip_view_cache"

#: Reserved name for the original root when another entry is promoted to root.
HOST_CONTEXT_NAME = "_host_context.md"

#: Per-view metadata written inside the cached view.
VIEW_META_NAME = ".view_meta.json"


def default_cache_root() -> Path:
    """Where views are cached when the caller does not say.

    Resolution order: ``SKILLZIP_VIEW_CACHE`` environment variable, else
    ``.skillzip_view_cache`` under the current working directory.  No absolute
    path is baked into the package.
    """
    env = os.environ.get("SKILLZIP_VIEW_CACHE", "").strip()
    return Path(env).expanduser() if env else Path.cwd() / DEFAULT_CACHE_DIRNAME


def view_key(source: str, entry: str, environment_contract: Optional[str] = None) -> str:
    """Cache key for one (bundle, entry, environment) triple.

    All three parts matter: editing any bundle file, calling a different entry, or
    changing the environment contract must all invalidate the cached view.
    """
    env_digest = "none"
    if environment_contract and Path(environment_contract).is_file():
        env_digest = hashlib.sha256(
            Path(environment_contract).read_bytes()).hexdigest()[:12]
    raw = f"{bundle_digest(source)}|{entry}|{env_digest}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _reroot_links(markdown: Path, old_parent: Path) -> None:
    """Rewrite relative links after an entry document is promoted to view root."""
    text = markdown.read_text(encoding="utf-8")

    def fix(match):
        target = match.group(2).strip()
        if target.startswith(("http", "/", "#")):
            return match.group(0)
        joined = os.path.normpath(str(old_parent / target))
        return f"[{match.group(1)}]({joined})"

    markdown.write_text(LINK_RE.sub(fix, text), encoding="utf-8")


def materialize_closure(source: str, entry: str, view_dir: Path) -> Dict:
    """Copy {entry closure, root, shared deps} into a tree rooted at ``entry``.

    The root document is included on purpose: it carries the catalogue and the
    host-level contract a direct call would otherwise miss, and its presence is
    what lets the kernel factor the entry *against* the root instead of guessing.
    When the entry is not already the root it is promoted to ``SKILL.md`` and the
    original root is kept as ``_host_context.md``, linked as mandatory reading, so
    no host-level obligation is lost.
    """
    src = Path(source).resolve()
    if view_dir.exists():
        shutil.rmtree(view_dir)
    view_dir.mkdir(parents=True)

    closure = set(reachable_from(source, entry))
    closure.add(entry)
    root_closure = set(reachable_from(source, "SKILL.md"))
    shared = closure & root_closure

    # Markdown of the closure, plus the root itself.
    for rel in sorted(closure | {"SKILL.md"}):
        src_file = src / rel
        if not src_file.is_file():
            continue
        dest = view_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dest)

    # Non-markdown assets (scripts, schemas, data) are copied verbatim.
    for rel in sorted(closure):
        src_file = src / rel
        if src_file.is_file() and src_file.suffix.lower() not in MD_SUFFIXES:
            dest = view_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dest)

    entry_path = view_dir / entry
    if entry != "SKILL.md" and entry_path.is_file():
        host = view_dir / HOST_CONTEXT_NAME
        shutil.move(str(view_dir / "SKILL.md"), str(host))
        rel_host = os.path.relpath(host, entry_path.parent)
        text = entry_path.read_text(encoding="utf-8").rstrip("\n")
        text += (f"\n\n## Host context\n"
                 f"Before executing this module, read [host context]({rel_host}).\n")
        entry_path.write_text(text, encoding="utf-8")
        shutil.move(str(entry_path), str(view_dir / "SKILL.md"))
        if len(Path(entry).parts) - 1:
            # The entry moved up directories, so its own relative links must move.
            _reroot_links(view_dir / "SKILL.md", Path(entry).parent)

    return {"closure_files": len(closure), "shared_files": len(shared)}


def view_load_tokens(view_dir: Path) -> int:
    """Tokens a run starting at this view's root has to load.

    Same convention as ``entry_load_tokens`` for bundles: the root document plus
    every mandatory dispatcher target, so view and bundle costs are comparable.
    """
    root = Path(view_dir).resolve()
    start = root / "SKILL.md"
    if not start.is_file():
        return 0

    charged = {"SKILL.md"}
    for line in start.read_text(encoding="utf-8").splitlines():
        if not MANDATORY_RE.search(line):
            continue
        for match in LINK_RE.finditer(line):
            target = match.group(2).split("#")[0].strip()
            resolved = (start.parent / target).resolve()
            try:
                charged.add(str(resolved.relative_to(root)))
            except ValueError:
                continue

    total = 0
    for rel in charged:
        path = root / rel
        if path.is_file() and path.suffix.lower() in MD_SUFFIXES:
            total += tokens(path.read_text(encoding="utf-8"))
    return total


def build_execution_view(source: str, entry: str,
                         environment_contract: Optional[str] = None,
                         cache: bool = True,
                         cache_root: Optional[Path] = None,
                         cli=None) -> Dict:
    """Build (or fetch) one task-specific execution view for a direct call.

    Args:
        source: Canonical bundle.  Hashed before and after, and never written to.
        entry: Bundle-relative entry the agent selected.
        environment_contract: Optional environment contract JSON.
        cache: Reuse a cached view when the key matches (a warm read costs ~1 ms
            against a cold build of a few hundred ms).
        cache_root: Where to store views; defaults to :func:`default_cache_root`.
        cli: Optional model client; ``None`` keeps the build deterministic.

    Returns:
        Metadata including ``view_dir`` (hand this directory to the agent),
        ``cache_hit``, ``fallback``, ``raw_closure_tokens``, ``view_tokens``,
        ``build_ms``, and ``canonical_unchanged`` -- the proof that the shipped
        bundle was not touched.
    """
    cache_root = Path(cache_root) if cache_root else default_cache_root()
    key = view_key(source, entry, environment_contract)
    cached = cache_root / key
    digest_before = bundle_digest(source)

    started = time.time()

    # ---- warm path: no kernel call at all -----------------------------------
    if cache and (cached / "SKILL.md").is_file():
        meta_path = cached / VIEW_META_NAME
        meta = {}
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                meta = {}
        meta.update({
            "cache_hit": True,
            "build_ms": round((time.time() - started) * 1000, 2),
            "view_dir": str(cached),
            "canonical_unchanged": bundle_digest(source) == digest_before,
        })
        return meta

    # ---- cold path: materialize the closure, then compress it ---------------
    scratch = cache_root / f".build_{key}"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True, exist_ok=True)

    raw = scratch / "raw"
    info = materialize_closure(source, entry, raw)
    raw_tokens = view_load_tokens(raw)

    compressed = scratch / "view"
    from .persistent import _run_kernel      # same kernel, different boundary
    report = _run_kernel(str(raw), str(compressed), environment_contract,
                         {"promote_cross_file": True}, cli)
    built = (compressed / "SKILL.md").is_file()
    view_tokens = view_load_tokens(compressed) if built else None

    # Safety fallback: an unusable or non-improving view is never shipped.
    audit_ok = bool(report.get("audit_ok", False))
    fallback = (view_tokens is None) or (not audit_ok) or (view_tokens >= raw_tokens)
    final_source = raw if fallback else compressed

    if cached.exists():
        shutil.rmtree(cached)
    cached.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(final_source, cached)

    meta = {
        "entry": entry,
        "cache_key": key,
        "cache_hit": False,
        "fallback": bool(fallback),
        "audit_ok": audit_ok,
        "closure_files": info["closure_files"],
        "shared_files": info["shared_files"],
        "raw_closure_tokens": raw_tokens,
        "view_tokens": view_load_tokens(cached),
        "kernel_calls": report.get("kernel_calls", 0),
        "build_ms": round((time.time() - started) * 1000, 2),
        "view_dir": str(cached),
    }
    (cached / VIEW_META_NAME).write_text(json.dumps(meta, indent=1), encoding="utf-8")
    shutil.rmtree(scratch, ignore_errors=True)

    # The canonical bundle must be untouched: this is the mode's core promise.
    meta["canonical_unchanged"] = bundle_digest(source) == digest_before
    return meta


def publish_transient(source: str, output: str) -> Dict:
    """Publish the canonical bundle unchanged, as transient mode requires.

    Publishing a verbatim copy is not a formality -- it *is* the mode: disk size
    does not move, and each run pays a build (or a cache read) for its context
    reduction via :func:`build_execution_view`.
    """
    out_path = Path(output)
    if out_path.exists():
        shutil.rmtree(out_path)
    shutil.copytree(source, out_path)
    return {"lifecycle": "transient", "verbatim_canonical": True,
            "entry_labels_used": True, "kernel_calls": 0,
            "canonical_digest": bundle_digest(source)}


def clear_view_cache(cache_root: Optional[Path] = None) -> int:
    """Delete every cached view.  Returns how many were removed.

    Views are pure derived artifacts keyed by content digest, so dropping them is
    always safe; the next call simply rebuilds.
    """
    cache_root = Path(cache_root) if cache_root else default_cache_root()
    if not cache_root.is_dir():
        return 0
    removed = 0
    for child in sorted(cache_root.iterdir()):
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
    return removed
