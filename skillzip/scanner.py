"""Deterministic markdown scanner (paper Alg. 1 line 1, Appendix B.2).

Parses front matter, headings, nested lists, code blocks, tables, and file
references into blocks with stable provenance IDs *before* any model is used.
Code fences and tables stay atomic (splitting them can destroy a template or
schema). Markdown nesting supplies an initial scope tree; numbered lists and
temporal markers are recorded as high-confidence workflow hints.

No model, no task access.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import List, Tuple

from .contract import Block

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)(?:[-*+]|(\d+)[.)])\s+(.*)$")
_FENCE = re.compile(r"^\s*(```+|~~~+)")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_ORDINAL = re.compile(r"^\s*(\d+)[.)]\s")
_TEMPORAL = re.compile(r"\b(first|then|next|after|before|finally|step\s*\d+|"
                       r"once|when done)\b", re.I)
_MD_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
_PLAIN_FILE = re.compile(
    r"(?<![\w:/])([\w.@+-]+(?:/[\w.@+-]+)*\."
    r"(?:md|markdown|txt|json|ya?ml|toml|csv|tsv|py|sh|bash|js|ts|"
    r"sql|xml|html|css|svg|png|jpe?g|gif|webp|pdf))(?:#[\w.-]+)?",
    re.I,
)
_EXTERNAL = re.compile(r"^(?:https?|data|mailto):", re.I)
_GUARD_LINE = re.compile(
    r"^\s*(?:[-*+]\s*)?(?:if|when|whenever|for|on|in case|unless)\b"
    r"\s+(.+?)(?:[:,.;]|$)", re.I,
)


@dataclass(frozen=True)
class FileReference:
    """A file/resource reference with enough context for progressive loading.

    ``raw`` is the exact target token found in the source. ``target`` excludes
    an optional fragment and Markdown title.  The resolver, rather than this
    lexical scanner, decides whether the path is allowed and whether it exists.
    """

    raw: str
    target: str
    fragment: str = ""
    line: int = 0
    condition: str = "on_demand_unspecified"
    syntax: str = "plain"
    external: bool = False


def _bid(norm_text: str, heading_path: List[str]) -> str:
    """Stable id = hash(normalized text + ancestor headings) (B.2). Line
    numbers are stored separately so unrelated insertions do not invalidate
    provenance."""
    key = "\u0001".join(heading_path) + "\u0002" + re.sub(r"\s+", " ", norm_text.lower()).strip()
    return "b" + hashlib.sha1(key.encode()).hexdigest()[:10]


def scan(text: str) -> List[Block]:
    lines = (text or "").splitlines()
    blocks: List[Block] = []
    heading_path: List[str] = []
    i = 0
    n = len(lines)

    # YAML front matter
    if i < n and lines[i].strip() == "---":
        j = i + 1
        while j < n and lines[j].strip() != "---":
            j += 1
        fm = "\n".join(lines[i:min(j + 1, n)])
        blocks.append(Block(_bid(fm, ["frontmatter"]), "frontmatter",
                            ["frontmatter"], fm, (i + 1, j + 1), 0))
        i = j + 1

    para: List[str] = []
    para_start = 0

    def flush_para():
        nonlocal para, para_start
        if not para:
            return
        chunk = "\n".join(para).strip()
        if chunk:
            blocks.append(Block(_bid(chunk, heading_path), "paragraph",
                                list(heading_path), chunk,
                                (para_start + 1, para_start + len(para)),
                                len(heading_path)))
        para = []

    while i < n:
        line = lines[i]

        m = _HEADING.match(line)
        if m:
            flush_para()
            level = len(m.group(1))
            title = m.group(2).strip()
            heading_path = heading_path[:level - 1] + [title]
            blocks.append(Block(_bid(title, heading_path), "heading",
                                list(heading_path), title, (i + 1, i + 1), level))
            i += 1
            continue

        f = _FENCE.match(line)
        if f:
            flush_para()
            fence = f.group(1)[0] * 3
            start = i
            body = [line]
            i += 1
            while i < n and not lines[i].strip().startswith(fence):
                body.append(lines[i])
                i += 1
            if i < n:
                body.append(lines[i])
                i += 1
            chunk = "\n".join(body)
            blocks.append(Block(_bid(chunk, heading_path), "code",
                                list(heading_path), chunk,
                                (start + 1, i), len(heading_path)))
            continue

        if _TABLE_ROW.match(line):
            flush_para()
            start = i
            body = []
            while i < n and _TABLE_ROW.match(lines[i]):
                body.append(lines[i])
                i += 1
            chunk = "\n".join(body)
            blocks.append(Block(_bid(chunk, heading_path), "table",
                                list(heading_path), chunk,
                                (start + 1, i), len(heading_path)))
            continue

        b = _BULLET.match(line)
        if b:
            flush_para()
            indent = len(b.group(1))
            ordinal = b.group(2)
            content = b.group(3).strip()
            # gather continuation lines (deeper indented, non-bullet)
            start = i
            i += 1
            cont: List[str] = []
            while i < n:
                nxt = lines[i]
                if not nxt.strip():
                    break
                if _BULLET.match(nxt) or _HEADING.match(nxt) or _FENCE.match(nxt):
                    break
                if len(nxt) - len(nxt.lstrip()) > indent:
                    cont.append(nxt.strip())
                    i += 1
                else:
                    break
            full = content + ((" " + " ".join(cont)) if cont else "")
            depth = len(heading_path) + (indent // 2) + 1
            blk = Block(_bid(full, heading_path + [full[:40]]), "list_item",
                        list(heading_path), full, (start + 1, i), depth)
            blocks.append(blk)
            continue

        if line.strip():
            if not para:
                para_start = i
            para.append(line)
            i += 1
        else:
            flush_para()
            i += 1

    flush_para()
    return blocks


def workflow_hint(block: Block) -> bool:
    """High-confidence workflow signal: ordered list item or temporal marker."""
    return bool(_ORDINAL.match(block.text) or _TEMPORAL.search(block.text))


def _target_parts(raw: str) -> Tuple[str, str]:
    value = (raw or "").strip()
    if value.startswith("<") and ">" in value:
        value = value[1:value.index(">")]
    else:
        # Markdown permits an optional quoted title after the target.  Paths
        # containing spaces should use angle brackets; keep the conservative
        # first-token interpretation for unbracketed links.
        value = re.split(r"\s+[\"']", value, maxsplit=1)[0].strip()
    target, sep, fragment = value.partition("#")
    return target, fragment if sep else ""


def _line_condition(line: str) -> str:
    m = _GUARD_LINE.match(line or "")
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    low = (line or "").lower()
    if any(word in low for word in ("only when", "as needed", "if needed",
                                     "when needed", "on demand")):
        return "on_demand"
    return "on_demand_unspecified"


def reference_occurrences(text: str) -> List[FileReference]:
    """Return structured local/external reference occurrences.

    Results are stable and de-duplicated by ``(target, fragment, line)``.  Code
    fences are intentionally ignored: examples must not become deployed bundle
    dependencies.  Markdown links/images and conservative plain file mentions
    are supported without adding a parser dependency.
    """
    out: List[FileReference] = []
    seen = set()
    in_fence = False
    for lineno, line in enumerate((text or "").splitlines(), 1):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        occupied: List[Tuple[int, int]] = []
        for match in _MD_LINK.finditer(line):
            raw = match.group(1).strip()
            target, fragment = _target_parts(raw)
            if not target:
                continue
            key = (target, fragment, lineno)
            if key not in seen:
                seen.add(key)
                out.append(FileReference(
                    raw=raw, target=target, fragment=fragment, line=lineno,
                    condition=_line_condition(line), syntax="markdown",
                    external=bool(_EXTERNAL.match(target)),
                ))
            occupied.append(match.span())
        for match in _PLAIN_FILE.finditer(line):
            if any(a <= match.start() < b for a, b in occupied):
                continue
            raw = match.group(1).strip()
            target, fragment = _target_parts(raw)
            key = (target, fragment, lineno)
            if key in seen:
                continue
            seen.add(key)
            out.append(FileReference(
                raw=raw, target=target, fragment=fragment, line=lineno,
                condition=_line_condition(line), syntax="plain",
                external=bool(_EXTERNAL.match(target)),
            ))
    return out


def file_references(text: str) -> List[str]:
    """Backward-compatible list API used by the original implementation."""
    seen, out = set(), []
    for ref in reference_occurrences(text):
        value = ref.target + (("#" + ref.fragment) if ref.fragment else "")
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out
