from datetime import UTC, datetime, timedelta

import pytest

from judge.service import GateReason, Judge, JudgeDecision
from shared.schemas import (
    AcceptanceCriteria,
    ModelTier,
    Requirement,
    RequirementStatus,
    RequirementType,
    RoutingTarget,
    Spec,
    TaskContext,
    TaskContract,
    TaskStream,
    TaskType,
)
from shared.utils.config import MXConfig
from shared.utils.metrics import MetricsCollector
from shared.utils.shadow_governor import GovernorRecommendation


class StubQueue:
    def __init__(self) -> None:
        self.published: list[TaskContract] = []

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def get_queue_depth(self) -> int:
        return 0

    async def publish(self, task: TaskContract, priority: int = 5) -> str:
        self.published.append(task)
        return f"msg-{task.id}"


class StubLLM:
    pass


class StubGitHub:
    def __init__(self) -> None:
        self.checked_refs: list[str] = []

    async def list_pull_requests(self, state: str = "open", head: str | None = None):
        if state == "open":
            return [
                {"title": "[task-a] Implement", "head": {"sha": "sha-ok"}},
                {"title": "Regular PR", "head": {"sha": "sha-ignore"}},
                {"title": "[task-b] Fix", "head": {"sha": "sha-fail"}},
            ]
        return [
            {"title": "[task-a] Implement", "merged_at": "2026-02-14T10:00:00+00:00"},
            {"title": "[task-c] Closed no merge", "merged_at": None},
        ]

    async def all_checks_passed(self, ref: str) -> bool:
        self.checked_refs.append(ref)
        return ref == "sha-ok"


@pytest.mark.asyncio
async def test_get_pr_metrics_filters_swarm_and_counts_ci() -> None:
    judge = Judge(config=MXConfig(), queue=StubQueue(), llm=StubLLM())
    judge._github = StubGitHub()  # type: ignore[assignment]

    metrics = await judge._get_pr_metrics()

    assert metrics == {
        "prs_open": 2,
        "prs_merged": 1,
        "prs_passing_ci": 1,
        "prs_failing_ci": 1,
    }


@pytest.mark.asyncio
async def test_should_reset_when_no_progress_timeout_elapsed() -> None:
    config = MXConfig()
    config.judge.no_progress_timeout_minutes = 1
    judge = Judge(config=config, queue=StubQueue(), llm=StubLLM())
    judge.state.no_progress_since = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()

    should_reset = await judge._should_reset({"failure_rate": 0.0})

    assert should_reset is True


@pytest.mark.asyncio
async def test_evaluate_escalates_on_high_failure_rate(monkeypatch) -> None:
    config = MXConfig()
    judge = Judge(config=config, queue=StubQueue(), llm=StubLLM())

    spec = Spec.create(prd_hash="hash")
    req = Requirement.create(
        req_type=RequirementType.FUNCTIONAL,
        title="Requirement",
        description="Desc",
        acceptance_criteria=["works"],
    )
    req.status = RequirementStatus.IN_PROGRESS
    req.assigned_tasks = ["task-replan"]
    spec.add_requirement(req)

    escalations: list[tuple] = []

    monkeypatch.setattr(judge, "_load_spec", lambda: spec)
    monkeypatch.setattr(judge, "_append_report", lambda report: None)

    async def fake_update(_spec: Spec) -> int:
        return 0

    async def fake_compute(_spec: Spec) -> dict:
        return {
            "requirements_total": 1,
            "requirements_completed": 0,
            "requirements_in_progress": 1,
            "requirements_pending": 0,
            "requirements_blocked": 0,
            "completion_percent": 10.0,
            "failure_rate": 0.9,
            "prs_open": 1,
            "prs_merged": 0,
            "prs_passing_ci": 0,
            "prs_failing_ci": 1,
            "queue_depth": 3,
        }

    async def fake_dod(_spec: Spec, _metrics: dict) -> bool:
        return False

    monkeypatch.setattr(judge, "_update_spec_from_prs", fake_update)
    monkeypatch.setattr(judge, "_compute_metrics", fake_compute)
    monkeypatch.setattr(judge, "_check_dod_met", fake_dod)
    monkeypatch.setattr(
        judge.escalation_engine,
        "_create_escalation",
        lambda *args, **kwargs: escalations.append((args, kwargs)),
    )

    report = await judge.evaluate()

    assert report.decision == JudgeDecision.ESCALATE
    assert report.reason_code == GateReason.ESCALATE_FAILURE_RATE
    assert "High failure rate" in report.reason
    assert any("Escalating to human" in action for action in report.actions_taken)
    assert len(escalations) == 1
    assert judge.state.consecutive_failures == 1


