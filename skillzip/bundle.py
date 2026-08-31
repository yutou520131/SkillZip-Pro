"""Phase-A skill bundle model and safe recursive resource resolver.

The original SkillZip treats a skill as one Markdown string.  Phase A keeps the
same agent-facing artifact (``SKILL.md`` plus ordinary local files), but gives
the compressor a bundle graph.  Resolution is deliberately stricter than a
runtime loader: it never follows a path outside the selected bundle root and it
does not fetch network resources.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote

from .scanner import reference_occurrences


MARKDOWN_SUFFIXES = {".md", ".markdown"}
TEXT_SUFFIXES = MARKDOWN_SUFFIXES | {
    ".txt", ".json", ".yaml", ".yml", ".toml", ".csv", ".tsv",
    ".py", ".sh", ".bash", ".js", ".ts", ".sql", ".xml", ".html",
    ".css", ".svg",
}
SCRIPT_SUFFIXES = {".py", ".sh", ".bash", ".js", ".ts", ".sql"}
IGNORED_PARTS = {".git", "__pycache__", ".skillzip_cache", ".DS_Store"}


def _posix(path: Path) -> str:
    return PurePosixPath(path).as_posix()


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _read_text(path: Path) -> Optional[str]:
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def classify_resource(path: Path, root_file: Path) -> str:
    if path == root_file:
        return "entry_skill"
    if path.name.lower() == "skill.md":
        return "subskill"
    suffix = path.suffix.lower()
    if suffix in MARKDOWN_SUFFIXES:
        return "reference"
    if suffix in SCRIPT_SUFFIXES:
        return "script"
    if suffix in TEXT_SUFFIXES:
        return "data"
    return "asset"


@dataclass
class BundleNode:
    path: str
    kind: str
    size_bytes: int
    text: Optional[str] = None
    reachable: bool = False
    generated: bool = False
    output_path: str = ""

    @property
    def is_markdown(self) -> bool:
        return PurePosixPath(self.path).suffix.lower() in MARKDOWN_SUFFIXES

    def to_json(self) -> dict:
        data = asdict(self)
        if self.text is not None:
            data["text_sha256"] = hashlib.sha256(self.text.encode()).hexdigest()
        data.pop("text", None)
        return data


@dataclass
class ReferenceEdge:
    source: str
    raw_target: str
    target: str = ""
    fragment: str = ""
    condition: str = "on_demand_unspecified"
    line: int = 0
    syntax: str = "plain"
    status: str = "local"  # local|external|missing|path_escape|directory

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class BundleGraph:
    root_dir: str
    entry: str
    nodes: Dict[str, BundleNode] = field(default_factory=dict)
    edges: List[ReferenceEdge] = field(default_factory=list)
    cycles: List[List[str]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def local_edges(self) -> List[ReferenceEdge]:
        return [e for e in self.edges if e.status == "local" and e.target]

    def to_json(self) -> dict:
        return {
            "root_dir": self.root_dir,
            "entry": self.entry,
            "nodes": [self.nodes[k].to_json() for k in sorted(self.nodes)],
            "edges": [e.to_json() for e in self.edges],
            "cycles": self.cycles,
            "warnings": self.warnings,
            "errors": self.errors,
        }


def _iter_bundle_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in IGNORED_PARTS or part.startswith(".skillzip_tmp")
               for part in rel.parts):
            continue
        yield path


def _resolve_target(source_abs: Path, target: str, root: Path) -> Tuple[str, Path]:
    clean = unquote((target or "").split("?", 1)[0]).strip()
    candidate = (source_abs.parent / clean).resolve(strict=False)
    if not _within(candidate, root):
        return "path_escape", candidate
    if candidate.is_dir():
        nested = candidate / "SKILL.md"
        if nested.is_file():
            return "local", nested.resolve()
        return "directory", candidate
    if not candidate.is_file():
        return "missing", candidate
    real = candidate.resolve()
    if not _within(real, root):
        return "path_escape", real
    return "local", real


def _find_cycles(graph: BundleGraph) -> List[List[str]]:
    adj: Dict[str, List[str]] = {path: [] for path in graph.nodes}
    for edge in graph.local_edges():
        if edge.source in adj and edge.target in adj:
            adj[edge.source].append(edge.target)
    found: List[List[str]] = []
    visiting: List[str] = []
    state: Dict[str, int] = {}

    def walk(node: str) -> None:
        state[node] = 1
        visiting.append(node)
        for nxt in sorted(set(adj.get(node, []))):
            if state.get(nxt, 0) == 0:
                walk(nxt)
            elif state.get(nxt) == 1:
                start = visiting.index(nxt)
                cycle = visiting[start:] + [nxt]
                if cycle not in found:
                    found.append(cycle)
        visiting.pop()
        state[node] = 2

    for node in sorted(adj):
        if state.get(node, 0) == 0:
            walk(node)
    return found


def resolve_bundle(path: str, include_unreferenced: bool = True) -> BundleGraph:
    """Resolve a directory or root ``SKILL.md`` into a closed local graph.

    All ordinary files are retained by default because dynamically constructed
    paths cannot be proven dead without a runtime trace.  ``reachable`` records
    the statically reachable subset, enabling reporting without unsafe pruning.
    """
    supplied = Path(path).expanduser().resolve()
    entry_abs = supplied / "SKILL.md" if supplied.is_dir() else supplied
    if not entry_abs.is_file():
        raise FileNotFoundError(f"bundle entry SKILL.md not found: {entry_abs}")
    root = entry_abs.parent.resolve()
    entry_rel = _posix(entry_abs.relative_to(root))
    graph = BundleGraph(root_dir=str(root), entry=entry_rel)

    def add_node(abs_path: Path, reachable: bool) -> BundleNode:
        rel = _posix(abs_path.relative_to(root))
        node = graph.nodes.get(rel)
        if node is None:
            node = BundleNode(
                path=rel,
                kind=classify_resource(abs_path, entry_abs),
                size_bytes=abs_path.stat().st_size,
                text=_read_text(abs_path),
                reachable=reachable,
                output_path=rel,
            )
            graph.nodes[rel] = node
        elif reachable:
            node.reachable = True
        return node

    root_node = add_node(entry_abs, True)
    queue = [root_node.path]
    scanned = set()
    while queue:
        source_rel = queue.pop(0)
        if source_rel in scanned:
            continue
        scanned.add(source_rel)
        source_node = graph.nodes[source_rel]
        if not (source_node.is_markdown and source_node.text is not None):
            continue
        source_abs = root / source_rel
        for ref in reference_occurrences(source_node.text):
            if ref.external or not ref.target:
                graph.edges.append(ReferenceEdge(
                    source=source_rel, raw_target=ref.raw,
                    fragment=ref.fragment, condition=ref.condition,
                    line=ref.line, syntax=ref.syntax, status="external",
                ))
                continue
            status, target_abs = _resolve_target(source_abs, ref.target, root)
            target_rel = ""
            if status == "local":
                target_node = add_node(target_abs, True)
                target_rel = target_node.path
                if target_rel not in scanned:
                    queue.append(target_rel)
            graph.edges.append(ReferenceEdge(
                source=source_rel, raw_target=ref.raw, target=target_rel,
                fragment=ref.fragment, condition=ref.condition, line=ref.line,
                syntax=ref.syntax, status=status,
            ))
            if status in ("missing", "path_escape", "directory"):
                msg = f"{status}: {source_rel}:{ref.line} -> {ref.target}"
                (graph.errors if status == "path_escape" else graph.warnings).append(msg)

    if include_unreferenced:
        for abs_path in _iter_bundle_files(root):
            add_node(abs_path.resolve(), False)

    graph.cycles = _find_cycles(graph)
    if graph.cycles:
        graph.warnings.append(
            f"reference cycles detected: {len(graph.cycles)}; preserved and bounded in cost analysis")
    return graph


def split_frontmatter(text: str) -> Tuple[str, str]:
    lines = (text or "").splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return "", text or ""
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "".join(lines[:i + 1]), "".join(lines[i + 1:]).lstrip("\n")
    return "", text or ""


def frontmatter_fields(text: str) -> Dict[str, str]:
    """Minimal top-level YAML reader for catalog-cost fields.

    It intentionally supports only scalar ``name`` and ``description`` values;
    the frontmatter bytes themselves are always preserved verbatim.
    """
    fm, _ = split_frontmatter(text)
    fields: Dict[str, str] = {}
    for line in fm.splitlines()[1:-1]:
        if ":" not in line or line[:1].isspace():
            continue
        key, value = line.split(":", 1)
        if key.strip() in ("name", "description"):
            fields[key.strip()] = value.strip().strip("\"'")
    return fields


def normalized_nested_output(path: str) -> str:
    """Keep one canonical ``SKILL.md`` while retaining nested subskills as refs."""
    p = PurePosixPath(path)
    if p.name.lower() != "skill.md" or len(p.parts) == 1:
        return path
    return str(p.with_name("SUBSKILL.md"))


def output_path_map(graph: BundleGraph, normalize_nested: bool = True) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for path, node in graph.nodes.items():
        out = normalized_nested_output(path) if normalize_nested else path
        node.output_path = out
        mapping[path] = out
    return mapping


def source_path(graph: BundleGraph, rel: str) -> Path:
    return Path(graph.root_dir) / Path(rel)
