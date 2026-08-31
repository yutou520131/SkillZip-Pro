"""Multi-layer cost accounting for progressive skill bundles."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from statistics import mean
from typing import Dict, List

from .bundle import BundleGraph, frontmatter_fields
from .skill import approx_tokens


@dataclass
class BundleCost:
    catalog_tokens: int
    activation_tokens: int
    deployment_text_tokens: int
    deployment_bytes: int
    reachable_text_tokens: int
    path_count: int
    path_min_tokens: int
    path_mean_tokens: float
    path_max_tokens: int
    expected_execution_tokens: float

    def to_json(self) -> dict:
        data = asdict(self)
        data["path_mean_tokens"] = round(self.path_mean_tokens, 2)
        data["expected_execution_tokens"] = round(self.expected_execution_tokens, 2)
        return data


def _node_tokens(graph: BundleGraph) -> Dict[str, int]:
    return {path: approx_tokens(node.text or "") if node.text is not None else 0
            for path, node in graph.nodes.items()}


def _execution_paths(graph: BundleGraph, tokens: Dict[str, int]) -> List[int]:
    adj: Dict[str, List[str]] = {path: [] for path in graph.nodes}
    for edge in graph.local_edges():
        if edge.source in adj and edge.target in graph.nodes:
            adj[edge.source].append(edge.target)
    values: List[int] = []

    def walk(node: str, visited: frozenset, total: int) -> None:
        children = [child for child in sorted(set(adj.get(node, [])))
                    if child not in visited]
        if not children or len(values) >= 1000:
            values.append(total)
            return
        for child in children:
            walk(child, visited | {child}, total + tokens.get(child, 0))

    start = graph.entry
    walk(start, frozenset({start}), tokens.get(start, 0))
    return values or [tokens.get(start, 0)]


def measure_bundle_cost(graph: BundleGraph) -> BundleCost:
    tokens = _node_tokens(graph)
    root_text = graph.nodes[graph.entry].text or ""
    metadata = frontmatter_fields(root_text)
    catalog = approx_tokens(" ".join([
        metadata.get("name", ""), metadata.get("description", ""), graph.entry,
    ]))
    activation = tokens.get(graph.entry, 0)
    paths = _execution_paths(graph, tokens)
    deployment_tokens = sum(tokens.values())
    deployment_bytes = sum(node.size_bytes for node in graph.nodes.values())
    reachable_tokens = sum(tokens[path] for path, node in graph.nodes.items()
                           if node.reachable)
    path_mean = mean(paths)
    # This static proxy corresponds to root activation plus one graph path.
    # It is not called an empirical average because no tasks/traces are read.
    expected = catalog + path_mean
    return BundleCost(
        catalog_tokens=catalog,
        activation_tokens=activation,
        deployment_text_tokens=deployment_tokens,
        deployment_bytes=deployment_bytes,
        reachable_text_tokens=reachable_tokens,
        path_count=len(paths),
        path_min_tokens=min(paths),
        path_mean_tokens=path_mean,
        path_max_tokens=max(paths),
        expected_execution_tokens=expected,
    )


def compare_costs(original: BundleCost, compressed: BundleCost) -> dict:
    def ratio(after: float, before: float) -> float:
        return round(after / max(1.0, before), 4)

    return {
        "catalog_remaining_ratio": ratio(compressed.catalog_tokens,
                                           original.catalog_tokens),
        "activation_remaining_ratio": ratio(compressed.activation_tokens,
                                              original.activation_tokens),
        "deployment_text_remaining_ratio": ratio(
            compressed.deployment_text_tokens, original.deployment_text_tokens),
        "deployment_byte_remaining_ratio": ratio(
            compressed.deployment_bytes, original.deployment_bytes),
        "mean_path_remaining_ratio": ratio(compressed.path_mean_tokens,
                                            original.path_mean_tokens),
        "execution_proxy_remaining_ratio": ratio(
            compressed.expected_execution_tokens,
            original.expected_execution_tokens),
    }
