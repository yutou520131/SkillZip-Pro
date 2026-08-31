"""Typed skill contract data model for SkillZip (paper Eq. 1-3).

A skill is represented as a typed contract C(S) = <I, G, T, C, O, E> whose
elements are *units* a = (tau, sigma, g, m, p, P) (Eq. 2):

    tau  unit type          {interface, workflow, tool, rule, output, evidence}
    sigma scope path         e.g. ("root", "workflow", "if-validation-fails")
    g    optional guard      free-text condition under which the unit applies
    m    modality            {must, must_not, should, info}  (rules; else info)
    p    normalized content  canonical text of the requirement
    P    provenance          source block ids that support the unit

Workflow units additionally record incoming/outgoing edges; tool units record
an argument signature; output units record field/validation info; interface
units record a role (name|purpose|trigger|exclusion).

The compressed skill is a Library K of reusable contract elements + a Residual
R of unique / exceptional / uncertain content (paper Sec. 4.1). ZipState is the
persistent `skillzip.json` sidecar used by Zip-on-Write (paper Sec. 5.2).

This module is pure data + (de)serialization; no optimization logic lives here.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

# The six contract element types of Eq. (1). `evidence` (examples/templates/
# rationale) is the only removable-when-covered type; the rest are required.
UNIT_TYPES = ("interface", "workflow", "tool", "rule", "output", "evidence")
MODALITIES = ("must", "must_not", "should", "info")
# Required-unit types that A_req(S) must keep covered (Eq. 3). Evidence is
# excluded: it is removable exactly when the requirements it uniquely expresses
# are represented elsewhere.
REQUIRED_TYPES = ("interface", "workflow", "tool", "rule", "output")

Scope = Tuple[str, ...]
ROOT: Scope = ("root",)


def _norm(text: str) -> str:
    """Case/)whitespace/punctuation-insensitive normalization for hashing and
    exact-duplicate detection. Keeps word content, drops surface noise."""
    t = (text or "").lower().strip()
    t = re.sub(r"[`*_#>]+", " ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def content_hash(unit_type: str, modality: str, guard: str, content: str) -> str:
    key = json.dumps([unit_type, modality or "", _norm(guard or ""), _norm(content)],
                     sort_keys=True)
    return hashlib.sha1(key.encode()).hexdigest()[:16]


@dataclass
class Block:
    """A deterministic scan block with stable provenance (paper B.2)."""
    id: str
    kind: str                      # heading|paragraph|list_item|code|table|frontmatter
    heading_path: List[str]
    text: str
    line_range: Tuple[int, int]
    depth: int = 0                 # markdown nesting depth (list/heading level)

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class Unit:
    """A typed contract unit a = (tau, sigma, g, m, p, P) (Eq. 2)."""
    id: str
    type: str
    scope: Scope
    content: str
    modality: str = "info"
    guard: str = ""
    provenance: List[str] = field(default_factory=list)
    role: str = ""                                 # interface: name|purpose|trigger|exclusion
    edges_in: List[str] = field(default_factory=list)   # workflow: predecessor unit ids
    edges_out: List[str] = field(default_factory=list)  # workflow: successor unit ids
    tool: str = ""                                 # tool: tool name
    args: List[str] = field(default_factory=list)  # tool: required argument signature
    fields: List[str] = field(default_factory=list)  # output: required field names
    validation: str = ""                           # output: validation/completion condition
    order: int = 0                                 # workflow: position for numbered rendering
    anchor: str = ""                               # source heading defining an
                                                   # anchor this unit lives under
                                                   # (e.g. "步骤3：..."); kept so
                                                   # cross-references stay resolvable
    locked: bool = False                           # locked residual: verbatim, undeletable
    exceptions: List["Exception_"] = field(default_factory=list)  # attached guarded deltas

    @property
    def norm(self) -> str:
        return _norm(self.content)

    @property
    def hash(self) -> str:
        return content_hash(self.type, self.modality, self.guard, self.content)

    def blocking_key(self) -> tuple:
        """Hard compatibility key applied before similarity (paper Eq. 12):
        (type, modality, tool/output namespace, scope family)."""
        namespace = self.tool or (self.role if self.type == "interface" else "")
        if self.type == "output":
            namespace = "output"
        scope_family = self.scope[:2]
        return (self.type, self.modality, namespace, scope_family)

    def is_required(self) -> bool:
        return self.type in REQUIRED_TYPES

    def to_json(self) -> dict:
        d = asdict(self)
        d["scope"] = list(self.scope)
        d["exceptions"] = [e.to_json() for e in self.exceptions]
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Unit":
        d = dict(d)
        d["scope"] = tuple(d.get("scope") or ["root"])
        d["exceptions"] = [Exception_.from_json(e) for e in d.get("exceptions", [])]
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Exception_:
    """A guarded delta delta_i attached to a common rule (Eq. 11)."""
    guard: str
    content: str

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "Exception_":
        return cls(guard=d.get("guard", ""), content=d.get("content", ""))


@dataclass
class Procedure:
    """A named shared workflow abstraction def(q) (Eq. 10)."""
    name: str
    steps: List[str]                    # ordered normalized action contents
    unit_ids: List[str] = field(default_factory=list)  # workflow units it expands to

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "Procedure":
        return cls(name=d["name"], steps=list(d.get("steps", [])),
                   unit_ids=list(d.get("unit_ids", [])))


@dataclass
class Contract:
    """The extracted typed contract C(S) = <I,G,T,C,O,E> (Eq. 1), stored as a
    flat, id-indexed unit collection grouped on demand by type."""
    units: List[Unit] = field(default_factory=list)
    residual: List[Unit] = field(default_factory=list)     # locked verbatim spans
    blocks: List[Block] = field(default_factory=list)

    def by_type(self, t: str) -> List[Unit]:
        return [u for u in self.units if u.type == t]

    def required(self) -> List[Unit]:
        """A_req(S): interface conditions, workflow nodes/edges, tool
        requirements, scoped rules, and output requirements (Eq. 3)."""
        return [u for u in self.units if u.is_required()]

    def get(self, uid: str) -> Optional[Unit]:
        for u in self.units:
            if u.id == uid:
                return u
        for u in self.residual:
            if u.id == uid:
                return u
        return None

    def all_units(self) -> List[Unit]:
        return list(self.units) + list(self.residual)

    def to_json(self) -> dict:
        return {
            "units": [u.to_json() for u in self.units],
            "residual": [u.to_json() for u in self.residual],
            "blocks": [b.to_json() for b in self.blocks],
        }

    @classmethod
    def from_json(cls, d: dict) -> "Contract":
        c = cls()
        c.units = [Unit.from_json(u) for u in d.get("units", [])]
        c.residual = [Unit.from_json(u) for u in d.get("residual", [])]
        c.blocks = [Block(**b) if not isinstance(b, Block) else b
                    for b in d.get("blocks", [])]
        return c


@dataclass
class Library:
    """The compressed representation (K, R): reusable contract elements plus a
    residual of unique/exceptional/uncertain content (paper Sec. 4.1)."""
    units: List[Unit] = field(default_factory=list)       # selected/lifted units (K)
    procedures: List[Procedure] = field(default_factory=list)
    residual: List[Unit] = field(default_factory=list)    # R (locked verbatim spans)

    def all_units(self) -> List[Unit]:
        return list(self.units) + list(self.residual)

    def to_json(self) -> dict:
        return {
            "units": [u.to_json() for u in self.units],
            "procedures": [p.to_json() for p in self.procedures],
            "residual": [u.to_json() for u in self.residual],
        }

    @classmethod
    def from_json(cls, d: dict) -> "Library":
        lib = cls()
        lib.units = [Unit.from_json(u) for u in d.get("units", [])]
        lib.procedures = [Procedure.from_json(p) for p in d.get("procedures", [])]
        lib.residual = [Unit.from_json(u) for u in d.get("residual", [])]
        return lib


@dataclass
class ZipState:
    """The persistent sidecar `skillzip.json` for Zip-on-Write (paper Sec. 5.2,
    B.1): current library, scope tree, reuse statistics, and repack bookkeeping."""
    name: str
    library: Library = field(default_factory=Library)
    frontmatter: str = ""        # verbatim YAML retrieval contract (never compressed)
    rule_family_counts: Dict[str, int] = field(default_factory=dict)
    ngram_counts: Dict[str, int] = field(default_factory=dict)
    patches_since_repack: int = 0
    units_at_last_repack: int = 0
    log: List[dict] = field(default_factory=list)          # write-ahead transaction log

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "library": self.library.to_json(),
            "frontmatter": self.frontmatter,
            "rule_family_counts": self.rule_family_counts,
            "ngram_counts": self.ngram_counts,
            "patches_since_repack": self.patches_since_repack,
            "units_at_last_repack": self.units_at_last_repack,
            "log": self.log,
        }

    @classmethod
    def from_json(cls, d: dict) -> "ZipState":
        return cls(
            name=d.get("name", "skill"),
            library=Library.from_json(d.get("library", {})),
            frontmatter=d.get("frontmatter", ""),
            rule_family_counts=d.get("rule_family_counts", {}),
            ngram_counts=d.get("ngram_counts", {}),
            patches_since_repack=d.get("patches_since_repack", 0),
            units_at_last_repack=d.get("units_at_last_repack", 0),
            log=d.get("log", []),
        )

    def save(self, path: str) -> None:
        import os
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_json(), f, indent=2)

    @classmethod
    def load(cls, path: str) -> "ZipState":
        with open(path, encoding="utf-8") as f:
            return cls.from_json(json.load(f))
