"""Activation-aware Markdown capsule planning for Phase A.

Capsules use only ordinary Markdown links and imperative loading instructions;
they require no resolver service or agent-harness hook.  The transformation is
therefore intentionally conservative: only explicit conditional H2 sections
are split and the unconditional material remains in the original dispatcher.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Dict, List, Optional, Tuple

from .skill import approx_tokens


_H2 = re.compile(r"^##\s+(.+?)\s*#*\s*$")
_CONDITION = re.compile(
    r"^(?:when|if|for|on|whenever|in case of|unless)\s+(.+)$", re.I)


def _slug(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return value[:48] or "case"


def _split_h2(text: str) -> Tuple[str, List[Tuple[str, str]]]:
    lines = (text or "").splitlines()
    preamble: List[str] = []
    sections: List[Tuple[str, str]] = []
    title = ""
    body: List[str] = []
    for line in lines:
        match = _H2.match(line)
        if match:
            if title:
                sections.append((title, "\n".join(body).strip()))
            elif body:
                preamble.extend(body)
            title = match.group(1).strip()
            body = []
        else:
            body.append(line)
    if title:
        sections.append((title, "\n".join(body).strip()))
    elif body:
        preamble.extend(body)
    return "\n".join(preamble).strip(), sections


def _condition(title: str, body: str) -> str:
    match = _CONDITION.match(title.strip())
    if match:
        return match.group(1).strip()
    first = next((line.strip().lstrip("-*+ ") for line in body.splitlines()
                  if line.strip()), "")
    match = _CONDITION.match(first)
    return match.group(1).strip(" :,.\t") if match else ""


@dataclass
class CapsulePlan:
    resource: str
    dispatcher: str
    capsules: Dict[str, str] = field(default_factory=dict)
    conditions: Dict[str, str] = field(default_factory=dict)
    original_tokens: int = 0

    def to_json(self) -> dict:
        return {
            "resource": self.resource,
            "capsules": [
                {"path": path, "condition": self.conditions[path],
                 "tokens": approx_tokens(text)}
                for path, text in sorted(self.capsules.items())
            ],
            "original_tokens": self.original_tokens,
            "dispatcher_tokens": approx_tokens(self.dispatcher),
        }


def plan_capsules(
    resource: str,
    text: str,
    min_tokens: int = 220,
    min_conditional_sections: int = 2,
) -> Optional[CapsulePlan]:
    """Split explicit conditional H2 sections into on-demand Markdown files."""
    if approx_tokens(text) < min_tokens:
        return None
    preamble, sections = _split_h2(text)
    conditional = [(title, body, _condition(title, body))
                   for title, body in sections]
    conditional = [item for item in conditional if item[2]]
    if len(conditional) < min_conditional_sections:
        return None

    p = PurePosixPath(resource)
    capsule_dir = p.parent / ".skillzip_capsules" / _slug(p.stem)
    capsules: Dict[str, str] = {}
    conditions: Dict[str, str] = {}
    moved = {(title, body) for title, body, _ in conditional}
    common_parts = [preamble] if preamble else []
    for title, body in sections:
        if (title, body) not in moved:
            common_parts.append(f"## {title}\n\n{body}".strip())

    index: List[str] = []
    for i, (title, body, condition) in enumerate(conditional, 1):
        cap_path = str(capsule_dir / f"{i:02d}-{_slug(title)}.md")
        capsule_text = f"## {title}\n\n{body}".strip() + "\n"
        capsules[cap_path] = capsule_text
        conditions[cap_path] = condition
        rel = PurePosixPath(cap_path).relative_to(p.parent)
        index.append(f"- When {condition}, read [{title}]({rel.as_posix()}).")

    dispatcher_parts = [part for part in common_parts if part.strip()]
    dispatcher_parts.extend([
        "## On-demand modules",
        "Load only the module whose condition matches the current task; do not load unrelated modules.",
        "\n".join(index),
    ])
    dispatcher = "\n\n".join(dispatcher_parts).strip() + "\n"
    # The split should reduce a representative one-branch load even before the
    # ordinary SkillZip optimizer runs.  Otherwise retain the original file.
    largest_branch = max(approx_tokens(value) for value in capsules.values())
    if approx_tokens(dispatcher) + largest_branch >= approx_tokens(text):
        return None
    return CapsulePlan(
        resource=resource,
        dispatcher=dispatcher,
        capsules=capsules,
        conditions=conditions,
        original_tokens=approx_tokens(text),
    )
