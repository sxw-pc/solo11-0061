"""Temporal parent-child hierarchy diagnostics over actual SCD2 history.

A parent edge exists only during the half-open interval of the row carrying
it; edges from different periods are never merged into one static graph. Time
is segmented into epochs with a constant live-row set. Overlapping versions
of one business key make its parent pointer ambiguous, so every candidate
resolution is checked: a cycle is ``necessary`` when it holds in every
resolution and ``possible`` when it holds in at least one. Enumeration is
factored per strongly connected component and capped by a budget; exceeding
the budget yields an explicit undecided finding instead of silent clearance.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from itertools import product
from math import prod
from typing import Any

from .config import Contract
from .model import ActualVersion, Finding

DEFAULT_MAX_RESOLUTIONS = 256
_CODES = {"cycle": "DIM_HIER_CYCLE", "cycle_aggregate": "DIM_HIER_CYCLE",
          "dangling": "DIM_HIER_DANGLING_PARENT",
          "ambiguity": "DIM_HIER_AMBIGUOUS_PARENT", "undecided": "DIM_HIER_UNDECIDED"}


@dataclass
class _Row:
    row_no: int
    member_sk: str
    natural_key: str
    parent: str
    valid_from: str
    valid_to: str | None

    def evidence(self) -> dict[str, Any]:
        return {"row_no": self.row_no, "member_sk": self.member_sk,
                "natural_key": self.natural_key, "valid_from": self.valid_from,
                "valid_to": self.valid_to, "parent": self.parent}


@dataclass
class _Fragment:
    kind: str
    identity: tuple  # (certainty, *kind-specific merge keys)
    start: str
    end: str | None
    details: dict[str, Any]


def diagnose_temporal_hierarchy(
    contract: Contract, actual: list[ActualVersion],
    max_resolutions: int = DEFAULT_MAX_RESOLUTIONS,
) -> list[Finding]:
    attribute = contract.parent_key_attribute
    if attribute is None:
        return []
    budget = max(1, max_resolutions)
    rows = [
        _Row(v.row_no, v.member_sk, v.natural_key, _parent_of(v, attribute),
             v.valid_from, v.valid_to)
        for v in actual if v.valid_to is None or v.valid_from < v.valid_to
    ]
    boundaries = sorted({row.valid_from for row in rows}
                        | {row.valid_to for row in rows if row.valid_to is not None})
    if not boundaries:
        return []
    epochs: list[tuple[str, str | None]] = list(zip(boundaries, boundaries[1:]))
    if any(row.valid_to is None for row in rows):
        epochs.append((boundaries[-1], None))
    fragments: list[_Fragment] = []
    for start, end in epochs:
        alive = [row for row in rows if row.valid_from <= start
                 and (row.valid_to is None or row.valid_to > start)]
        if alive:
            fragments.extend(_epoch_fragments(alive, start, end, budget))
    return _build_findings(fragments, attribute)


def _epoch_fragments(
    alive: list[_Row], start: str, end: str | None, budget: int
) -> list[_Fragment]:
    by_key: dict[str, list[_Row]] = defaultdict(list)
    for row in alive:
        by_key[row.natural_key].append(row)
    choices = {key: sorted({row.parent for row in items}) for key, items in by_key.items()}
    fragments: list[_Fragment] = []
    for key in sorted(by_key):
        if len(choices[key]) > 1:
            fragments.append(_Fragment(
                "ambiguity", (None, key, tuple(choices[key])), start, end,
                {"parent_values": choices[key], "rows": _evidence(by_key[key])}))
        for parent in choices[key]:
            if parent and parent not in by_key:
                certainty = "necessary" if choices[key] == [parent] else "possible"
                fragments.append(_Fragment("dangling", (certainty, key, parent),
                    start, end, {"parent_key": parent, "child_rows": _evidence(
                        [row for row in by_key[key] if row.parent == parent])}))
    graph = {key: {parent for parent in choices[key] if parent} for key in by_key}
    nodes = set(graph) | {parent for targets in graph.values() for parent in targets}
    for component in _components(nodes, graph):
        node = next(iter(component))
        if len(component) > 1 or node in graph.get(node, ()):
            fragments.extend(_cycle_fragments(component, by_key, choices, start, end, budget))
    return fragments


def _cycle_fragments(
    component: frozenset, by_key: dict[str, list[_Row]],
    choices: dict[str, list[str]], start: str, end: str | None, budget: int,
) -> list[_Fragment]:
    keys = sorted(component)
    space = prod(len(choices[key]) for key in keys)
    if space > budget:
        return [_Fragment("undecided", (None, tuple(keys), space, budget), start, end, {
            "component_keys": keys, "combinations": space, "budget": budget,
            "ambiguous_keys": [key for key in keys if len(choices[key]) > 1]})]
    instances: dict[tuple[str, ...], int] = {}
    cyclic = total = 0
    for combo in product(*(choices[key] for key in keys)):
        edges = {key: parent for key, parent in zip(keys, combo) if parent in component}
        cycles = _resolution_cycles(edges)
        cyclic += bool(cycles)
        total += 1
        for cycle in cycles:
            instances[cycle] = instances.get(cycle, 0) + 1
    fragments = []
    universal = False
    for cycle in sorted(instances):
        present = instances[cycle]
        certainty = "necessary" if present == total else "possible"
        universal = universal or present == total
        fragments.append(_Fragment("cycle", (certainty,) + cycle, start, end, {
            "cycle_keys": list(cycle), "resolution_counts": [[present, total]],
            "edges": _edge_evidence(list(_pairs(cycle)), by_key)}))
    if instances and cyclic == total and not universal:
        variants = sorted(instances)
        pairs = sorted({pair for cycle in variants for pair in _pairs(cycle)})
        fragments.append(_Fragment(
            "cycle_aggregate", ("necessary", "aggregate", tuple(keys)), start, end, {
                "component_keys": keys, "variants": [list(c) for c in variants],
                "edges": _edge_evidence(pairs, by_key),
                "resolution_counts": [[total, total]]}))
    return fragments


def _resolution_cycles(edges: dict[str, str]) -> list[tuple[str, ...]]:
    cycles: list[tuple[str, ...]] = []
    done: set[str] = set()
    for start in sorted(edges):
        if start in done:
            continue
        path, index = [], {}
        node = start
        while node in edges and node not in index and node not in done:
            index[node] = len(path)
            path.append(node)
            node = edges[node]
        if node in index:
            cycles.append(_canonical(path[index[node]:]))
        done.update(path)
    return cycles


def _canonical(cycle: list[str]) -> tuple[str, ...]:
    pivot = min(range(len(cycle)), key=cycle.__getitem__)
    return tuple(cycle[pivot:] + cycle[:pivot])


def _pairs(cycle: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    return tuple(zip(cycle, cycle[1:] + cycle[:1]))


def _components(nodes: set[str], graph: dict[str, set[str]]) -> list[frozenset]:
    reach: dict[str, set[str]] = {}
    for node in nodes:
        seen, stack = {node}, [node]
        while stack:
            for nxt in graph.get(stack.pop(), ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        reach[node] = seen
    components, assigned = [], set()
    for node in sorted(nodes):
        if node not in assigned:
            component = frozenset(other for other in nodes
                                  if other in reach[node] and node in reach[other])
            assigned |= component
            components.append(component)
    return components


def _build_findings(fragments: list[_Fragment], attribute: str) -> list[Finding]:
    groups: dict[tuple, list[_Fragment]] = defaultdict(list)
    for fragment in fragments:
        groups[(fragment.kind, fragment.identity)].append(fragment)
    findings: list[Finding] = []
    for (kind, _), group in groups.items():
        group.sort(key=lambda item: (item.start, item.end or "9999"))
        merged = [group[0]]
        for fragment in group[1:]:
            if merged[-1].end is not None and merged[-1].end == fragment.start:
                merged[-1] = _merge_fragment(merged[-1], fragment)
            else:
                merged.append(fragment)
        findings.extend(_to_finding(kind, item, attribute) for item in merged)
    return findings


def _merge_fragment(head: _Fragment, fragment: _Fragment) -> _Fragment:
    details = dict(head.details)
    if head.kind in {"cycle", "cycle_aggregate"}:
        details["edges"] = _merge_edges(head.details["edges"], fragment.details["edges"])
        details["resolution_counts"] = (
            head.details["resolution_counts"] + fragment.details["resolution_counts"])
        if head.kind == "cycle_aggregate":
            variants = {tuple(item) for item in head.details["variants"]}
            variants |= {tuple(item) for item in fragment.details["variants"]}
            details["variants"] = [list(item) for item in sorted(variants)]
    elif head.kind == "dangling":
        details["child_rows"] = _union_rows(head.details["child_rows"],
                                            fragment.details["child_rows"])
    elif head.kind == "ambiguity":
        details["rows"] = _union_rows(head.details["rows"], fragment.details["rows"])
    return _Fragment(head.kind, head.identity, head.start, fragment.end, details)


def _merge_edges(first: list[dict], second: list[dict]) -> list[dict]:
    merged: dict[tuple, dict] = {}
    for edge in first + second:
        slot = merged.setdefault(
            (edge["child"], edge["parent"]),
            {"child": edge["child"], "parent": edge["parent"],
             "child_rows": [], "parent_rows": []})
        slot["child_rows"] = _union_rows(slot["child_rows"], edge["child_rows"])
        slot["parent_rows"] = _union_rows(slot["parent_rows"], edge["parent_rows"])
    return [merged[key] for key in sorted(merged)]


def _union_rows(first: list[dict], second: list[dict]) -> list[dict]:
    seen = {row["row_no"]: row for row in first + second}
    return [seen[key] for key in sorted(seen)]


def _to_finding(kind: str, fragment: _Fragment, attribute: str) -> Finding:
    details = {"parent_attribute": attribute, **fragment.details}
    certainty = fragment.identity[0]
    if certainty is not None:
        details["certainty"] = certainty
    keys = fragment.details.get("cycle_keys") or fragment.details.get("component_keys") or []
    if kind == "cycle":
        natural_key = keys[0]
        severity = "high" if certainty == "necessary" else "medium"
        message = (f"hierarchy cycle {' -> '.join(keys + [keys[0]])} holds on "
                   f"common live intervals ({certainty})")
    elif kind == "cycle_aggregate":
        natural_key, severity = keys[0], "high"
        message = ("hierarchy cycle is unavoidable across ambiguous versions of "
                   + ", ".join(keys))
    elif kind == "dangling":
        natural_key = fragment.identity[1]
        severity = "medium" if certainty == "necessary" else "low"
        message = (f"parent key {fragment.details['parent_key']} has no live "
                   f"version while {natural_key} references it ({certainty})")
    elif kind == "ambiguity":
        natural_key, severity = fragment.identity[1], "medium"
        message = f"{natural_key} has live versions with different parent values"
    else:
        natural_key, severity = keys[0], "medium"
        message = ("cycle detection for " + ", ".join(keys)
                   + " exceeds the candidate budget; interval left undecided")
    return Finding(code=_CODES[kind], severity=severity, phase="diagnose",
                   natural_key=natural_key, start=fragment.start, end=fragment.end,
                   message=message, details=details)


def _parent_of(version: ActualVersion, attribute: str) -> str:
    raw = version.attrs.get(attribute)
    if raw is None:
        return ""
    return raw.strip() if isinstance(raw, str) else str(raw).strip()


def _evidence(rows: list[_Row]) -> list[dict[str, Any]]:
    return [row.evidence() for row in sorted(rows, key=lambda item: item.row_no)]


def _edge_evidence(
    pairs: list[tuple[str, str]], by_key: dict[str, list[_Row]]
) -> list[dict]:
    return [{
        "child": child, "parent": parent,
        "child_rows": _evidence(
            [row for row in by_key.get(child, []) if row.parent == parent]),
        "parent_rows": _evidence(by_key.get(parent, []))}
        for child, parent in pairs]
