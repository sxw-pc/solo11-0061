"""Build explainable, dry-run-only repair plans and simulation documents."""

from __future__ import annotations

from typing import Any

from .asof import asof_stats
from .config import Contract
from .model import ActualVersion, AsOfCheck, Finding, PlanAction
from .simulate import SimulationOutcome, row_fingerprint
from .util import canonical_json

ACTION_MAP: dict[str, tuple[str, str, str]] = {
    "EVT_DUP_CONFLICT": (
        "REVIEW_EVENT_CONFLICT",
        "Inspect the retained event and conflicting duplicate before replaying again.",
        "low",
    ),
    "EVT_SAME_TIME_CONFLICT": (
        "REVIEW_EVENT_ORDER",
        "Confirm the business sequence for events sharing an effective timestamp.",
        "low",
    ),
    "DIM_OVERLAP": (
        "RESOLVE_OVERLAP",
        "Trim or close overlapping versions so every instant has at most one match.",
        "high",
    ),
    "DIM_GAP": (
        "BACKFILL_GAP",
        "Recover the missing interval from the expected replay or the source feed.",
        "medium",
    ),
    "DIM_DUPLICATE_VERSION": (
        "MERGE_DUPLICATE_VERSION",
        "Choose one version row and retire the duplicate after human review.",
        "medium",
    ),
    "DIM_MULTI_CURRENT": (
        "CLOSE_EXTRA_CURRENT",
        "Keep exactly one current version and end the extra open versions.",
        "high",
    ),
    "DIM_NO_CURRENT": (
        "CREATE_CURRENT_VERSION",
        "Recreate the open version from the latest expected state.",
        "high",
    ),
    "DIM_ATTR_DRIFT": (
        "REWRITE_VERSION_ATTRIBUTES",
        "Replace drifted attributes with the replayed expected snapshot.",
        "medium",
    ),
    "DIM_ORPHAN_ACTUAL": (
        "RETIRE_ORPHAN",
        "Confirm the key is out of scope, then retire its actual-only versions.",
        "medium",
    ),
    "DIM_MISSING_EXPECTED": (
        "CREATE_MISSING_VERSIONS",
        "Create the missing expected version chain for this business key.",
        "high",
    ),
    "DIM_INVALID_INTERVAL": (
        "REPAIR_INTERVAL",
        "Repair the row so valid_to is absent or strictly later than valid_from.",
        "medium",
    ),
    "DIM_CURRENT_FLAG": (
        "FIX_CURRENT_FLAG",
        "Align is_current with the open or closed shape of the interval.",
        "low",
    ),
    "DIM_CURRENT_SNAPSHOT_DRIFT": (
        "REBUILD_CURRENT_SNAPSHOT",
        "Rebuild the current snapshot from the reconciled open version rows.",
        "medium",
    ),
}


def build_plan(findings: list[Finding]) -> list[PlanAction]:
    actions: list[PlanAction] = []
    for finding in sorted(findings, key=lambda item: item.finding_id):
        kind, rationale, risk = ACTION_MAP.get(
            finding.code,
            (
                "MANUAL_REVIEW",
                "Investigate this finding manually; no automated action is proposed.",
                "medium",
            ),
        )
        details: dict[str, Any] = {
            "operation": kind,
            "execution_mode": "dry_run_only",
            "source_code": finding.code,
            "source_phase": finding.phase,
            "evidence": finding.details,
        }
        action = PlanAction(
            kind=kind,
            natural_key=finding.natural_key,
            start=finding.start,
            end=finding.end,
            finding_ids=(finding.finding_id,),
            rationale=rationale,
            risk=risk,
            review_required=True,
            details=details,
        )
        actions.append(action)
    for index, action in enumerate(actions, start=1):
        action.action_id = f"ACT-{index:04d}"
    return actions


def plan_stats(actions: list[PlanAction]) -> dict[str, Any]:
    by_kind: dict[str, int] = {}
    by_risk: dict[str, int] = {}
    for action in actions:
        by_kind[action.kind] = by_kind.get(action.kind, 0) + 1
        by_risk[action.risk] = by_risk.get(action.risk, 0) + 1
    return {
        "actions": len(actions),
        "review_required": sum(1 for action in actions if action.review_required),
        "by_kind": dict(sorted(by_kind.items())),
        "by_risk": dict(sorted(by_risk.items())),
    }


