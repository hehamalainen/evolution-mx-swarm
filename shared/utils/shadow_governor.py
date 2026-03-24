"""Passive governor recommendation engine for lane/rig control."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True, frozen=True)
class GovernorRecommendation:
    recommendation_type: str
    scope_type: str
    scope_id: str
    severity: str
    status: str
    reason: str
    attributes: dict[str, Any] = field(default_factory=dict)


def evaluate_shadow_governor(
    rig_telemetry: list[dict[str, Any]],
    budget_snapshot: dict[str, Any],
) -> list[GovernorRecommendation]:
    """Evaluate passive governor rules over current rig telemetry."""
    recommendations: list[GovernorRecommendation] = []
    frontier_frozen = bool(budget_snapshot.get("frontier_frozen", False))

    for rig in rig_telemetry:
        rig_id = str(rig.get("rig_id") or "unknown")
        task_count = int(rig.get("task_count") or 0)
        width = max(1, int(rig.get("width") or 1))
        pass_rate = float(rig.get("pass_rate", 0.0))
        blocked_ratio = float(rig.get("blocked_dependency_ratio", 0.0))
        acceptance_failure_rate = float(rig.get("acceptance_failure_rate", 0.0))
        false_success_rate = float(rig.get("false_success_rate", 0.0))
        frontier_spend_usd = float(rig.get("frontier_spend_usd", 0.0))
        frontier_budget_cap_usd = float(rig.get("frontier_budget_cap_usd", 0.0))

        if frontier_budget_cap_usd > 0 and (
            frontier_spend_usd >= frontier_budget_cap_usd or frontier_frozen
        ):
            recommendations.append(
                GovernorRecommendation(
                    recommendation_type="ECONOMIC_FREEZE",
                    scope_type="rig",
                    scope_id=rig_id,
                    severity="critical",
                    status="FROZEN",
                    reason=(
                        "Frontier spend reached the rig frontier budget cap; "
                        "frontier-capable lanes should be frozen."
                    ),
                    attributes={
                        "frontier_spend_usd": frontier_spend_usd,
                        "frontier_budget_cap_usd": frontier_budget_cap_usd,
                    },
                )
            )

        if task_count > 0 and blocked_ratio > 0.5:
            recommendations.append(
                GovernorRecommendation(
                    recommendation_type="DEPENDENCY_DRAIN",
                    scope_type="rig",
                    scope_id=rig_id,
                    severity="high",
                    status="DRAINING",
                    reason=(
                        "More than half of the rig's active tasks are blocked on dependencies; "
                        "execution width should drain until prerequisites settle."
                    ),
                    attributes={
                        "blocked_dependency_ratio": round(blocked_ratio, 4),
                        "task_count": task_count,
                    },
                )
            )

        if false_success_rate > 0.0 or acceptance_failure_rate > 0.2:
            recommendations.append(
                GovernorRecommendation(
                    recommendation_type="QA_DENSITY_SURGE",
                    scope_type="rig",
                    scope_id=rig_id,
                    severity="critical" if false_success_rate > 0.0 else "high",
                    status="THROTTLED",
                    reason=(
                        "Acceptance integrity is degrading; QA density should increase and "
                        "execution width should be throttled until the lane stabilizes."
                    ),
                    attributes={
                        "false_success_rate": round(false_success_rate, 4),
                        "acceptance_failure_rate": round(acceptance_failure_rate, 4),
                    },
                )
            )

        if (
            task_count > width * 3
            and pass_rate > 0.8
            and blocked_ratio < 0.5
            and acceptance_failure_rate < 0.2
            and not frontier_frozen
        ):
            recommendations.append(
                GovernorRecommendation(
                    recommendation_type="QUEUE_WIDEN",
                    scope_type="rig",
                    scope_id=rig_id,
                    severity="medium",
                    status="ACTIVE",
                    reason=(
                        "Queue depth is outpacing current rig width while pass rate remains healthy; "
                        "the rig is a candidate for safe widening."
                    ),
                    attributes={
                        "task_count": task_count,
                        "current_width": width,
                        "suggested_width": max(width + 1, int(math.ceil(width * 1.5))),
                        "pass_rate": round(pass_rate, 4),
                    },
                )
            )

    return recommendations
