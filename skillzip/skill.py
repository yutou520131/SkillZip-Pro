"""The ``Skill`` artifact: a markdown document that acts as the trainable state
of a frozen agent.

A skill is deliberately an *ordinary text file* -- portable, diffable, and
human-auditable. SkillZip consumes and produces exactly this type, so a
compressed skill remains a drop-in replacement for the original.

``approx_tokens`` is the lightweight length proxy used throughout SkillZip's cost
model; it avoids a tokenizer dependency while staying conservative (it never
under-counts badly), which keeps compression decisions reproducible.
"""
from __future__ import annotations
import os
import re
from dataclasses import dataclass


def approx_tokens(text: str) -> int:
    words = len(re.findall(r"\S+", text or ""))
    return max(len(text) // 4, words)  # ~conservative


@dataclass
class Skill:
    name: str
    body: str = ""

    @property
    def tokens(self) -> int:
        return approx_tokens(self.body)

    def to_markdown(self) -> str:
        if self.body.lstrip().startswith("---"):
            # YAML front-matter (name/description) is the skill-retrieval
            # contract and must stay the first bytes of the artifact -- never
            # shadow it with a synthetic '# SKILL:' heading.
            return self.body.strip() + "\n"
        return f"# SKILL: {self.name}\n\n{self.body.strip()}\n"

    def clone(self, body: str) -> "Skill":
        return Skill(name=self.name, body=body)

    def apply_edit(self, edit: dict, budget_tokens: int) -> "Skill":
        """Return a new Skill with the edit applied, or self (unchanged) when the
        edit would add more than `budget_tokens` tokens or is malformed."""
        op = (edit or {}).get("op")
        text = (edit or {}).get("text", "")
        find = (edit or {}).get("find", "")
        if op == "add":
            if approx_tokens(text) > budget_tokens:
                return self
            new = (self.body.rstrip() + "\n" + text.strip()).strip()
        elif op == "replace" and find:
            if approx_tokens(text) > budget_tokens:
                return self
            if find not in self.body:
                return self
            new = self.body.replace(find, text)
        elif op == "delete" and find:
            if find not in self.body:
                return self
            new = self.body.replace(find, "").strip()
        else:
            return self
        return self.clone(new)

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.to_markdown())

    @classmethod
    def load(cls, path: str, name: str) -> "Skill":
        with open(path, encoding="utf-8") as f:
            text = f.read()
        body = re.sub(r"^#\s*SKILL:.*\n+", "", text, count=1)
        return cls(name=name, body=body.strip())