@pytest.mark.asyncio
async def test_evaluate_returns_missing_spec_reason_code(monkeypatch) -> None:
    judge = Judge(config=MXConfig(), queue=StubQueue(), llm=StubLLM())
    monkeypatch.setattr(judge, "_load_spec", lambda: None)

    report = await judge.evaluate()

    assert report.decision == JudgeDecision.ESCALATE
    assert report.reason_code == GateReason.MISSING_SPEC


@pytest.mark.asyncio
async def test_evaluate_continue_queue_empty_sets_reason_code(monkeypatch) -> None:
    judge = Judge(config=MXConfig(), queue=StubQueue(), llm=StubLLM())

    spec = Spec.create(prd_hash="hash")
    req = Requirement.create(
        req_type=RequirementType.FUNCTIONAL,
        title="Requirement",
        description="Desc",
        acceptance_criteria=["works"],
    )
    req.status = RequirementStatus.IN_PROGRESS
    req.assigned_tasks = ["task-human"]
    spec.add_requirement(req)

    monkeypatch.setattr(judge, "_load_spec", lambda: spec)
    monkeypatch.setattr(judge, "_append_report", lambda report: None)

    async def fake_update(_spec: Spec) -> int:
        return 0

    async def fake_compute(_spec: Spec) -> dict:
        return {
            "requirements_total": 1,
            "requirements_completed": 0,
            "requirements_in_progress": 1,
            "requirements_pending": 0,
            "requirements_blocked": 0,
            "completion_percent": 10.0,
            "failure_rate": 0.0,
            "prs_open": 0,
            "prs_merged": 0,
            "prs_passing_ci": 0,
            "prs_failing_ci": 0,
            "queue_depth": 0,
        }

    async def fake_dod(_spec: Spec, _metrics: dict) -> bool:
        return False

    async def fake_escalate(_metrics: dict) -> bool:
        return False

    async def fake_reset(_metrics: dict) -> bool:
        return False

    monkeypatch.setattr(judge, "_update_spec_from_prs", fake_update)
    monkeypatch.setattr(judge, "_compute_metrics", fake_compute)
    monkeypatch.setattr(judge, "_check_dod_met", fake_dod)
    monkeypatch.setattr(judge, "_should_escalate", fake_escalate)
    monkeypatch.setattr(judge, "_should_reset", fake_reset)

    report = await judge.evaluate()

    assert report.decision == JudgeDecision.CONTINUE
    assert report.reason_code == GateReason.CONTINUE_QUEUE_EMPTY


@pytest.mark.asyncio
async def test_evaluate_reports_shadow_governor_recommendations(monkeypatch) -> None:
    judge = Judge(config=MXConfig(), queue=StubQueue(), llm=StubLLM())

    spec = Spec.create(prd_hash="hash")
    req = Requirement.create(
        req_type=RequirementType.FUNCTIONAL,
        title="Requirement",
        description="Desc",
        acceptance_criteria=["works"],
    )
    req.status = RequirementStatus.IN_PROGRESS
    req.assigned_tasks = ["task-shadow-governor"]
    spec.add_requirement(req)

    monkeypatch.setattr(judge, "_load_spec", lambda: spec)
    monkeypatch.setattr(judge, "_append_report", lambda report: None)

    async def fake_update(_spec: Spec) -> int:
        return 0

    async def fake_compute(_spec: Spec) -> dict:
        return {
            "requirements_total": 1,
            "requirements_completed": 0,
            "requirements_in_progress": 1,
            "requirements_pending": 0,
            "requirements_blocked": 0,
            "completion_percent": 0.0,
            "failure_rate": 0.0,
            "prs_open": 0,
            "prs_merged": 0,
            "prs_passing_ci": 0,
            "prs_failing_ci": 0,
            "queue_depth": 0,
            "tasks_blocked": 0,
            "tasks_waiting_on_dependencies": 0,
        }

    async def fake_dod(_spec: Spec, _metrics: dict) -> bool:
        return False

    async def fake_escalate(_metrics: dict) -> bool:
        return False

    async def fake_reset(_metrics: dict) -> bool:
        return False

    monkeypatch.setattr(judge, "_update_spec_from_prs", fake_update)
    monkeypatch.setattr(judge, "_compute_metrics", fake_compute)
    monkeypatch.setattr(judge, "_check_dod_met", fake_dod)
    monkeypatch.setattr(judge, "_should_escalate", fake_escalate)
    monkeypatch.setattr(judge, "_should_reset", fake_reset)
    monkeypatch.setattr(judge, "_emit_routing_decisions", lambda decisions: [])
    monkeypatch.setattr(judge, "_collect_routing_decisions", lambda spec: [])
    monkeypatch.setattr(judge, "_emit_shadow_routing_decisions", lambda decisions: [])
    monkeypatch.setattr(judge, "_collect_shadow_routing_decisions", lambda spec: [])
    monkeypatch.setattr(
        judge,
        "_collect_shadow_governor_recommendations",
        lambda: [
            GovernorRecommendation(
                recommendation_type="QUEUE_WIDEN",
                scope_type="rig",
                scope_id="rig-backend",
                severity="medium",
                status="ACTIVE",
                reason="safe to widen",
            )
        ],
    )
    monkeypatch.setattr(judge, "_emit_shadow_governor_recommendations", lambda recs: recs)

    report = await judge.evaluate()

    assert any("Shadow governor emitted 1 recommendation" in action for action in report.actions_taken)


