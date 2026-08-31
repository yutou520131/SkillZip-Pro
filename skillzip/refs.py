"""Intra-document reference integrity (anchors <-> references).

A skill document often carries *internal cross-references*: "jump to Step 8",
"see §9.1", "continue with 步骤2". Such a reference is part of the behavioral
contract -- it encodes a control transfer -- yet its target is a *heading*, i.e.
exactly the kind of organizational scaffolding that type-based re-rendering is
free to drop. Dropping the anchor while keeping the reference leaves a dangling
pointer: the text still says "jump to Step 8" but no Step 8 exists any more.

This module makes anchors first-class:
  * `anchor_label`      recognizes an anchor label in a heading title
  * `attach_anchors`    records, for every unit, the anchored source heading it
                        came from (via provenance -> heading path)
  * `referenced_labels` finds anchor labels referenced inside a text
  * `defined_labels`    finds anchor labels defined by headings of a text
  * `dangling_labels`   referenced - defined  (must be empty after rendering)

Everything here is deterministic: no model calls, no task execution.
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Set

from . import scanner

# Anchor label as it appears in a *heading*, e.g.
#   "### 步骤3：A1 事件直接描述速判"      -> 步骤3
#   "### Step 0: pre-scan"               -> Step 0
#   "## 8. 证据固定规则"                  -> §8
#   "### 9.1 联系方式类豁免"              -> §9.1
#   "## 二、输出格式"                     -> §二
_HEAD_PATTERNS = [
    (re.compile(r"^\s*(步骤\s*([0-9０-９]+))"), lambda m: "步骤" + _ascii_digits(m.group(2))),
    (re.compile(r"^\s*(Step\s*([0-9]+))", re.I), lambda m: "Step " + m.group(2)),
    (re.compile(r"^\s*§\s*([0-9]+(?:\.[0-9]+)*)"), lambda m: "§" + m.group(1)),
    (re.compile(r"^\s*([0-9]+(?:\.[0-9]+)*)[.、)\s]"), lambda m: "§" + m.group(1)),
    (re.compile(r"^\s*([一二三四五六七八九十]+)[、.]"), lambda m: "§" + m.group(1)),
]

# The same labels as they appear *inside body text* (a reference, not a heading).
_REF_PATTERNS = [
    (re.compile(r"步骤\s*([0-9０-９]+)"), lambda m: "步骤" + _ascii_digits(m.group(1))),
    (re.compile(r"\bStep\s*([0-9]+)", re.I), lambda m: "Step " + m.group(1)),
    (re.compile(r"§\s*([0-9]+(?:\.[0-9]+)*)"), lambda m: "§" + m.group(1)),
    (re.compile(r"§\s*([一二三四五六七八九十]+)"), lambda m: "§" + m.group(1)),
]


# A label may also be *defined inline* rather than by a heading, e.g. a decision
# tree listing "Step 1. parse the content form -> ...; Step 2. ...". A label
# immediately followed by a definition marker and descriptive text declares the
# step; a bare mention ("jump to 步骤8", "see §9.0.1 table") only references it.
_INLINE_DEF_PATTERNS = [
    (re.compile(r"步骤\s*([0-9０-９]+)\s*[.:：、]\s*\S"),
     lambda m: "步骤" + _ascii_digits(m.group(1))),
    (re.compile(r"\bStep\s*([0-9]+)\s*[.:：、]\s*\S", re.I),
     lambda m: "Step " + m.group(1)),
    (re.compile(r"§\s*([0-9]+(?:\.[0-9]+)*)\s*[.:：、]\s*\S"),
     lambda m: "§" + m.group(1)),
]


def _ascii_digits(s: str) -> str:
    return s.translate(str.maketrans("０１２３４５６７８９", "0123456789"))


def anchor_label(heading_title: str) -> str:
    """Anchor label defined by a heading title, or "" when it defines none."""
    for rx, fmt in _HEAD_PATTERNS:
        m = rx.match(heading_title or "")
        if m:
            return fmt(m)
    return ""


def _labels(text: str, patterns) -> Set[str]:
    out: Set[str] = set()
    for rx, fmt in patterns:
        for m in rx.finditer(text or ""):
            out.add(fmt(m))
    return out


def referenced_labels(text: str) -> Set[str]:
    """Anchor labels referenced by non-heading lines of `text`."""
    body = "\n".join(ln for ln in (text or "").splitlines()
                     if not ln.lstrip().startswith("#"))
    return _labels(body, _REF_PATTERNS)


def defined_labels(text: str) -> Set[str]:
    """Anchor labels defined by `text`: by a heading, by an inline heading alias
    such as '## Output (§二)', or by an inline definition such as
    'Step 1. parse the content form'."""
    out: Set[str] = set()
    for ln in (text or "").splitlines():
        s = ln.lstrip()
        if s.startswith("#"):
            title = s.lstrip("#").strip()
            lab = anchor_label(title)
            if lab:
                out.add(lab)
            out |= _labels(title, _REF_PATTERNS)   # inline "(§二)" alias
        else:
            out |= _labels(s, _INLINE_DEF_PATTERNS)
    return out


def dangling_labels(text: str) -> List[str]:
    """Referenced-but-undefined anchor labels: every one is a broken control
    transfer in the compressed skill."""
    return sorted(referenced_labels(text) - defined_labels(text))


def source_section(source_text: str, label: str) -> str:
    """Verbatim source text of the section anchored by `label` (heading line plus
    its blocks). Used by the audit as the shortest covering span to restore when
    a reference target was dropped entirely."""
    keep: List[str] = []
    for b in scanner.scan(source_text):
        path = list(b.heading_path)
        if b.kind == "heading":
            path = path + [b.text] if b.text not in path else path
        if any(anchor_label(h) == label for h in path):
            keep.append(b.text.strip() if b.kind != "heading"
                        else "### " + b.text.strip())
    return "\n".join(keep).strip()


def attach_anchors(units: Iterable, source_text: str) -> None:
    """Record on each unit the anchored source heading it originated from, so
    the renderer can keep that anchor resolvable. Deepest anchored heading of
    the unit's provenance block wins; units without provenance keep anchor=""."""
    heads: Dict[str, List[str]] = {}
    for b in scanner.scan(source_text):
        heads[b.id] = list(b.heading_path)
    for u in units:
        if getattr(u, "anchor", ""):
            continue
        best = ""
        for bid in getattr(u, "provenance", []) or []:
            for title in reversed(heads.get(bid, [])):
                if anchor_label(title):
                    best = title
                    break
            if best:
                break
        u.anchor = best
