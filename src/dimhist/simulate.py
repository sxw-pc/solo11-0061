"""In-memory dry-run simulation of user-selected repair actions.

Validated actions are applied to a private copy of the actual dimension rows;
structural findings and as-of checks are recomputed on that copy. Source CSV
files, the audit ledger and the suggested plan are never modified, no SQL is
generated, and any validation failure discards the whole simulation.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, replace
from typing import Any

from .asof import reconcile_asof
from .config import Contract
from .diagnose import diagnose_actual
from .model import ActualVersion, AsOfCheck, ExpectedVersion, Finding
from .structural import merged_row, split_row, updated_row
from .util import DimhistError, canonical_json, normalize_time, sha256_text

OPS = ("split_interval", "update_attributes", "merge_duplicates")
_OUTPUT_COUNT = {"split_interval": 2, "update_attributes": 1, "merge_duplicates": 1}
_PARAMS = {
    "split_interval": {"at", "member_sk"},
    "update_attributes": {"set", "valid_from", "valid_to", "is_current"},
    "merge_duplicates": {"keep"},
}
_HEX = set("0123456789abcdef")


@dataclass
class AppliedStep:
    action_id: str
    op: str
    depends_on: tuple[str, ...]
    consumed: dict[str, ActualVersion]
    produced: dict[str, ActualVersion]


@dataclass
class SimulationOutcome:
    steps: list["_Step"]
    order: list[str]
    depends: dict[str, set[str]]
    state: dict[str, ActualVersion]
    applied: list[AppliedStep]
    findings_before: list[Finding]
    findings_after: list[Finding]
    asof_before: list[AsOfCheck]
    asof_after: list[AsOfCheck]


@dataclass
class _Step:
    action_id: str
    op: str
    targets: list[str]
    fingerprints: dict[str, str]
    params: dict[str, Any]
    plan_action: str | None
    index: int

    @property
    def outputs(self) -> tuple[str, ...]:
        count = _OUTPUT_COUNT[self.op]
        return tuple(f"{self.action_id}#{n}" for n in range(1, count + 1))


def row_fingerprint(row: ActualVersion) -> str:
    """Content fingerprint used as the per-row application precondition."""
    payload = {
        "member_sk": row.member_sk,
        "natural_key": row.natural_key,
        "attributes": dict(sorted(row.attrs.items())),
        "valid_from": row.valid_from,
        "valid_to": row.valid_to,
        "is_current": row.is_current,
    }
    return sha256_text(canonical_json(payload))


def simulate_repairs(
    contract: Contract,
    expected: list[ExpectedVersion],
    actual: list[ActualVersion],
    now: str,
    request: dict[str, Any],
) -> SimulationOutcome:
    steps = _parse_steps(request)
    originals: dict[str, ActualVersion] = {}
    for row in actual:
        ref = f"row:{row.row_no}"
        if ref in originals:
            raise DimhistError("SIM_001", f"duplicate source row_no: {row.row_no}")
        originals[ref] = replace(row, attrs=dict(row.attrs))
    order, depends = _resolve_order(steps, set(originals))
    state, applied = _apply_all(contract, order, depends, originals)
    moment = normalize_time(now, code="SIM_001", field="now")
    before = list(originals.values())
    after = list(state.values())
    return SimulationOutcome(
        steps=steps,
        order=[step.action_id for step in order],
        depends=depends,
        state=state,
        applied=applied,
        findings_before=diagnose_actual(contract, expected, before),
        findings_after=diagnose_actual(contract, expected, after),
        asof_before=reconcile_asof(contract, expected, before, moment),
        asof_after=reconcile_asof(contract, expected, after, moment),
    )


def rollback_state(
    state: dict[str, ActualVersion], applied: list[AppliedStep]
) -> dict[str, ActualVersion]:
    """Undo applied steps in reverse order, restoring the original rows."""
    restored = dict(state)
    for record in reversed(applied):
        for ref in record.produced:
            if ref not in restored:
                raise DimhistError("SIM_007", f"rollback cannot find produced row {ref}")
            del restored[ref]
        for ref, row in record.consumed.items():
            if ref in restored:
                raise DimhistError("SIM_007", f"rollback would duplicate row {ref}")
            restored[ref] = row
    return restored


def _parse_steps(request: Any) -> list[_Step]:
    if not isinstance(request, dict) or not isinstance(request.get("steps"), list):
        raise DimhistError("SIM_001", "request must be an object with a steps list")
    steps: list[_Step] = []
    for index, raw in enumerate(request["steps"]):
        if not isinstance(raw, dict):
            raise DimhistError("SIM_001", f"step {index + 1} must be an object")
        action_id = raw.get("id") or f"SIM-{index + 1:04d}"
        known = {step.action_id for step in steps}
        if not isinstance(action_id, str) or not action_id or action_id in known:
            raise DimhistError("SIM_001", f"step {index + 1} has a missing or duplicate id")
        op = raw.get("op")
        if op not in OPS:
            raise DimhistError("SIM_001", f"step {action_id} has unknown op: {op!r}")
        params = raw.get("params", {})
        if not isinstance(params, dict) or set(params) - _PARAMS[op]:
            raise DimhistError("SIM_001", f"step {action_id} has invalid params")
        targets, fingerprints = _parse_targets(action_id, raw.get("targets"))
        if (op == "merge_duplicates") != (len(targets) > 1):
            raise DimhistError("SIM_001", f"step {action_id} has the wrong target count")
        plan_action = raw.get("plan_action")
        if plan_action is not None and not isinstance(plan_action, str):
            raise DimhistError("SIM_001", f"step {action_id} plan_action must be a string")
        steps.append(_Step(action_id, op, targets, fingerprints, params, plan_action, index))
    return steps


def _parse_targets(action_id: str, raw: Any) -> tuple[list[str], dict[str, str]]:
    if not isinstance(raw, list) or not raw:
        raise DimhistError("SIM_001", f"step {action_id} needs a non-empty targets list")
    targets: list[str] = []
    fingerprints: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise DimhistError("SIM_001", f"step {action_id} target must be an object")
        ref = _normalize_ref(item.get("row"), action_id)
        fingerprint = item.get("fingerprint")
        if not isinstance(fingerprint, str) or len(fingerprint) != 64 or set(fingerprint) - _HEX:
            raise DimhistError(
                "SIM_001", f"step {action_id} row {ref} needs a sha256 fingerprint"
            )
        if ref in fingerprints:
            raise DimhistError("SIM_001", f"step {action_id} lists row {ref} twice")
        targets.append(ref)
        fingerprints[ref] = fingerprint
    return targets, fingerprints


def _normalize_ref(value: Any, action_id: str) -> str:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return f"row:{value}"
    if isinstance(value, str) and value:
        return value
    raise DimhistError("SIM_001", f"step {action_id} has an invalid row reference")


def _resolve_order(
    steps: list[_Step], original_refs: set[str]
) -> tuple[list[_Step], dict[str, set[str]]]:
    producer = {ref: step.action_id for step in steps for ref in step.outputs}
    depends: dict[str, set[str]] = {step.action_id: set() for step in steps}
    owner: dict[str, str] = {}
    for step in steps:
        for ref in step.targets:
            if ref in owner:
                raise DimhistError(
                    "SIM_004",
                    f"row {ref} is consumed by both {owner[ref]} and {step.action_id}",
                )
            owner[ref] = step.action_id
            if ref in original_refs:
                continue
            if ref not in producer:
                raise DimhistError(
                    "SIM_002", f"step {step.action_id} references unknown row {ref}"
                )
            depends[step.action_id].add(producer[ref])
    dependents: dict[str, list[str]] = {step.action_id: [] for step in steps}
    indegree = {step.action_id: len(depends[step.action_id]) for step in steps}
    for step in steps:
        for source in depends[step.action_id]:
            dependents[source].append(step.action_id)
    by_id = {step.action_id: step for step in steps}
    ready = [(step.index, step.action_id) for step in steps if not depends[step.action_id]]
    heapq.heapify(ready)
    order: list[_Step] = []
    while ready:
        _, action_id = heapq.heappop(ready)
        order.append(by_id[action_id])
        for follower in dependents[action_id]:
            indegree[follower] -= 1
            if indegree[follower] == 0:
                heapq.heappush(ready, (by_id[follower].index, follower))
    if len(order) != len(steps):
        remaining = sorted(aid for aid, degree in indegree.items() if degree > 0)
        raise DimhistError("SIM_005", f"cyclic action dependencies: {', '.join(remaining)}")
    return order, depends


def _apply_all(
    contract: Contract,
    order: list[_Step],
    depends: dict[str, set[str]],
    originals: dict[str, ActualVersion],
) -> tuple[dict[str, ActualVersion], list[AppliedStep]]:
    state = dict(originals)
    next_row_no = max((row.row_no for row in originals.values()), default=0) + 1
    applied: list[AppliedStep] = []
    for step in order:
        consumed = {ref: _precondition_row(state, ref, step) for ref in step.targets}
        rows = list(consumed.values())
        if step.op == "split_interval":
            pair = split_row(
                rows[0], step.params.get("at"), step.params.get("member_sk"),
                next_row_no, step.action_id,
            )
            produced = dict(zip(step.outputs, pair))
            next_row_no += 1
        elif step.op == "update_attributes":
            row = updated_row(contract, rows[0], step.params, step.action_id)
            produced = {step.outputs[0]: row}
        else:
            keeper = rows[0]
            if "keep" in step.params:
                ref = _normalize_ref(step.params["keep"], step.action_id)
                if ref not in consumed:
                    raise DimhistError(
                        "SIM_006", f"step {step.action_id} keep must name a target row"
                    )
                keeper = consumed[ref]
            produced = {step.outputs[0]: merged_row(rows, keeper, step.action_id)}
        for ref in step.targets:
            del state[ref]
        state.update(produced)
        applied.append(
            AppliedStep(
                step.action_id, step.op, tuple(sorted(depends[step.action_id])),
                consumed, produced,
            )
        )
    return state, applied


def _precondition_row(
    state: dict[str, ActualVersion], ref: str, step: _Step
) -> ActualVersion:
    row = state.get(ref)
    if row is None:
        raise DimhistError("SIM_003", f"row {ref} required by step {step.action_id} is gone")
    fingerprint = row_fingerprint(row)
    if fingerprint != step.fingerprints[ref]:
        raise DimhistError(
            "SIM_003",
            f"stale fingerprint for row {ref} in step {step.action_id}: "
            f"expected {step.fingerprints[ref][:12]}…, actual {fingerprint[:12]}…",
        )
    return row
