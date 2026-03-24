from shared.schemas import (
    AcceptanceCriteria,
    ModelTier,
    RoutingTarget,
    TaskConstraints,
    TaskContext,
    TaskContract,
    TaskState,
    TaskStream,
    TaskType,
)
from shared.schemas.org_topology import CapabilityVector, RoutingHoldReason, build_lane_key
from shared.utils.routing import route_task_failures, route_task_shadow


def _failure_event(
    *,
    task_id: str,
    attempt_id: str,
    reason: str,
    timestamp: str,
    detail: str = "",
) -> dict[str, str]:
    return {
        "task_id": task_id,
        "attempt_id": attempt_id,
        "state_to": "settled_nack",
        "reason": reason,
        "detail": detail,
        "trace_id": "trace-123",
        "timestamp": timestamp,
    }


def test_route_task_failures_routes_capability_failures_to_frontier() -> None:
    events = [
        _failure_event(task_id="task-1", attempt_id="a3", reason="ERR_NO_DIFF", timestamp="2026-03-09T10:00:03+00:00"),
        _failure_event(task_id="task-1", attempt_id="a2", reason="ERR_NO_DIFF", timestamp="2026-03-09T10:00:02+00:00"),
        _failure_event(task_id="task-1", attempt_id="a1", reason="ERR_NO_DIFF", timestamp="2026-03-09T10:00:01+00:00"),
    ]

    decision = route_task_failures("task-1", events)

    assert decision is not None
    assert decision.target == RoutingTarget.FRONTIER
    assert decision.consecutive_failures == 3


def test_route_task_failures_routes_stable_check_failures_to_replan() -> None:
    events = [
        _failure_event(
            task_id="task-2",
            attempt_id="a3",
            reason="ERR_CHECK_FAIL",
            detail="Acceptance checks failed: pytest tests/test_jsonpeek.py -q -> failed",
            timestamp="2026-03-09T10:01:03+00:00",
        ),
        _failure_event(
            task_id="task-2",
            attempt_id="a2",
            reason="ERR_CHECK_FAIL",
            detail="Acceptance checks failed: pytest tests/test_jsonpeek.py -q -> failed",
            timestamp="2026-03-09T10:01:02+00:00",
        ),
        _failure_event(
            task_id="task-2",
            attempt_id="a1",
            reason="ERR_CHECK_FAIL",
            detail="Acceptance checks failed: pytest tests/test_jsonpeek.py -q -> failed",
            timestamp="2026-03-09T10:01:01+00:00",
        ),
    ]

    decision = route_task_failures("task-2", events)

    assert decision is not None
    assert decision.target == RoutingTarget.REPLAN
    assert decision.failure_signature is not None


def test_route_task_failures_routes_operational_failures_to_human_review() -> None:
    events = [
        _failure_event(task_id="task-3", attempt_id="a3", reason="ERR_QUEUE_LEASE", timestamp="2026-03-09T10:02:03+00:00"),
        _failure_event(task_id="task-3", attempt_id="a2", reason="ERR_QUEUE_LEASE", timestamp="2026-03-09T10:02:02+00:00"),
        _failure_event(task_id="task-3", attempt_id="a1", reason="ERR_QUEUE_LEASE", timestamp="2026-03-09T10:02:01+00:00"),
    ]

    decision = route_task_failures("task-3", events)

    assert decision is not None
    assert decision.target == RoutingTarget.HUMAN_REVIEW


def test_route_task_shadow_prefers_matching_feature_lane() -> None:
    task = TaskContract.create(
        task_type=TaskType.FEATURE,
        goal="Implement jsonpeek output formatting",
        context=TaskContext(files_hint=["tools/jsonpeek.py"]),
        acceptance=AcceptanceCriteria(checks=["pytest tests/test_jsonpeek.py -q"]),
        constraints=TaskConstraints(),
        stream=TaskStream.FEATURE,
        model_tier=ModelTier.LOCAL_OPEN_WEIGHT,
    )
    rig_plans = [
        {
            "rig_id": "rig-jsonpeek",
            "goal_cluster_node_ids": ["pkg-jsonpeek"],
            "frontier_budget_cap_usd": 5.0,
            "governance_owner_id": "governor:jsonpeek",
            "streams": [
                {
                    "stream_id": "jsonpeek-execution",
                    "stream_type": "execution",
                    "qa_density_ratio": 0.25,
                    "shards": [
                        {
                            "shard_id": "jsonpeek.execution",
                            "target_task_classes": ["feature"],
                            "supported_toolchains": ["python"],
                            "max_mutable_files": 2,
                            "concurrency_cap": 2,
                            "freeze_condition_threshold": 3,
                            "allowed_model_tiers": ["local_open_weight"],
                        }
                    ],
                }
            ],
        }
    ]
    capability_snapshot = {
        build_lane_key(
            rig_id="legacy",
            stream_id="feature",
            shard_id="feature.feature.local_open_weight",
            model_tier="local_open_weight",
            provider_model="ollama/mistral:7b",
        ): CapabilityVector(
            pass_rate=0.9,
            escalation_rate=0.1,
            task_class_strengths={"feature": 0.95},
            toolchain_strengths={"python": 0.9},
        )
    }

    decision = route_task_shadow(task, rig_plans, capability_snapshot)

    assert decision is not None
    assert decision.hold_reason is None
    assert decision.recommended_stream == "feature"
    assert decision.recommended_model_tier == "local_open_weight"
    assert decision.candidate_count == 1


def test_route_task_shadow_reports_dependency_blocked_hold() -> None:
    task = TaskContract.create(
        task_type=TaskType.FEATURE,
        goal="Implement slug cli",
        context=TaskContext(files_hint=["tools/slugcli.py"]),
        acceptance=AcceptanceCriteria(checks=["pytest tests/test_slugcli.py -q"]),
        constraints=TaskConstraints(),
        stream=TaskStream.FEATURE,
        model_tier=ModelTier.LOCAL_OPEN_WEIGHT,
        prerequisite_task_ids=["core-task"],
    )
    task.state = TaskState.BLOCKED
    rig_plans = [
        {
            "rig_id": "rig-slug",
            "goal_cluster_node_ids": ["pkg-slug"],
            "frontier_budget_cap_usd": 5.0,
            "governance_owner_id": "governor:slug",
            "streams": [
                {
                    "stream_id": "slug-execution",
                    "stream_type": "execution",
                    "qa_density_ratio": 0.25,
                    "shards": [
                        {
                            "shard_id": "slug.execution",
                            "target_task_classes": ["feature"],
                            "supported_toolchains": ["python"],
                            "max_mutable_files": 1,
                            "concurrency_cap": 1,
                            "freeze_condition_threshold": 3,
                            "allowed_model_tiers": ["local_open_weight"],
                        }
                    ],
                }
            ],
        }
    ]

    decision = route_task_shadow(task, rig_plans, {})

    assert decision is not None
    assert decision.hold_reason == RoutingHoldReason.DEPENDENCY_BLOCKED
    assert decision.recommended_lane_key is None