@pytest.mark.asyncio
async def test_evaluate_resets_on_deterministic_replan_route(monkeypatch) -> None:
    judge = Judge(config=MXConfig(), queue=StubQueue(), llm=StubLLM())

    spec = Spec.create(prd_hash="hash")
    req = Requirement.create(
        req_type=RequirementType.FUNCTIONAL,
        title="Requirement",
        description="Desc",
        acceptance_criteria=["works"],
    )
    req.status = RequirementStatus.IN_PROGRESS
    req.assigned_tasks = ["task-replan"]
    spec.add_requirement(req)

    monkeypatch.setattr(judge, "_load_spec", lambda: spec)
    monkeypatch.setattr(judge, "_append_report", lambda report: None)
    monkeypatch.setattr(judge, "_save_spec", lambda _spec: None)
    monkeypatch.setattr(
        judge.metrics_collector,
        "read_task_state_events",
        lambda limit=20000: [
            {
                "task_id": "task-replan",
                "attempt_id": "a3",
                "trace_id": "trace-replan",
                "state_to": "settled_nack",
                "reason": "ERR_CHECK_FAIL",
                "detail": "Acceptance checks failed: pytest tests/test_jsonpeek.py -q -> failed",
                "timestamp": "2026-03-09T10:01:03+00:00",
            },
            {
                "task_id": "task-replan",
                "attempt_id": "a2",
                "trace_id": "trace-replan",
                "state_to": "settled_nack",
                "reason": "ERR_CHECK_FAIL",
                "detail": "Acceptance checks failed: pytest tests/test_jsonpeek.py -q -> failed",
                "timestamp": "2026-03-09T10:01:02+00:00",
            },
            {
                "task_id": "task-replan",
                "attempt_id": "a1",
                "trace_id": "trace-replan",
                "state_to": "settled_nack",
                "reason": "ERR_CHECK_FAIL",
                "detail": "Acceptance checks failed: pytest tests/test_jsonpeek.py -q -> failed",
                "timestamp": "2026-03-09T10:01:01+00:00",
            },
        ],
    )
    monkeypatch.setattr(judge.metrics_collector, "read_routing_decisions", lambda limit=5000: [])

    async def fake_update(_spec: Spec) -> int:
        return 0

    async def fake_compute(_spec: Spec) -> dict:
        return {
            "requirements_total": 1,
            "requirements_completed": 0,
            "requirements_in_progress": 1,
            "requirements_pending": 0,
            "requirements_blocked": 0,
            "completion_percent": 10.0,
            "failure_rate": 0.1,
            "prs_open": 0,
            "prs_merged": 0,
            "prs_passing_ci": 0,
            "prs_failing_ci": 0,
            "queue_depth": 1,
        }

    async def fake_dod(_spec: Spec, _metrics: dict) -> bool:
        return False

    monkeypatch.setattr(judge, "_update_spec_from_prs", fake_update)
    monkeypatch.setattr(judge, "_compute_metrics", fake_compute)
    monkeypatch.setattr(judge, "_check_dod_met", fake_dod)

    report = await judge.evaluate()

    assert report.decision == JudgeDecision.RESET
    assert report.reason_code == GateReason.RESET_REPLAN_ROUTE


