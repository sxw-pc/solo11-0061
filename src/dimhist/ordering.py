"""Feasible-order analysis for committed transactions at each business moment.

The constraints are the user-declared ``depends_on`` edges between transactions
sharing a business moment. Cyclic dependencies quarantine the minimal mutually
dependent set; genuinely ambiguous orders (feasible orders that disagree on a
final value) quarantine the involved transactions instead of being masked by an
event_id lexicographic tie-break. Only after the result is proven identical for
every feasible order is a deterministic representative order chosen.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .model import EventRecord
from .util import canonical_json


@dataclass
class TxOrderWork:
    """A validated transaction awaiting or holding its feasible order."""

    declaration: Any
    key_events: dict[str, list[EventRecord]]
    key_payloads: dict[str, dict[str, Any]]
    order_index: int = 0
    classification: str = ""


def resolve_ordering(
    declared: dict[str, Any],
    works: dict[str, TxOrderWork],
    quarantined: dict[str, tuple[str, dict[str, Any]]],
) -> None:
    """Quarantine infeasible transactions and classify the survivors.

    ``works`` holds validated transactions; quarantined entries are moved out
    of it into ``quarantined`` with their reason and evidence. Survivors keep
    their moment's fold position in ``order_index`` and a ``unique`` /
    ``equivalent`` classification.
    """
    while True:
        by_moment = _moments_of(works)
        unmet = {
            tx_id: _unsatisfied(tx_id, works, declared, quarantined)
            for tx_id in works
        }
        unmet = {tx_id: deps for tx_id, deps in unmet.items() if deps}
        if unmet:
            for tx_id in sorted(unmet):
                works.pop(tx_id)
                quarantined[tx_id] = (
                    "unmet_dependency",
                    {"unsatisfied_dependencies": unmet[tx_id]},
                )
            continue
        cycles = _find_cycles(works, by_moment)
        if cycles:
            for tx_id in sorted(cycles):
                works.pop(tx_id)
                quarantined[tx_id] = ("dependency_cycle", {"cycle": cycles[tx_id]})
            continue
        conflicts = _find_order_conflicts(works, by_moment)
        if conflicts:
            for tx_id in sorted(conflicts):
                works.pop(tx_id)
                quarantined[tx_id] = (
                    "order_conflict",
                    {"conflicts": conflicts[tx_id]},
                )
            continue
        break
    for moment, tx_ids in _moments_of(works).items():
        edges = _intra_edges(works, tx_ids)
        order, unique = _kahn_order(tx_ids, edges)
        for index, tx_id in enumerate(order):
            works[tx_id].order_index = index
            works[tx_id].classification = "unique" if unique else "equivalent"


def _unsatisfied(
    tx_id: str,
    works: dict[str, TxOrderWork],
    declared: dict[str, Any],
    quarantined: dict[str, tuple[str, dict[str, Any]]],
) -> list[str]:
    moment = works[tx_id].declaration.effective_at
    bad: set[str] = set()
    for dep in works[tx_id].declaration.depends_on:
        target = declared.get(dep)
        if target is None or dep in quarantined or target.effective_at > moment:
            bad.add(dep)
    return sorted(bad)


def _find_cycles(
    works: dict[str, TxOrderWork], by_moment: dict[str, list[str]]
) -> dict[str, list[str]]:
    cycles: dict[str, list[str]] = {}
    for tx_ids in by_moment.values():
        edges = _intra_edges(works, tx_ids)
        reach = _reachability(tx_ids, edges)
        for tx_id in tx_ids:
            if tx_id in cycles:
                continue
            component = sorted(
                other
                for other in tx_ids
                if other in reach[tx_id] and tx_id in reach[other]
            )
            if component:
                for member in component:
                    cycles[member] = component
    return cycles


def _find_order_conflicts(
    works: dict[str, TxOrderWork], by_moment: dict[str, list[str]]
) -> dict[str, list[dict[str, Any]]]:
    conflicts: dict[str, list[dict[str, Any]]] = {}
    for tx_ids in by_moment.values():
        edges = _intra_edges(works, tx_ids)
        reach = _reachability(tx_ids, edges)
        keys = sorted(
            {key for tx_id in tx_ids for key in works[tx_id].key_payloads}
        )
        for key in keys:
            writers = [tx_id for tx_id in tx_ids if key in works[tx_id].key_payloads]
            if len(writers) < 2:
                continue
            attrs = sorted(
                {
                    attr
                    for tx_id in writers
                    for attr in works[tx_id].key_payloads[key]
                }
            )
            for attr in attrs:
                attr_writers = [
                    tx_id
                    for tx_id in writers
                    if attr in works[tx_id].key_payloads[key]
                ]
                values = {
                    canonical_json(works[tx_id].key_payloads[key][attr])
                    for tx_id in attr_writers
                }
                if len(values) < 2:
                    continue
                maximal = [
                    tx_id
                    for tx_id in attr_writers
                    if not any(
                        other != tx_id and other in reach[tx_id]
                        for other in attr_writers
                    )
                ]
                maximal_values = {
                    canonical_json(works[tx_id].key_payloads[key][attr])
                    for tx_id in maximal
                }
                if len(maximal_values) < 2:
                    continue
                detail = {
                    "member": key,
                    "attribute": attr,
                    "candidate_values": {
                        tx_id: works[tx_id].key_payloads[key][attr]
                        for tx_id in sorted(maximal)
                    },
                }
                for tx_id in maximal:
                    conflicts.setdefault(tx_id, []).append(detail)
    return conflicts


def _moments_of(works: dict[str, TxOrderWork]) -> dict[str, list[str]]:
    by_moment: dict[str, list[str]] = {}
    for tx_id, work in works.items():
        by_moment.setdefault(work.declaration.effective_at, []).append(tx_id)
    return {moment: sorted(ids) for moment, ids in sorted(by_moment.items())}


def _intra_edges(
    works: dict[str, TxOrderWork], tx_ids: list[str]
) -> dict[str, set[str]]:
    present = set(tx_ids)
    edges: dict[str, set[str]] = {tx_id: set() for tx_id in tx_ids}
    for tx_id in tx_ids:
        for dep in works[tx_id].declaration.depends_on:
            if dep in present:
                edges[dep].add(tx_id)
    return edges


def _reachability(
    tx_ids: list[str], edges: dict[str, set[str]]
) -> dict[str, set[str]]:
    reach: dict[str, set[str]] = {}
    for tx_id in tx_ids:
        seen: set[str] = set()
        stack = sorted(edges[tx_id])
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(sorted(edges[node] - seen))
        reach[tx_id] = seen
    return reach


def _kahn_order(
    tx_ids: list[str], edges: dict[str, set[str]]
) -> tuple[list[str], bool]:
    """One deterministic linear extension and whether it is the only one.

    The transaction_id tie-break is used only after the result has been proven
    identical for every feasible order, so it never masks business ambiguity.
    """
    indegree = {tx_id: 0 for tx_id in tx_ids}
    for successors in edges.values():
        for node in successors:
            indegree[node] += 1
    available = sorted(tx_id for tx_id in tx_ids if indegree[tx_id] == 0)
    order: list[str] = []
    unique = True
    while available:
        if len(available) > 1:
            unique = False
        node = available.pop(0)
        order.append(node)
        for successor in sorted(edges[node]):
            indegree[successor] -= 1
            if indegree[successor] == 0:
                available.append(successor)
        available.sort()
    return order, unique
