"""Entry contracts: which resources must keep working when called on their own.

A progressively loaded bundle has one root document plus references, subskills,
scripts, and assets.  Compression is only safe if we know *how each file may be
entered*, because that decides what may be deleted from it.  Every markdown
resource therefore carries an **entry contract**:

``private``
    Reached only through the root.  Text the root already states may be removed,
    because the root is always loaded before this file is.

``public``
    An entry document the host agent can select directly.  It must keep working
    with nothing else loaded, so nothing a direct call needs may be removed --
    and, just as importantly, its filename must stay discoverable.

``conditional``
    Runnable on its own only when a declared host context holds.  It may lean on
    what an environment contract guarantees, but not on the root's prose.

The labels are never guessed from wording.  They come from two machine-checkable
facts in the bundle itself (front matter that a discovery scan would index, and
the bundle's own ``manifest.json``), and an explicit ``entrypoints.json`` always
wins so an author can declare them by hand.

This module also provides the measurements the two lifecycles are scored with:
the dependency closure of *any* entry, an entry's independence, and the tokens a
single direct call has to load.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Set

from ._text import (FRONT_MATTER_RE, LINK_RE, MANDATORY_RE, MD_SUFFIXES,
                    PLAIN_PATH_RE, entailed_units, is_present, norm,
                    read_markdown_text, tokens, units_of_file)

#: The three entry contracts of the paper (Section "Entry Contracts").
PUBLIC = "public"
PRIVATE = "private"
CONDITIONAL = "conditional"

#: Filenames a host skill-discovery scan treats as an entry document.
ENTRY_FILENAMES = ("SKILL.md", "SUBSKILL.md")


# --------------------------------------------------------------- entry contracts

def _front_matter(path: Path) -> Dict[str, str]:
    """Parse the leading ``---`` block into a flat key/value mapping."""
    if not path.is_file() or path.suffix.lower() not in MD_SUFFIXES:
        return {}
    match = FRONT_MATTER_RE.match(path.read_text(encoding="utf-8"))
    if not match:
        return {}
    out: Dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            out[key.strip()] = value.strip()
    return out


def entry_contracts(bundle: str) -> Dict[str, str]:
    """Label every markdown resource in ``bundle`` with its entry contract.

    Resolution order:

    1. ``entrypoints.json`` at the bundle root, if present, is authoritative.
       This is how an author declares contracts explicitly.
    2. Otherwise a file is ``public`` when it is an entry document
       (``SKILL.md``/``SUBSKILL.md``) whose front matter declares both ``name``
       and ``description`` -- exactly the pair a discovery scan indexes, so such
       a file can be invoked without its parent ever loading.
    3. It is ``conditional`` when the bundle's own ``manifest.json`` records that
       a complete task contract was moved into it (``moved_sections``), but it
       declares no agent-facing description: it can run once the host has
       supplied the request, yet the agent cannot route to it unaided.
    4. Everything else is ``private``.

    Returns a mapping of bundle-relative path to contract label.
    """
    root = Path(bundle).resolve()

    override = root / "entrypoints.json"
    if override.is_file():
        try:
            data = json.loads(override.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("contracts"):
                return dict(data["contracts"])
        except (json.JSONDecodeError, OSError):
            pass  # A malformed override must not silently mislabel the bundle.

    # Destinations that received a full task contract during compression.
    contract_targets: Set[str] = set()
    manifest = root / "manifest.json"
    if manifest.is_file():
        try:
            record = json.loads(manifest.read_text(encoding="utf-8"))
            for moved in record.get("moved_sections", []):
                into = str(moved.get("into", "")).strip()
                if into:
                    contract_targets.add(into)
        except (json.JSONDecodeError, OSError):
            pass

    labels: Dict[str, str] = {}
    for path in sorted(root.rglob("*.md")):
        rel = str(path.relative_to(root))
        if path.name.startswith(".") or "/." in "/" + rel:
            labels[rel] = PRIVATE          # hidden bookkeeping, never an entry
            continue
        front = _front_matter(path)
        if path.name in ENTRY_FILENAMES and front.get("name") and front.get("description"):
            labels[rel] = PUBLIC
        elif rel in contract_targets:
            labels[rel] = CONDITIONAL
        else:
            labels[rel] = PRIVATE
    return labels


def public_entrypoints(bundle: str) -> List[str]:
    """Paths that must remain independently callable after compression."""
    labels = entry_contracts(bundle)
    return sorted(rel for rel, cls in labels.items() if cls == PUBLIC)


def conditional_entrypoints(bundle: str) -> List[str]:
    """Paths callable on their own only under a declared host context."""
    labels = entry_contracts(bundle)
    return sorted(rel for rel, cls in labels.items() if cls == CONDITIONAL)


def write_entry_manifest(bundle: str) -> Path:
    """Freeze the derived labels into ``entrypoints.json`` beside the bundle.

    Useful before publishing: the labels a compression run was audited against
    become part of the artifact, so the decision is reviewable later.
    """
    root = Path(bundle).resolve()
    labels = entry_contracts(str(root))
    out = root / "entrypoints.json"
    out.write_text(json.dumps({"contracts": labels}, indent=1, sort_keys=True),
                   encoding="utf-8")
    return out


# ------------------------------------------------------- closures from any entry

def reachable_from(bundle: str, entry: str) -> Set[str]:
    """Dependency closure of one entry, following the links the agent follows.

    Starting at ``entry`` (not necessarily the root), walk local markdown links
    and bare in-bundle paths breadth-first.  Targets outside the bundle root and
    absent files are ignored, so the result is always a set of real, contained
    paths -- which is what makes it usable as a materialization list.
    """
    root = Path(bundle).resolve()
    if not (root / entry).exists():
        return set()

    visited = {entry}
    queue = [entry]
    while queue:
        current = queue.pop(0)
        current_path = root / current
        if not current_path.exists() or current_path.suffix.lower() not in MD_SUFFIXES:
            continue
        text = current_path.read_text(encoding="utf-8")
        targets = [m.group(2).split("#")[0].strip() for m in LINK_RE.finditer(text)]
        targets += [m.group(0) for m in PLAIN_PATH_RE.finditer(text)]
        for target in targets:
            if not target or target.startswith("http"):
                continue
            resolved = (current_path.parent / target).resolve()
            try:
                rel = str(resolved.relative_to(root))
            except ValueError:
                continue               # escapes the bundle: not a bundle edge
            if rel not in visited and resolved.exists():
                visited.add(rel)
                queue.append(rel)
    return visited


def resolve_entry(bundle: str, entry: str) -> Optional[str]:
    """Published path of a source entry, tolerating the ``SUBSKILL.md`` rename.

    Bundle compression may publish a nested ``SKILL.md`` as ``SUBSKILL.md`` to
    keep one entry document per directory.  Resolving the rename lets callers
    score it where it belongs -- as a loss of *discoverability*, not as a missing
    file.  Returns ``None`` when the entry is gone entirely.
    """
    root = Path(bundle).resolve()
    if (root / entry).is_file():
        return entry
    alternative = re.sub(r"SKILL\.md$", "SUBSKILL.md", entry)
    if (root / alternative).is_file():
        return alternative
    return None


# ------------------------------------------------------- independence measurement

def independence(source: str, published: str, entry: str,
                 env_contract: Optional[str] = None) -> Dict:
    """How much of an entry's own knowledge survives a *direct* call.

    We take the behaviour-bearing lines the source entry owns, then ask how many
    the agent can still see when it starts at that entry in ``published`` and
    follows links from there.  Text only the root can reach does not count,
    because a direct call never loads the root.

    Two failures are reported separately because they need different fixes:

    * ``discoverable`` is ``False`` when the entry path is gone or was renamed --
      the call cannot even start, so effective independence is ``0``.
    * ``independence`` is the content score for the entry that *does* start.

    The paper's effective independence is the product of the two.
    """
    source_root = Path(source).resolve()
    source_file = source_root / entry
    if not source_file.is_file():
        return {"entry": entry, "units": 0, "independence": 1.0,
                "discoverable": True, "note": "entry absent in source"}

    units = units_of_file(source_file)

    published_entry = resolve_entry(published, entry)
    if published_entry is None:
        # The entry vanished: nothing to start, nothing retained.
        return {"entry": entry, "units": len(units), "independence": 0.0,
                "discoverable": False, "renamed": False,
                "retained": 0, "lost": len(units), "entailed_removals": 0,
                "reachable_files": 0}

    renamed = published_entry != entry
    published_root = Path(published).resolve()
    closure = reachable_from(published, published_entry)
    haystack = read_markdown_text(published_root, closure)
    entailed = entailed_units(env_contract)

    retained, entailed_removed, lost = 0, 0, []
    for unit in units:
        unit_norm = norm(unit)
        if is_present(unit_norm, haystack):
            retained += 1
        elif any(is_present(unit_norm, e) or is_present(e, unit_norm) for e in entailed):
            entailed_removed += 1   # excused: the host guarantees this statement
        else:
            lost.append(unit)

    considered = len(units) - entailed_removed
    return {
        "entry": entry,
        "published_entry": published_entry,
        "discoverable": not renamed,
        "renamed": renamed,
        "units": len(units),
        "entailed_removals": entailed_removed,
        "retained": retained,
        "lost": len(lost),
        "independence": round(retained / considered, 4) if considered else 1.0,
        "lost_examples": lost[:4],
        "reachable_files": len(closure),
    }


def effective_independence(source: str, published: str, entry: str,
                           env_contract: Optional[str] = None) -> float:
    """Independence a direct call actually gets: content score x discoverability.

    This is the single number to watch when publishing persistently.  A bundle
    that keeps 93% of an entry's text but renames the file scores ``0.0`` here,
    which is the honest verdict: the call can never start.
    """
    report = independence(source, published, entry, env_contract)
    return round(float(report["independence"]) * (1.0 if report["discoverable"] else 0.0), 4)


# ------------------------------------------------------------------ cost measures

def entry_load_tokens(bundle: str, entry: str) -> Dict:
    """Tokens a single direct call to ``entry`` has to load.

    Charged: the entry document itself plus every *mandatory* dispatcher target
    (a shared module or capsule the entry is told to read before executing).
    Not charged: guarded topic branches, which a given run may never open.  This
    matches the path-cost convention used for bundles, so persistent and
    transient numbers stay comparable.
    """
    root = Path(bundle).resolve()
    published = resolve_entry(bundle, entry)
    if published is None:
        return {"entry": entry, "tokens": 0, "files": 0, "missing": True}

    charged = {published}
    text = (root / published).read_text(encoding="utf-8")
    for line in text.splitlines():
        if not MANDATORY_RE.search(line):
            continue
        for match in LINK_RE.finditer(line):
            target = match.group(2).split("#")[0].strip()
            resolved = ((root / published).parent / target).resolve()
            try:
                charged.add(str(resolved.relative_to(root)))
            except ValueError:
                continue

    total = 0
    for rel in charged:
        path = root / rel
        if path.is_file() and path.suffix.lower() in MD_SUFFIXES:
            total += tokens(path.read_text(encoding="utf-8"))
    return {"entry": entry, "tokens": total, "files": len(charged), "missing": False}


def bundle_bytes(bundle: str) -> int:
    """On-disk size of the whole bundle: the cost persistent mode reduces."""
    root = Path(bundle)
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


def bundle_digest(bundle: str) -> str:
    """Content digest over every file, path-sensitive and order-independent.

    Two uses: part of the transient view cache key, and proof that a transient
    run left the canonical bundle byte-identical.
    """
    root = Path(bundle).resolve()
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]