@pytest.mark.asyncio
async def test_evaluate_escalates_on_human_review_route(monkeypatch) -> None:
    judge = Judge(config=MXConfig(), queue=StubQueue(), llm=StubLLM())

    spec = Spec.create(prd_hash="hash")
    req = Requirement.create(
        req_type=RequirementType.FUNCTIONAL,
        title="Requirement",
        description="Desc",
        acceptance_criteria=["works"],
    )
    req.status = RequirementStatus.IN_PROGRESS
    req.assigned_tasks = ["task-human"]
    spec.add_requirement(req)

    monkeypatch.setattr(judge, "_load_spec", lambda: spec)
    monkeypatch.setattr(judge, "_append_report", lambda report: None)
    monkeypatch.setattr(judge, "_save_spec", lambda _spec: None)
    monkeypatch.setattr(
        judge.metrics_collector,
        "read_task_state_events",
        lambda limit=20000: [
            {
                "task_id": "task-human",
                "attempt_id": "a3",
                "trace_id": "trace-human",
                "state_to": "settled_nack",
                "reason": "ERR_QUEUE_LEASE",
                "detail": "lease expired while worker was active",
                "timestamp": "2026-03-09T10:02:03+00:00",
            },
            {
                "task_id": "task-human",
                "attempt_id": "a2",
                "trace_id": "trace-human",
                "state_to": "settled_nack",
                "reason": "ERR_QUEUE_LEASE",
                "detail": "lease expired while worker was active",
                "timestamp": "2026-03-09T10:02:02+00:00",
            },
            {
                "task_id": "task-human",
                "attempt_id": "a1",
                "trace_id": "trace-human",
                "state_to": "settled_nack",
                "reason": "ERR_QUEUE_LEASE",
                "detail": "lease expired while worker was active",
                "timestamp": "2026-03-09T10:02:01+00:00",
            },
        ],
    )
    monkeypatch.setattr(judge.metrics_collector, "read_routing_decisions", lambda limit=5000: [])

    async def fake_update(_spec: Spec) -> int:
        return 0

    async def fake_compute(_spec: Spec) -> dict:
        return {
            "requirements_total": 1,
            "requirements_completed": 0,
            "requirements_in_progress": 1,
            "requirements_pending": 0,
            "requirements_blocked": 0,
            "completion_percent": 10.0,
            "failure_rate": 0.1,
            "prs_open": 0,
            "prs_merged": 0,
            "prs_passing_ci": 0,
            "prs_failing_ci": 0,
            "queue_depth": 1,
        }

    async def fake_dod(_spec: Spec, _metrics: dict) -> bool:
        return False

    monkeypatch.setattr(judge, "_update_spec_from_prs", fake_update)
    monkeypatch.setattr(judge, "_compute_metrics", fake_compute)
    monkeypatch.setattr(judge, "_check_dod_met", fake_dod)

    report = await judge.evaluate()

    assert report.decision == JudgeDecision.ESCALATE
    assert report.reason_code == GateReason.ESCALATE_HUMAN_ROUTE


