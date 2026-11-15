"""Build an explainable, dry-run-only repair plan from findings."""

from __future__ import annotations

from typing import Any

from .model import Finding, PlanAction

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
    "DIM_HIER_CYCLE": (
        "BREAK_HIERARCHY_CYCLE",
        "Review the co-existing parent edges and choose the correct parent "
        "manually; no parent key is rewritten automatically.",
        "high",
    ),
    "DIM_HIER_DANGLING_PARENT": (
        "RESOLVE_DANGLING_PARENT",
        "Confirm whether the parent key should be live during the edge "
        "interval; correct the reference by manual review only.",
        "medium",
    ),
    "DIM_HIER_AMBIGUOUS_PARENT": (
        "RESOLVE_PARENT_AMBIGUITY",
        "Reduce overlapping versions so each instant has exactly one parent "
        "value for the business key.",
        "medium",
    ),
    "DIM_HIER_UNDECIDED": (
        "REVIEW_HIERARCHY_MANUALLY",
        "Candidate combinations exceeded the budget; inspect the listed keys "
        "and interval manually before trusting the hierarchy.",
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
