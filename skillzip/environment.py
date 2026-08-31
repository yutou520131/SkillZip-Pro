"""Strict cross-layer entailment for Phase-A bundle compression.

An environment contract is optional, explicit, and versioned.  SkillZip never
guesses that a host prompt or tool schema contains a requirement.  A unit is
removed only for an exact normalized match with compatible type, modality,
guard, tool signature, output fields, and resource scope.  The returned witness
is retained in the bundle audit report.
"""
from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .contract import Contract, Unit


def _norm(text: str) -> str:
    value = (text or "").lower().strip()
    value = re.sub(r"[`*_#>]+", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


@dataclass(frozen=True)
class EnvironmentGuarantee:
    id: str
    type: str
    content: str
    modality: str = "info"
    guard: str = ""
    resources: Tuple[str, ...] = ("*",)
    source: str = "environment-contract"
    tool: str = ""
    args: Tuple[str, ...] = ()
    fields: Tuple[str, ...] = ()
    validation: str = ""
    allow_workflow: bool = False

    @classmethod
    def from_json(cls, data: dict, index: int) -> "EnvironmentGuarantee":
        resources = data.get("resources", ["*"])
        if isinstance(resources, str):
            resources = [resources]
        return cls(
            id=str(data.get("id") or f"G{index}"),
            type=str(data.get("type") or "rule"),
            content=str(data.get("content") or ""),
            modality=str(data.get("modality") or "info"),
            guard=str(data.get("guard") or ""),
            resources=tuple(str(x) for x in resources),
            source=str(data.get("source") or "environment-contract"),
            tool=str(data.get("tool") or ""),
            args=tuple(str(x) for x in data.get("args", [])),
            fields=tuple(str(x) for x in data.get("fields", [])),
            validation=str(data.get("validation") or ""),
            allow_workflow=bool(data.get("allow_workflow", False)),
        )

    def to_json(self) -> dict:
        data = asdict(self)
        data["resources"] = list(self.resources)
        data["args"] = list(self.args)
        data["fields"] = list(self.fields)
        return data


@dataclass
class EnvironmentContract:
    version: int = 1
    guarantees: List[EnvironmentGuarantee] = field(default_factory=list)
    name: str = ""
    digest: str = ""

    def to_json(self) -> dict:
        return {
            "version": self.version,
            "name": self.name,
            "digest": self.digest,
            "guarantees": [g.to_json() for g in self.guarantees],
        }


def load_environment_contract(path: Optional[str]) -> EnvironmentContract:
    if not path:
        return EnvironmentContract()
    raw = Path(path).read_bytes()
    data = json.loads(raw.decode("utf-8"))
    if int(data.get("version", 1)) != 1:
        raise ValueError("unsupported environment contract version")
    import hashlib
    return EnvironmentContract(
        version=1,
        name=str(data.get("name") or Path(path).stem),
        digest=hashlib.sha256(raw).hexdigest(),
        guarantees=[EnvironmentGuarantee.from_json(g, i + 1)
                    for i, g in enumerate(data.get("guarantees", []))],
    )


def _resource_applies(guarantee: EnvironmentGuarantee, resource: str) -> bool:
    return any(fnmatch.fnmatch(resource, pattern) for pattern in guarantee.resources)


def _entailed(unit: Unit, guarantee: EnvironmentGuarantee, resource: str) -> bool:
    if not _resource_applies(guarantee, resource):
        return False
    if unit.type != guarantee.type or unit.modality != guarantee.modality:
        return False
    if unit.type == "workflow" and not guarantee.allow_workflow:
        return False
    if _norm(unit.content) != _norm(guarantee.content):
        return False
    if _norm(unit.guard) != _norm(guarantee.guard):
        return False
    if unit.type == "tool":
        if (unit.tool or "") != (guarantee.tool or ""):
            return False
        if set(unit.args) != set(guarantee.args):
            return False
    if unit.type == "output":
        if set(unit.fields) != set(guarantee.fields):
            return False
        if _norm(unit.validation) != _norm(guarantee.validation):
            return False
    return True


def apply_environment_contract(
    contract: Contract,
    resource: str,
    environment: EnvironmentContract,
) -> Tuple[Contract, List[Dict]]:
    """Remove strictly entailed units and return auditable witnesses."""
    drops: List[Dict] = []
    kept: List[Unit] = []
    for unit in contract.units:
        witness = next((g for g in environment.guarantees
                        if _entailed(unit, g, resource)), None)
        if witness is None:
            kept.append(unit)
            continue
        drops.append({
            "op": "DROP_ENTAILED",
            "resource": resource,
            "unit_id": unit.id,
            "unit_hash": unit.hash,
            "guarantee_id": witness.id,
            "guarantee_source": witness.source,
            "environment_digest": environment.digest,
        })
    contract.units = kept
    return contract, drops