@pytest.mark.asyncio
async def test_evaluate_republishes_frontier_routes(monkeypatch) -> None:
    queue = StubQueue()
    judge = Judge(config=MXConfig(), queue=queue, llm=StubLLM())

    spec = Spec.create(prd_hash="hash")
    req = Requirement.create(
        req_type=RequirementType.FUNCTIONAL,
        title="Requirement",
        description="Desc",
        acceptance_criteria=["works"],
    )
    req.status = RequirementStatus.IN_PROGRESS
    req.assigned_tasks = ["task-frontier"]
    spec.add_requirement(req)

    task = TaskContract.create(
        task_type=TaskType.FEATURE,
        goal="Implement fallback behavior",
        context=TaskContext(files_hint=["tools/jsonpeek.py"]),
        acceptance=AcceptanceCriteria(criteria=["works"], checks=[]),
    )
    task.id = "task-frontier"
    task.frontier_eligible = True

    snapshots: list[TaskContract] = []

    monkeypatch.setattr(judge, "_load_spec", lambda: spec)
    monkeypatch.setattr(judge, "_append_report", lambda report: None)
    monkeypatch.setattr(judge, "_save_spec", lambda _spec: None)
    monkeypatch.setattr(judge, "_load_task_snapshot", lambda task_id: task if task_id == "task-frontier" else None)
    monkeypatch.setattr(judge, "_snapshot_task", lambda routed_task: snapshots.append(TaskContract.from_json(routed_task.to_json())))
    monkeypatch.setattr(
        judge.metrics_collector,
        "read_task_state_events",
        lambda limit=20000: [
            {
                "task_id": "task-frontier",
                "attempt_id": "a3",
                "trace_id": "trace-frontier",
                "state_to": "settled_nack",
                "reason": "ERR_NO_DIFF",
                "detail": "No file blocks produced",
                "timestamp": "2026-03-09T10:03:03+00:00",
            },
            {
                "task_id": "task-frontier",
                "attempt_id": "a2",
                "trace_id": "trace-frontier",
                "state_to": "settled_nack",
                "reason": "ERR_NO_DIFF",
                "detail": "No file blocks produced",
                "timestamp": "2026-03-09T10:03:02+00:00",
            },
            {
                "task_id": "task-frontier",
                "attempt_id": "a1",
                "trace_id": "trace-frontier",
                "state_to": "settled_nack",
                "reason": "ERR_NO_DIFF",
                "detail": "No file blocks produced",
                "timestamp": "2026-03-09T10:03:01+00:00",
            },
        ],
    )
    monkeypatch.setattr(judge.metrics_collector, "read_routing_decisions", lambda limit=5000: [])

    async def fake_update(_spec: Spec) -> int:
        return 0

    async def fake_compute(_spec: Spec) -> dict:
        return {
            "requirements_total": 1,
            "requirements_completed": 0,
            "requirements_in_progress": 1,
            "requirements_pending": 0,
            "requirements_blocked": 0,
            "completion_percent": 10.0,
            "failure_rate": 0.0,
            "prs_open": 0,
            "prs_merged": 0,
            "prs_passing_ci": 0,
            "prs_failing_ci": 0,
            "queue_depth": 1,
        }

    async def fake_dod(_spec: Spec, _metrics: dict) -> bool:
        return False

    async def fake_escalate(_metrics: dict) -> bool:
        return False

    async def fake_reset(_metrics: dict) -> bool:
        return False

    monkeypatch.setattr(judge, "_update_spec_from_prs", fake_update)
    monkeypatch.setattr(judge, "_compute_metrics", fake_compute)
    monkeypatch.setattr(judge, "_check_dod_met", fake_dod)
    monkeypatch.setattr(judge, "_should_escalate", fake_escalate)
    monkeypatch.setattr(judge, "_should_reset", fake_reset)

    report = await judge.evaluate()

    assert report.decision == JudgeDecision.CONTINUE
    assert report.reason_code == GateReason.CONTINUE_FRONTIER_ROUTE
    assert queue.published
    assert queue.published[0].id == "task-frontier"
    assert queue.published[0].routing_target == RoutingTarget.FRONTIER
    assert queue.published[0].model_tier.value == "frontier"
    assert snapshots
    assert any("Frontier route published for task-frontier" in action for action in report.actions_taken)


def test_update_governor_cache_tracks_queue_pr_and_ci_health() -> None:
    judge = Judge(config=MXConfig(), queue=StubQueue(), llm=StubLLM())

    judge._update_governor_cache(
        {
            "queue_depth": 7,
            "prs_open": 3,
            "prs_merged": 1,
            "prs_passing_ci": 4,
            "prs_failing_ci": 1,
        }
    )

    assert judge.state.last_queue_depth == 7
    assert judge.state.last_prs_open == 3
    assert judge.state.last_prs_merged == 1
    assert judge.state.last_ci_health_percent == 80.0


def test_get_telemetry_snapshot_uses_cached_real_values() -> None:
    judge = Judge(config=MXConfig(), queue=StubQueue(), llm=StubLLM())
    judge.metrics_collector = MetricsCollector()
    judge.state.last_queue_depth = 9
    judge.state.last_prs_open = 5
    judge.state.last_prs_merged = 2
    judge.state.last_ci_health_percent = 66.6

    snapshot = judge._get_telemetry_snapshot()

    assert snapshot.queue_depth == 9
    assert snapshot.prs_open_total == 5
    assert snapshot.prs_merged_1h == 2
    assert snapshot.ci_health_percent == 66.6


