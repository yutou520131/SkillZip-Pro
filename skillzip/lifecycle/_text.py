"""Deterministic text primitives shared by the lifecycle layer.

These helpers answer one question in three variants: *is this behavioural
statement still visible to the agent?*  They are intentionally simple and
model-free so that every lifecycle number is reproducible offline.

Nothing here is specific to a benchmark; the same routines are used to score an
entry's independence, to decide what a standalone-preserving publish must
restore, and to charge tokens for a view.
"""
from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import List, Optional

_WORD = re.compile(r"[a-z0-9]+")

# Markdown link with a *local* target (an http(s) URL is not a bundle edge).
LINK_RE = re.compile(r"\[([^\]]*)\]\(\s*(?!https?:)([^)]+)\)")

# Bare paths some skills write without link syntax, e.g. "see references/csv.md".
PLAIN_PATH_RE = re.compile(r"(?:references|sub|subskills|scripts|assets)/[\w./-]+\.\w+")

# YAML front matter at the very top of a markdown document.
FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)

# Words that make a reference mandatory rather than conditional.  A line saying
# "always read X before starting" is charged to every run; a line saying "if the
# task is about Y, read Z" is not.
MANDATORY_RE = re.compile(r"\b(before|first|always|must)\b", re.I)

MD_SUFFIXES = (".md", ".markdown")


def norm(text: str) -> str:
    """Lowercase alphanumeric word stream, punctuation and spacing removed.

    Comparing normalized forms keeps the checks robust to reformatting (a
    compressor is allowed to change bullets, casing, and whitespace) while still
    being sensitive to deleted words.
    """
    return " ".join(_WORD.findall((text or "").lower()))


def tokens(text: str) -> int:
    """Whitespace-and-punctuation token count.

    A deterministic static proxy, not a tokenizer for any specific model.  The
    same estimator is used for bundles and for views so the two are comparable;
    production users can swap in a deployment tokenizer without touching the
    lifecycle logic.
    """
    return len(re.findall(r"\w+|[^\w\s]", text))


def is_present(unit_norm: str, haystack_norm: str) -> bool:
    """Is this statement still present, verbatim or lightly reworded?

    Verbatim containment is tried first.  Otherwise we slide a window the size of
    the statement over the haystack and accept either a high local edit
    similarity or a high local coverage of the statement's content words.  The
    second test matters because a faithful compressor may legitimately move a
    qualifier ("If X, do Y" rendered as "do Y (if X)"): every word is still
    there and still local, so the obligation survives even though the order
    changed.

    Coverage is deliberately measured *inside one window*, so a statement whose
    words merely happen to be scattered across unrelated files still counts as
    lost.
    """
    if not unit_norm:
        return True
    if unit_norm in haystack_norm:
        return True
    words = unit_norm.split()
    hay = haystack_norm.split()
    if not hay:
        return False
    content_words = [w for w in words if len(w) > 3]
    window = len(words)
    step = max(1, window // 3)
    for start in range(0, max(1, len(hay) - window + 1), step):
        seg_words = hay[start:start + window + 6]
        seg = " ".join(seg_words)
        if difflib.SequenceMatcher(None, unit_norm, seg).ratio() >= 0.82:
            return True
        if content_words:
            seg_set = set(seg_words)
            hits = sum(1 for w in content_words if w in seg_set)
            if hits / len(content_words) >= 0.9:
                return True
    return False


def entailed_units(env_contract: Optional[str]) -> List[str]:
    """Normalized statements an environment contract already guarantees.

    A `conditional` entry is allowed to lean on its declared host context, so a
    statement the contract promises is not counted as lost when it is removed.
    Without a contract the list is empty and no such removal is excused.
    """
    if not env_contract or not Path(env_contract).is_file():
        return []
    try:
        data = json.loads(Path(env_contract).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return [norm(g.get("content", "")) for g in data.get("guarantees", [])
            if g.get("content")]


def units_of_file(path: Path) -> List[str]:
    """Behaviour-bearing lines a single document owns, in file order.

    Headings, fenced code, front matter, and very short fragments are skipped:
    they carry structure rather than an obligation, and counting them would make
    the independence score sensitive to formatting.
    """
    if not path.is_file():
        return []
    text = re.sub(r"^---.*?---", "", path.read_text(encoding="utf-8"), flags=re.S)
    out: List[str] = []
    in_fence = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or stripped.startswith("#"):
            continue
        body = re.sub(r"^\d+[.)]\s*", "", stripped.lstrip("-*+ ").strip())
        if len(body) < 25:
            continue
        out.append(body)
    return out


def read_markdown_text(root: Path, rels) -> str:
    """Normalized concatenation of the markdown files in ``rels`` under ``root``."""
    parts: List[str] = []
    for rel in sorted(rels):
        path = root / rel
        if path.is_file() and path.suffix.lower() in MD_SUFFIXES:
            parts.append(path.read_text(encoding="utf-8"))
    return norm(" ".join(parts))