def build_simulation_document(
    contract: Contract,
    now: str,
    outcome: SimulationOutcome,
    plan: list[PlanAction],
    findings: list[Finding],
) -> dict[str, Any]:
    """Render a simulation outcome as JSON, preserving plan and finding evidence."""
    actions_by_id = {action.action_id: action for action in plan}
    findings_by_id = {finding.finding_id: finding for finding in findings}
    applied_by_id = {record.action_id: record for record in outcome.applied}
    step_docs: list[dict[str, Any]] = []
    selected: list[dict[str, Any]] = []
    for step in outcome.steps:
        record = applied_by_id[step.action_id]
        doc: dict[str, Any] = {
            "action_id": step.action_id,
            "op": step.op,
            "plan_action": step.plan_action,
            "depends_on": sorted(outcome.depends[step.action_id]),
            "preconditions": [
                {"row": ref, "fingerprint": step.fingerprints[ref]}
                for ref in step.targets
            ],
            "params": step.params,
            "before": [_image(ref, row) for ref, row in record.consumed.items()],
            "after": [_image(ref, row) for ref, row in record.produced.items()],
            "status": "applied",
        }
        action = actions_by_id.get(step.plan_action or "")
        if action is not None:
            doc["evidence"] = {
                "plan_action": action.to_dict(),
                "findings": [
                    findings_by_id[fid].to_dict()
                    for fid in action.finding_ids
                    if fid in findings_by_id
                ],
            }
            selected.append(action.to_dict())
        step_docs.append(doc)
    eliminated, introduced = _diff_findings(
        outcome.findings_before, outcome.findings_after
    )
    asof_eliminated, asof_introduced = _diff_asof(
        outcome.asof_before, outcome.asof_after
    )
    return {
        "schema_version": 1,
        "mode": "simulation_only",
        "generated_at": now,
        "table": contract.table,
        "contract_sha256": contract.sha256,
        "mutates_source": False,
        "generates_sql": False,
        "application_order": outcome.order,
        "steps": step_docs,
        "findings": {
            "before": len(outcome.findings_before),
            "after": len(outcome.findings_after),
            "eliminated": eliminated,
            "introduced": introduced,
        },
        "asof": {
            "before": asof_stats(outcome.asof_before),
            "after": asof_stats(outcome.asof_after),
            "eliminated": asof_eliminated,
            "introduced": asof_introduced,
        },
        "rollback": {
            "order": [record.action_id for record in reversed(outcome.applied)],
            "steps": [
                {
                    "action_id": record.action_id,
                    "op": record.op,
                    "remove": list(record.produced),
                    "restore": [
                        _image(ref, row) for ref, row in record.consumed.items()
                    ],
                }
                for record in reversed(outcome.applied)
            ],
        },
        "source_plan": {"actions_total": len(plan), "selected": selected},
    }


def _image(ref: str, row: ActualVersion) -> dict[str, Any]:
    return {
        "ref": ref,
        "row_no": row.row_no,
        "member_sk": row.member_sk,
        "natural_key": row.natural_key,
        "attributes": dict(sorted(row.attrs.items())),
        "valid_from": row.valid_from,
        "valid_to": row.valid_to,
        "is_current": row.is_current,
        "source": row.source,
        "updated_at": row.updated_at,
        "fingerprint": row_fingerprint(row),
    }


def _diff_findings(
    before: list[Finding], after: list[Finding]
) -> tuple[list[Any], list[Any]]:
    def key(finding: Finding) -> str:
        return canonical_json(
            [finding.code, finding.natural_key, finding.start, finding.end, finding.details]
        )

    before_map = {key(item): item for item in before}
    after_map = {key(item): item for item in after}
    eliminated = [
        before_map[name].to_dict() for name in sorted(before_map.keys() - after_map.keys())
    ]
    introduced = [
        after_map[name].to_dict() for name in sorted(after_map.keys() - before_map.keys())
    ]
    return eliminated, introduced


def _diff_asof(
    before: list[AsOfCheck], after: list[AsOfCheck]
) -> tuple[list[Any], list[Any]]:
    def key(check: AsOfCheck) -> str:
        return canonical_json([check.natural_key, check.at, check.status, check.differences])

    def doc(check: AsOfCheck) -> dict[str, Any]:
        return {
            "natural_key": check.natural_key,
            "at": check.at,
            "status": check.status,
            "differences": check.differences,
        }

    before_map = {key(item): item for item in before if item.status != "match"}
    after_map = {key(item): item for item in after if item.status != "match"}
    eliminated = [doc(before_map[name]) for name in sorted(before_map.keys() - after_map.keys())]
    introduced = [doc(after_map[name]) for name in sorted(after_map.keys() - before_map.keys())]
    return eliminated, introduced