@pytest.mark.asyncio
async def test_evaluate_resets_requirement_when_all_assigned_tasks_dead_letter(monkeypatch) -> None:
    judge = Judge(config=MXConfig(), queue=StubQueue(), llm=StubLLM())

    spec = Spec.create(prd_hash="hash")
    req = Requirement.create(
        req_type=RequirementType.FUNCTIONAL,
        title="Handle missing files",
        description="Desc",
        acceptance_criteria=["works"],
    )
    req.status = RequirementStatus.IN_PROGRESS
    req.assigned_tasks = ["task-dead"]
    spec.add_requirement(req)

    monkeypatch.setattr(judge, "_load_spec", lambda: spec)
    monkeypatch.setattr(judge, "_append_report", lambda report: None)
    monkeypatch.setattr(judge, "_save_spec", lambda _spec: None)
    monkeypatch.setattr(
        judge.metrics_collector,
        "read_task_state_events",
        lambda limit=20000: [
            {
                "task_id": "task-dead",
                "state_to": "dead_letter",
                "reason": "ERR_CHECK_FAIL",
                "timestamp": datetime.now(UTC).isoformat(),
            }
        ],
    )

    async def fake_update(_spec: Spec) -> int:
        return 0

    async def fake_compute(_spec: Spec) -> dict:
        return {
            "requirements_total": 1,
            "requirements_completed": 0,
            "requirements_in_progress": 0,
            "requirements_pending": 1,
            "requirements_blocked": 0,
            "completion_percent": 0.0,
            "failure_rate": 0.0,
            "prs_open": 0,
            "prs_merged": 0,
            "prs_passing_ci": 0,
            "prs_failing_ci": 0,
            "queue_depth": 0,
        }

    async def fake_dod(_spec: Spec, _metrics: dict) -> bool:
        return False

    async def fake_escalate(_metrics: dict) -> bool:
        return False

    async def fake_reset(_metrics: dict) -> bool:
        return False

    monkeypatch.setattr(judge, "_update_spec_from_prs", fake_update)
    monkeypatch.setattr(judge, "_compute_metrics", fake_compute)
    monkeypatch.setattr(judge, "_check_dod_met", fake_dod)
    monkeypatch.setattr(judge, "_should_escalate", fake_escalate)
    monkeypatch.setattr(judge, "_should_reset", fake_reset)

    report = await judge.evaluate()

    assert report.decision == JudgeDecision.RESET
    assert report.reason_code == GateReason.RESET_STALLED_OR_FAILURE
    assert req.status == RequirementStatus.PENDING
    assert req.assigned_tasks == []
    assert any("dead-letter outcomes" in action for action in report.actions_taken)


def test_collect_and_emit_shadow_routing_decisions_is_passive(monkeypatch, tmp_path) -> None:
    judge = Judge(config=MXConfig(), queue=StubQueue(), llm=StubLLM(), repo_path=str(tmp_path))
    judge.teamplan.rig_plans = [
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
    task = TaskContract.create(
        task_type=TaskType.FEATURE,
        goal="Implement jsonpeek output",
        context=TaskContext(files_hint=["tools/jsonpeek.py"]),
        acceptance=AcceptanceCriteria(checks=["pytest tests/test_jsonpeek.py -q"]),
        stream=TaskStream.FEATURE,
        model_tier=ModelTier.LOCAL_OPEN_WEIGHT,
    )
    task.id = "task-shadow"
    task.trace_id = "trace-shadow"

    spec = Spec.create(prd_hash="hash")
    req = Requirement.create(
        req_type=RequirementType.FUNCTIONAL,
        title="Shadow route",
        description="Desc",
        acceptance_criteria=["works"],
    )
    req.status = RequirementStatus.IN_PROGRESS
    req.assigned_tasks = ["task-shadow"]
    spec.add_requirement(req)

    captured: list[dict] = []
    monkeypatch.setattr(judge.metrics_collector, "read_task_state_events", lambda limit=20000: [])
    monkeypatch.setattr(judge.metrics_collector, "read_routing_decisions", lambda limit=5000: [])
    monkeypatch.setattr(judge.capability_registry, "get_routing_snapshot", lambda: {})
    monkeypatch.setattr(
        "judge.service.load_task_snapshot",
        lambda task_id, repo_path=".": task if task_id == "task-shadow" else None,
    )
    monkeypatch.setattr(
        "judge.service.append_routing_decision",
        lambda **kwargs: captured.append(kwargs),
    )

    decisions = judge._collect_shadow_routing_decisions(spec)
    emitted = judge._emit_shadow_routing_decisions(decisions)

    assert len(decisions) == 1
    assert len(emitted) == 1
    assert captured
    assert captured[0]["actor_id"] == "shadow_scheduler"
    assert captured[0]["mode"] == "shadow_lane"
    assert captured[0]["actual_stream"] == "feature"
    assert captured[0]["recommended_stream"] == "feature"
