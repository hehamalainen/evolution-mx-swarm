"""Judge Service - Progress evaluation and orchestration.

The Judge is responsible for:
1. Periodically evaluating CI + progress
2. Computing completion status vs SPEC
3. Deciding: Continue, Reset, Stop, or Escalate
4. Triggering replanning when needed
5. Appending decisions to JUDGE_REPORT.md
"""

import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum

from boss import (
    Adjustment,
    EscalationEngine,
    EscalationSeverity,
    EscalationType,
    Governor,
    TelemetrySnapshot,
)
from shared.adapters import QueueAdapter, create_queue_adapter
from shared.schemas import (
    ModelTier,
    RequirementStatus,
    RoutingTarget,
    Spec,
    TaskState,
)
from shared.schemas.teamplan import GovernorMode, TeamPlan
from shared.utils import (
    GitHubAPI,
    LLMClient,
    MockGitHubAPI,
    MXConfig,
    create_llm_client,
    get_capability_registry,
    load_task_snapshot,
    snapshot_task,
)
from shared.utils.control_plane import compute_rig_telemetry
from shared.utils.metrics import (
    append_decision_event,
    append_governor_event,
    append_routing_decision,
    append_trace_span,
    get_metrics_collector,
)
from shared.utils.routing import (
    RoutingDecision,
    ShadowRoutingDecision,
    route_task_failures,
    route_task_shadow,
)
from shared.utils.shadow_governor import GovernorRecommendation, evaluate_shadow_governor

from .reputation import ReputationManager


class JudgeDecision(str, Enum):
    CONTINUE = "continue"  # Keep going, optionally replan some tasks
    RESET = "reset"  # Full reset - restart workers, recompile plan
    STOP = "stop"  # DoD met, stop swarm
    ESCALATE = "escalate"  # Ask human for help


class GateReason(str, Enum):
    """Machine-readable control-loop reason for each judge decision."""

    MISSING_SPEC = "MISSING_SPEC"
    DOD_MET = "DOD_MET"
    ESCALATE_FAILURE_RATE = "ESCALATE_FAILURE_RATE"
    RESET_STALLED_OR_FAILURE = "RESET_STALLED_OR_FAILURE"
    CONTINUE_PROGRESS = "CONTINUE_PROGRESS"
    CONTINUE_QUEUE_EMPTY = "CONTINUE_QUEUE_EMPTY"
    CONTINUE_FRONTIER_ROUTE = "CONTINUE_FRONTIER_ROUTE"
    RESET_REPLAN_ROUTE = "RESET_REPLAN_ROUTE"
    ESCALATE_HUMAN_ROUTE = "ESCALATE_HUMAN_ROUTE"




@dataclass
class GovernorReport:
    """Report from a governor check."""
    timestamp: str
    state: GovernorMode
    actions: list[str]
    metrics: dict

    def to_markdown(self) -> str:
        return f"""
## Governor Report - {self.timestamp}

**State:** {self.state.value}

### Actions
{chr(10).join(f'- {a}' for a in self.actions) if self.actions else '- No actions taken'}

### Metrics
| Metric | Value |
|--------|-------|
| CI Health | {self.metrics.get('ci_health', 0):.1%} |
| Open PRs | {self.metrics.get('open_prs', 0)} |
| PR Rate (1h) | {self.metrics.get('pr_rate_1h', 0)} |
| Retries (1h) | {self.metrics.get('retries_1h', 0)} |

---
"""


@dataclass
class JudgeReport:
    """Report from a judge evaluation."""
    timestamp: str
    decision: JudgeDecision
    reason_code: GateReason
    reason: str
    metrics: dict
    actions_taken: list[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        """Generate markdown for JUDGE_REPORT.md."""
        return f"""
## Judge Report - {self.timestamp}

**Decision:** {self.decision.value.upper()}

**Reason Code:** `{self.reason_code.value}`

**Reason:** {self.reason}

### Metrics
| Metric | Value |
|--------|-------|
| Requirements Total | {self.metrics.get('requirements_total', 0)} |
| Requirements Completed | {self.metrics.get('requirements_completed', 0)} |
| Completion % | {self.metrics.get('completion_percent', 0)}% |
| Tasks Blocked | {self.metrics.get('tasks_blocked', 0)} |
| Dependency Wait Tasks | {self.metrics.get('tasks_waiting_on_dependencies', 0)} |
| PRs Open | {self.metrics.get('prs_open', 0)} |
| PRs Merged | {self.metrics.get('prs_merged', 0)} |
| Queue Depth | {self.metrics.get('queue_depth', 0)} |
| Failure Rate | {self.metrics.get('failure_rate', 0):.1%} |

### Actions Taken
{chr(10).join(f'- {a}' for a in self.actions_taken) if self.actions_taken else '- No actions taken'}

---
"""


@dataclass
class JudgeState:
    """Judge internal state."""
    last_run: str | None = None
    last_progress: float = 0.0
    last_ci_health_percent: float = 100.0
    last_queue_depth: int = 0
    last_prs_open: int = 0
    last_prs_merged: int = 0
    no_progress_since: str | None = None
    consecutive_failures: int = 0
    total_evaluations: int = 0
    governor_state: GovernorMode = GovernorMode.NORMAL
    last_governor_check: str | None = None


class Judge:
    """
    Judge service for MX Swarm.

    Evaluates progress, makes decisions, and orchestrates the swarm.
    """

    def __init__(
        self,
        config: MXConfig,
        queue: QueueAdapter | None = None,
        llm: LLMClient | None = None,
        repo_path: str = ".",
    ):
        self.config = config
        self.queue = queue or create_queue_adapter(config.queue.to_dict())
        self.llm = llm or create_llm_client(config.llm.to_dict())
        self.repo_path = repo_path
        self.state = JudgeState()
        self._github: GitHubAPI | MockGitHubAPI | None = None
        self._running = False
        self.metrics_collector = get_metrics_collector()
        self.capability_registry = get_capability_registry(repo_path=repo_path)
        self.escalation_engine = EscalationEngine(repo_path=repo_path)

        # Initialize Governor with a default TeamPlan if none exists
        teamplan_path = os.path.join(repo_path, "TEAMPLAN.json")
        if os.path.exists(teamplan_path):
            self.teamplan = TeamPlan.load(teamplan_path)
        else:
            self.teamplan = TeamPlan.default()

        self.governor = Governor(
            teamplan=self.teamplan,
            telemetry_provider=self._get_telemetry_snapshot,
            on_adjustment=self._on_governor_adjustment
        )
        self.reputation = ReputationManager(
            db_path=os.path.join(repo_path, ".runtime", "reputation_db.json")
        )

    @property
    def github(self) -> GitHubAPI | MockGitHubAPI:
        """Lazy-initialize GitHub API."""
        if self._github is None:
            token = self.config.github.token or os.environ.get("GITHUB_TOKEN")
            if not token:
                # Use Mock API in Local Mode
                print("[Judge] No GITHUB_TOKEN found - using Mock GitHub API (Local Mode)")
                self._github = MockGitHubAPI()
            else:
                self._github = GitHubAPI(
                    token=token,
                    owner=self.config.github.repo_owner,
                    repo=self.config.github.repo_name,
                )
        return self._github

    async def initialize(self) -> None:
        """Initialize judge connections."""
        await self.queue.connect()

    async def shutdown(self) -> None:
        """Cleanup judge connections."""
        self._running = False
        await self.queue.disconnect()

    def _load_spec(self) -> Spec | None:
        """Load SPEC.json from repo."""
        spec_path = os.path.join(self.repo_path, "SPEC.json")
        if os.path.exists(spec_path):
            return Spec.load(spec_path)
        return None

    def _save_spec(self, spec: Spec) -> None:
        """Save SPEC.json to repo."""
        spec_path = os.path.join(self.repo_path, "SPEC.json")
        spec.save(spec_path)

    def _append_report(self, report: JudgeReport) -> None:
        """Append report to JUDGE_REPORT.md."""
        report_path = os.path.join(self.repo_path, "JUDGE_REPORT.md")

        # Create file if doesn't exist
        if not os.path.exists(report_path):
            with open(report_path, "w") as f:
                f.write("# MX Swarm Judge Reports\n\n")
                f.write("This file contains automated judge evaluations and decisions.\n\n")

        # Append report
        with open(report_path, "a") as f:
            f.write(report.to_markdown())

    async def _get_pr_metrics(self) -> dict:
        """Get PR-related metrics from GitHub."""
        try:
            open_prs = await self.github.list_pull_requests(state="open")
            merged_prs = await self.github.list_pull_requests(state="closed")

            # Filter for swarm PRs (have task ID in title)
            swarm_open = [p for p in open_prs if "[" in p.get("title", "")]
            swarm_merged = [
                p for p in merged_prs
                if "[" in p.get("title", "") and p.get("merged_at")
            ]

            # Check CI status for open PRs
            prs_passing = 0
            prs_failing = 0
            for pr in swarm_open[:10]:  # Check first 10
                ref = pr.get("head", {}).get("sha")
                if ref:
                    passed = await self.github.all_checks_passed(ref)
                    if passed:
                        prs_passing += 1
                    else:
                        prs_failing += 1

            return {
                "prs_open": len(swarm_open),
                "prs_merged": len(swarm_merged),
                "prs_passing_ci": prs_passing,
                "prs_failing_ci": prs_failing,
            }

        except Exception as e:
            print(f"Warning: Could not fetch PR metrics: {e}")
            return {
                "prs_open": 0,
                "prs_merged": 0,
                "prs_passing_ci": 0,
                "prs_failing_ci": 0,
            }

    async def _get_queue_metrics(self) -> dict:
        """Get queue-related metrics."""
        try:
            depth = await self.queue.get_queue_depth()
            return {"queue_depth": depth if depth >= 0 else "unknown"}
        except Exception:
            return {"queue_depth": "unknown"}

    async def _compute_metrics(self, spec: Spec) -> dict:
        """Compute all metrics for evaluation."""
        progress = spec.progress_summary()
        pr_metrics = await self._get_pr_metrics()
        queue_metrics = await self._get_queue_metrics()
        task_events = self.metrics_collector.read_task_state_events(limit=50000)
        latest_task_events: dict[str, dict] = {}
        for event in task_events:
            task_id = str(event.get("task_id", "")).strip()
            if task_id:
                latest_task_events[task_id] = event
        blocked_tasks = sum(
            1
            for event in latest_task_events.values()
            if str(event.get("state_to", "")).strip().lower() == TaskState.BLOCKED.value
        )
        dependency_wait_tasks = sum(
            1
            for event in latest_task_events.values()
            if str(event.get("state_to", "")).strip().lower() == TaskState.BLOCKED.value
            and str(event.get("reason", "")).strip().upper() == "DEPENDENCY_WAIT"
        )

        total_tasks = sum(len(r.assigned_tasks) for r in spec.requirements)
        failed_tasks = sum(
            1 for r in spec.requirements
            if r.status == RequirementStatus.BLOCKED
        )

        failure_rate = failed_tasks / total_tasks if total_tasks > 0 else 0

        return {
            "requirements_total": progress["total"],
            "requirements_completed": progress["completed"],
            "requirements_in_progress": progress["in_progress"],
            "requirements_pending": progress["pending"],
            "requirements_blocked": progress.get("blocked", 0),
            "tasks_blocked": blocked_tasks,
            "tasks_waiting_on_dependencies": dependency_wait_tasks,
            "completion_percent": progress["percent"],
            "failure_rate": failure_rate,
            **pr_metrics,
            **queue_metrics,
        }

    async def _check_dod_met(self, spec: Spec, metrics: dict) -> bool:
        """Check if Definition of Done is met."""
        # All requirements completed
        if metrics["completion_percent"] >= 100:
            return True

        # Check if all acceptance criteria are satisfied
        # This would require more sophisticated tracking
        return False

    async def _should_reset(self, metrics: dict) -> bool:
        """Determine if we should trigger a reset."""
        # High failure rate
        if metrics["failure_rate"] > self.config.judge.failure_rate_threshold:
            return True

        # No progress for too long
        if self.state.no_progress_since:
            no_progress_time = datetime.fromisoformat(self.state.no_progress_since)
            timeout = timedelta(minutes=self.config.judge.no_progress_timeout_minutes)
            if datetime.now(UTC) - no_progress_time > timeout:
                return True

        return False

    async def _should_escalate(self, metrics: dict) -> bool:
        """Determine if we should escalate to human."""
        # Multiple consecutive failures
        if self.state.consecutive_failures >= 3:
            return True

        # Very high failure rate
        if metrics["failure_rate"] > 0.5:
            return True

        return False

    async def _update_spec_from_prs(self, spec: Spec) -> int:
        """Update spec requirements based on merged PRs."""
        updates = 0

        try:
            merged_prs = await self.github.list_pull_requests(state="closed")

            for pr in merged_prs:
                if not pr.get("merged_at"):
                    continue

                title = pr.get("title", "")
                # Extract task ID from title like "[abc123]"
                import re
                match = re.search(r'\[(\w+)\]', title)
                if not match:
                    continue

                task_id = match.group(1)

                # Find requirement with this task
                for req in spec.requirements:
                    if task_id in req.assigned_tasks:
                        # Mark as completed if all tasks done
                        all_tasks_merged = True  # Simplified - would check all tasks
                        if all_tasks_merged and req.status != RequirementStatus.COMPLETED:
                            req.status = RequirementStatus.COMPLETED
                            req.completion_evidence.append(pr.get("html_url", ""))
                            updates += 1

        except Exception as e:
            print(f"Warning: Could not update spec from PRs: {e}")

        return updates

    def _process_reputation_updates(self, limit: int = 100) -> int:
        """Process recent task state events to update agent reputations."""
        events = self.metrics_collector.read_task_state_events(limit=limit)
        updates = 0

        # We only care about terminal states for reputation
        terminal_states = {
            TaskState.SETTLED_ACK.value,
            TaskState.SETTLED_NACK.value,
            TaskState.DEAD_LETTER.value
        }

        for event in events:
            state_to = str(event.get("state_to", "")).strip().lower()
            if state_to not in terminal_states:
                continue

            agent_id = event.get("actor_id")
            event_id = event.get("event_id")

            if not agent_id or not event_id:
                continue

            # Success is SETTLED_ACK
            success = (state_to == TaskState.SETTLED_ACK.value)

            # For simplicity, we'll assume CI pass/fail based on the state for now
            # In the future, we can parse CI metrics from the event payload
            ci_passes = 1 if success else 0
            total_checks = 1

            self.reputation.record_outcome(
                agent_id=agent_id,
                event_id=event_id,
                success=success,
                ci_passes=ci_passes,
                total_checks=total_checks
            )
            updates += 1

        return updates

    def _latest_task_states(self, limit: int = 20000) -> dict[str, dict[str, str]]:
        """Index latest known task state from event log."""
        events = self.metrics_collector.read_task_state_events(limit=limit)
        latest: dict[str, dict[str, str]] = {}
        for event in reversed(events):
            task_id = str(event.get("task_id", "")).strip()
            if not task_id or task_id in latest:
                continue
            latest[task_id] = {
                "state_to": str(event.get("state_to", "")).strip().lower(),
                "reason": str(event.get("reason", "")).strip(),
            }
        return latest

    def _collect_routing_decisions(self, spec: Spec, limit: int = 20000) -> list[RoutingDecision]:
        events = self.metrics_collector.read_task_state_events(limit=limit)
        candidate_task_ids = {
            task_id
            for req in spec.requirements
            if req.status == RequirementStatus.IN_PROGRESS
            for task_id in req.assigned_tasks
            if task_id
        }
        decisions: list[RoutingDecision] = []
        for task_id in sorted(task_id for task_id in candidate_task_ids if task_id):
            decision = route_task_failures(task_id, events)
            if decision is not None:
                decisions.append(decision)
        return decisions

    def _emit_routing_decisions(self, decisions: list[RoutingDecision]) -> list[RoutingDecision]:
        existing = self.metrics_collector.read_routing_decisions(limit=5000)
        dedupe = {
            (
                str(event.get("task_id", "")).strip(),
                str(event.get("target", "")).strip(),
                tuple(str(value) for value in event.get("attempt_ids", [])),
            )
            for event in existing
        }

        emitted: list[RoutingDecision] = []
        for decision in decisions:
            key = (decision.task_id, decision.target.value, tuple(decision.attempt_ids))
            if key in dedupe:
                continue
            append_routing_decision(
                task_id=decision.task_id,
                trace_id=decision.trace_id,
                target=decision.target.value,
                reason=decision.reason,
                latest_reason_code=decision.latest_reason_code,
                consecutive_failures=decision.consecutive_failures,
                attempt_ids=decision.attempt_ids,
                failure_signature=decision.failure_signature,
                actor_id="judge",
            )
            append_decision_event(
                task_id=decision.task_id,
                reasoning=decision.reason,
                choice=f"route:{decision.target.value}",
                impact_level="high",
                context=decision.to_dict(),
            )
            emitted.append(decision)
        return emitted

    def _collect_shadow_routing_decisions(self, spec: Spec, limit: int = 20000) -> list[ShadowRoutingDecision]:
        if not self.teamplan.rig_plans:
            return []

        latest_states = self._latest_task_states(limit=limit)
        capability_snapshot = self.capability_registry.get_routing_snapshot()
        candidate_task_ids = {
            task_id
            for req in spec.requirements
            if req.status == RequirementStatus.IN_PROGRESS
            for task_id in req.assigned_tasks
            if task_id
        }
        decisions: list[ShadowRoutingDecision] = []
        for task_id in sorted(candidate_task_ids):
            latest_state = latest_states.get(task_id, {})
            state_to = str(latest_state.get("state_to", "")).strip().lower()
            if state_to in {
                TaskState.SETTLED_ACK.value,
                TaskState.SETTLED_NACK.value,
                TaskState.DEAD_LETTER.value,
            }:
                continue
            task = load_task_snapshot(task_id, repo_path=self.repo_path)
            if task is None:
                continue
            decision = route_task_shadow(task, self.teamplan.rig_plans, capability_snapshot)
            if decision is not None:
                decisions.append(decision)
        return decisions

    def _emit_shadow_routing_decisions(
        self,
        decisions: list[ShadowRoutingDecision],
    ) -> list[ShadowRoutingDecision]:
        existing = self.metrics_collector.read_routing_decisions(limit=5000)
        dedupe = {
            (
                str(event.get("task_id", "")).strip(),
                str(event.get("actor_id", "")).strip(),
                str(event.get("actual_lane_key", "")).strip(),
                str(event.get("recommended_lane_key", "")).strip(),
                str(event.get("hold_reason", "")).strip(),
            )
            for event in existing
            if str(event.get("actor_id", "")).strip() == "shadow_scheduler"
        }

        emitted: list[ShadowRoutingDecision] = []
        for decision in decisions:
            key = (
                decision.task_id,
                "shadow_scheduler",
                decision.actual_lane_key,
                decision.recommended_lane_key or "",
                decision.hold_reason.value if decision.hold_reason else "",
            )
            if key in dedupe:
                continue
            append_routing_decision(
                task_id=decision.task_id,
                trace_id=decision.trace_id,
                target=decision.recommended_stream or "hold",
                reason=decision.reason,
                latest_reason_code=decision.hold_reason.value if decision.hold_reason else "SHADOW_OK",
                consecutive_failures=0,
                attempt_ids=[],
                actor_id="shadow_scheduler",
                mode="shadow_lane",
                actual_lane_key=decision.actual_lane_key,
                actual_stream=decision.actual_stream,
                actual_model_tier=decision.actual_model_tier,
                recommended_lane_key=decision.recommended_lane_key,
                recommended_stream=decision.recommended_stream,
                recommended_model_tier=decision.recommended_model_tier,
                matched=decision.matched,
                candidate_count=decision.candidate_count,
                utility_score=decision.utility_score,
                hold_reason=decision.hold_reason.value if decision.hold_reason else None,
            )
            emitted.append(decision)
        return emitted

    def _collect_shadow_governor_recommendations(self) -> list[GovernorRecommendation]:
        if not self.teamplan.rig_plans:
            return []
        rig_telemetry = compute_rig_telemetry(
            self.repo_path,
            teamplan=self.teamplan,
            metrics_collector=self.metrics_collector,
        )
        budget_snapshot = self.metrics_collector.get_budget_snapshot(self.config.budget)
        return evaluate_shadow_governor(rig_telemetry, budget_snapshot)

    def _emit_shadow_governor_recommendations(
        self,
        recommendations: list[GovernorRecommendation],
    ) -> list[GovernorRecommendation]:
        existing = self.metrics_collector.read_governor_events(limit=5000)
        dedupe = {
            (
                str(event.get("actor_id", "")).strip(),
                str(event.get("recommendation_type", "")).strip(),
                str(event.get("scope_type", "")).strip(),
                str(event.get("scope_id", "")).strip(),
                str(event.get("status", "")).strip(),
            )
            for event in existing
            if str(event.get("actor_id", "")).strip() == "shadow_governor"
        }

        emitted: list[GovernorRecommendation] = []
        for recommendation in recommendations:
            key = (
                "shadow_governor",
                recommendation.recommendation_type,
                recommendation.scope_type,
                recommendation.scope_id,
                recommendation.status,
            )
            if key in dedupe:
                continue
            append_governor_event(
                actor_id="shadow_governor",
                recommendation_type=recommendation.recommendation_type,
                scope_type=recommendation.scope_type,
                scope_id=recommendation.scope_id,
                severity=recommendation.severity,
                status=recommendation.status,
                reason=recommendation.reason,
                attributes=recommendation.attributes,
            )
            append_decision_event(
                task_id=recommendation.scope_id,
                reasoning=recommendation.reason,
                choice=f"governor:{recommendation.recommendation_type.lower()}",
                impact_level="high" if recommendation.severity in {"critical", "high"} else "medium",
                context={
                    "scope_type": recommendation.scope_type,
                    "status": recommendation.status,
                    **recommendation.attributes,
                },
            )
            emitted.append(recommendation)
        return emitted

    def _load_task_snapshot(self, task_id: str):
        return load_task_snapshot(task_id, repo_path=self.repo_path)

    def _snapshot_task(self, task) -> None:
        snapshot_task(task, repo_path=self.repo_path, actor_id="judge")

    async def _apply_frontier_routes(self, routes: list[RoutingDecision]) -> list[str]:
        actions: list[str] = []
        for route in routes:
            task = self._load_task_snapshot(route.task_id)
            if task is None:
                actions.append(f"Frontier route skipped for {route.task_id}: task snapshot missing")
                continue

            if route.target == RoutingTarget.FRONTIER and not getattr(task, "frontier_eligible", False):
                actions.append(f"Frontier route skipped for {route.task_id}: task is not frontier eligible")
                continue

            if task.model_tier == ModelTier.FRONTIER and task.routing_target == RoutingTarget.FRONTIER:
                actions.append(f"Frontier route already active for {route.task_id}")
                continue

            task.model_tier = ModelTier.FRONTIER
            task.routing_target = RoutingTarget.FRONTIER
            task.routing_reason = route.reason
            task.escalation_reason = route.latest_reason_code
            task.estimated_cost_bucket = "high"
            task.shard_key = f"{task.stream.value}.{task.type.value}.{task.model_tier.value}"
            task.state = TaskState.QUEUED
            task.assigned_worker = None
            task.submission_ledger_id = None
            task.settlement_reason = None
            task.last_error = route.reason
            self._snapshot_task(task)
            await self.queue.publish(task, priority=task.priority)
            actions.append(
                f"Frontier route published for {route.task_id}: shard={task.shard_key} model_tier={task.model_tier.value}"
            )
        return actions

    def _reset_dead_letter_requirements(self, spec: Spec) -> int:
        """Reset requirements whose assigned tasks are all dead-lettered."""
        latest = self._latest_task_states()
        updates = 0
        for req in spec.requirements:
            if req.status != RequirementStatus.IN_PROGRESS or not req.assigned_tasks:
                continue
            terminal_states = [
                latest.get(task_id, {}).get("state_to", "")
                for task_id in req.assigned_tasks
            ]
            if terminal_states and all(state == TaskState.DEAD_LETTER.value for state in terminal_states):
                req.status = RequirementStatus.PENDING
                req.assigned_tasks = []
                req.completion_evidence.append("Reset after dead-lettered tasks")
                updates += 1
        return updates

    async def evaluate(self) -> JudgeReport:
        """
        Run one evaluation cycle.

        Returns:
            JudgeReport with decision and metrics
        """
        self.state.total_evaluations += 1
        timestamp = datetime.now(UTC).isoformat()
        trace_id = f"judge-{timestamp}"
        actions_taken = []

        append_trace_span(
            trace_id=trace_id,
            service="judge",
            operation="evaluate_cycle_start",
            actor_id="judge",
            attributes={"evaluation_index": self.state.total_evaluations},
        )

        # Load spec
        spec = self._load_spec()
        if spec is None:
            append_trace_span(
                trace_id=trace_id,
                service="judge",
                operation="evaluate_cycle_end",
                actor_id="judge",
                status="error",
                attributes={"reason_code": GateReason.MISSING_SPEC.value},
            )
            return JudgeReport(
                timestamp=timestamp,
                decision=JudgeDecision.ESCALATE,
                reason_code=GateReason.MISSING_SPEC,
                reason="No SPEC.json found - planner may not have run",
                metrics={},
                actions_taken=["Escalate: Waiting for SPEC"],
            )

        # Process reputation updates from task events
        rep_updates = self._process_reputation_updates()
        if rep_updates > 0:
            actions_taken.append(f"Processed {rep_updates} reputation updates for agents")

        # Update spec from merged PRs
        updates = await self._update_spec_from_prs(spec)
        dead_letter_resets = self._reset_dead_letter_requirements(spec)
        updates += dead_letter_resets
        if updates > 0:
            self._save_spec(spec)
            actions_taken.append(f"Updated {updates} requirements from merged PRs")
            if dead_letter_resets > 0:
                actions_taken.append(
                    f"Reset {dead_letter_resets} in-progress requirement(s) after dead-letter outcomes"
                )

        # Compute metrics
        metrics = await self._compute_metrics(spec)
        self._update_governor_cache(metrics)
        routing_decisions = self._emit_routing_decisions(self._collect_routing_decisions(spec))
        shadow_routing_decisions = self._emit_shadow_routing_decisions(
            self._collect_shadow_routing_decisions(spec)
        )
        shadow_divergence_count = sum(1 for decision in shadow_routing_decisions if not decision.matched)
        if shadow_routing_decisions:
            actions_taken.append(
                f"Shadow scheduler evaluated {len(shadow_routing_decisions)} task(s); "
                f"{shadow_divergence_count} differed from live assignment"
            )
        shadow_governor_recommendations = self._emit_shadow_governor_recommendations(
            self._collect_shadow_governor_recommendations()
        )
        if shadow_governor_recommendations:
            actions_taken.append(
                f"Shadow governor emitted {len(shadow_governor_recommendations)} recommendation(s)"
            )

        # Track progress
        current_progress = metrics["completion_percent"]
        if current_progress > self.state.last_progress:
            self.state.no_progress_since = None
            self.state.consecutive_failures = 0
        elif self.state.no_progress_since is None:
            self.state.no_progress_since = timestamp

        self.state.last_progress = current_progress
        self.state.last_run = timestamp

        # Make decision
        decision: JudgeDecision
        reason_code: GateReason
        reason: str

        # Check DoD
        if await self._check_dod_met(spec, metrics):
            decision = JudgeDecision.STOP
            reason_code = GateReason.DOD_MET
            reason = "Definition of Done met - all requirements completed"
            actions_taken.append("Setting swarm to stop")

        elif any(route.target == RoutingTarget.HUMAN_REVIEW for route in routing_decisions):
            decision = JudgeDecision.ESCALATE
            reason_code = GateReason.ESCALATE_HUMAN_ROUTE
            human_routes = [route for route in routing_decisions if route.target == RoutingTarget.HUMAN_REVIEW]
            reason = (
                f"Deterministic routing flagged {len(human_routes)} task(s) for human review "
                "after repeated operational failures"
            )
            for route in human_routes:
                actions_taken.append(f"Human review required for {route.task_id}: {route.latest_reason_code}")

        elif any(route.target == RoutingTarget.REPLAN for route in routing_decisions):
            decision = JudgeDecision.RESET
            reason_code = GateReason.RESET_REPLAN_ROUTE
            replan_routes = [route for route in routing_decisions if route.target == RoutingTarget.REPLAN]
            reason = (
                f"Deterministic routing flagged {len(replan_routes)} task(s) for replan "
                "after repeated decomposition failures"
            )
            for route in replan_routes:
                actions_taken.append(f"Replan required for {route.task_id}: {route.latest_reason_code}")

        # Check if should escalate
        elif await self._should_escalate(metrics):
            decision = JudgeDecision.ESCALATE
            reason_code = GateReason.ESCALATE_FAILURE_RATE
            reason = f"High failure rate ({metrics['failure_rate']:.1%}) or repeated failures"
            self.state.consecutive_failures += 1

            # Trigger escalation
            self.escalation_engine._create_escalation(
                EscalationType.RETRY_RATE,
                EscalationSeverity.HIGH,
                "Judge Escalation: High Failure Rate",
                reason,
                "Review recent task failures and consider manual intervention or replanning."
            )
            actions_taken.append("Escalating to human via EscalationEngine")

        # Check if should reset
        elif await self._should_reset(metrics):
            decision = JudgeDecision.RESET
            reason_code = GateReason.RESET_STALLED_OR_FAILURE
            reason = "Triggering reset due to stalled progress or high failure rate"
            actions_taken.append("Initiating reset protocol")

        # Continue
        else:
            decision = JudgeDecision.CONTINUE
            reason_code = GateReason.CONTINUE_PROGRESS
            reason = f"Progress at {current_progress:.1f}%, continuing execution"

            frontier_routes = [route for route in routing_decisions if route.target == RoutingTarget.FRONTIER]
            if frontier_routes:
                reason_code = GateReason.CONTINUE_FRONTIER_ROUTE
                reason = (
                    f"Deterministic routing flagged {len(frontier_routes)} task(s) for frontier escalation"
                )
                actions_taken.extend(await self._apply_frontier_routes(frontier_routes))

            # Check if we need more tasks
            if metrics.get("queue_depth", 0) == 0 and current_progress < 100:
                actions_taken.append("Queue empty - may need replanning")
                reason_code = GateReason.CONTINUE_QUEUE_EMPTY
                if dead_letter_resets > 0:
                    decision = JudgeDecision.RESET
                    reason_code = GateReason.RESET_STALLED_OR_FAILURE
                    reason = "Queue empty after dead-letter resets - triggering replan reset"
                    actions_taken.append("Triggering reset to regenerate tasks for reset requirements")

        # Create and save report
        report = JudgeReport(
            timestamp=timestamp,
            decision=decision,
            reason_code=reason_code,
            reason=reason,
            metrics=metrics,
            actions_taken=actions_taken,
        )

        self._append_report(report)
        append_trace_span(
            trace_id=trace_id,
            service="judge",
            operation="evaluate_cycle_end",
            actor_id="judge",
            status="ok",
            attributes={
                "decision": decision.value,
                "reason_code": reason_code.value,
                "completion_percent": current_progress,
            },
        )

        return report

    def _update_governor_cache(self, metrics: dict) -> None:
        """Cache async-evaluated metrics for sync governor telemetry snapshots."""
        queue_depth = metrics.get("queue_depth")
        if isinstance(queue_depth, int) and queue_depth >= 0:
            self.state.last_queue_depth = queue_depth

        prs_open = metrics.get("prs_open")
        if isinstance(prs_open, int) and prs_open >= 0:
            self.state.last_prs_open = prs_open

        prs_merged = metrics.get("prs_merged")
        if isinstance(prs_merged, int) and prs_merged >= 0:
            self.state.last_prs_merged = prs_merged

        ci_passes = metrics.get("prs_passing_ci")
        ci_fails = metrics.get("prs_failing_ci")
        if isinstance(ci_passes, int) and isinstance(ci_fails, int) and ci_passes >= 0 and ci_fails >= 0:
            total = ci_passes + ci_fails
            if total > 0:
                self.state.last_ci_health_percent = (ci_passes / total) * 100.0

    async def run_once(self) -> JudgeReport:
        """Run one evaluation cycle."""
        return await self.evaluate()

    async def run_loop(self) -> None:
        """
        Run the judge loop - periodic evaluations.
        """
        self._running = True
        interval = self.config.judge.interval_minutes * 60

        print(f"[Judge] Starting evaluation loop (interval: {interval}s)")

        while self._running:
            try:
                report = await self.evaluate()
                print(
                    f"[Judge] Decision: {report.decision.value} ({report.reason_code.value}) - {report.reason}"
                )

                # Handle decision
                if report.decision == JudgeDecision.STOP:
                    # Set paused flag
                    config = MXConfig.load()
                    config.paused = True
                    config.save()
                    print("[Judge] Swarm stopped - DoD met")
                    break

                elif report.decision == JudgeDecision.RESET:
                    if self.config.judge.auto_reset_enabled:
                        await self._execute_reset()
                    else:
                        print("[Judge] Reset needed but auto-reset disabled")

            except Exception as e:
                print(f"[Judge] Evaluation error: {e}")

            # Wait for next interval
            await asyncio.sleep(interval)

        print("[Judge] Loop stopped")

    def _get_telemetry_snapshot(self) -> TelemetrySnapshot:
        """Provide telemetry snapshot for the Governor."""
        coordination = self.metrics_collector.compute_metrics(window_hours=1)
        event_observability = self.metrics_collector.compute_event_observability(window_minutes=60)
        event_metrics = event_observability.get("metrics", {})

        reps = self.reputation._reputations.values()
        blacklisted = [r for r in reps if r.blacklist_status]
        low_trust = [r.id for r in reps if r.trust_score < 40 and not r.blacklist_status]

        return TelemetrySnapshot(
            ci_health_percent=self.state.last_ci_health_percent,
            tasks_completed_1h=int(event_metrics.get("tasks_completed_total", coordination.spec_items_completed)),
            tasks_failed_1h=int(
                event_metrics.get("tasks_nacked_total", coordination.retry_cascades)
                + event_metrics.get("tasks_dead_letter_total", 0)
            ),
            tasks_nacked_1h=int(event_metrics.get("tasks_nacked_total", coordination.retry_cascades)),
            queue_depth=self.state.last_queue_depth,
            prs_open_total=self.state.last_prs_open,
            prs_merged_1h=self.state.last_prs_merged,
            blacklisted_count=len(blacklisted),
            low_trust_agents=low_trust,
        )

    def _on_governor_adjustment(self, adjustment: Adjustment):
        """Handle governor adjustments."""
        print(f"[Judge] Governor Adjustment: {adjustment.type.value} - {adjustment.reason}")
        # Here we could trigger specific judge actions if needed

    async def run_governor_check(self) -> GovernorReport:
        """Run a governor health check using the boss.Governor."""
        # Update telemetry and evaluate
        await self.governor._evaluate()

        # Get state summary for report
        gov_state = self.governor.get_state_summary()

        report = GovernorReport(
            timestamp=datetime.now(UTC).isoformat(),
            state=GovernorMode(gov_state["mode"]),
            actions=[adj["reason"] for adj in gov_state["recent_adjustments"]],
            metrics=gov_state["current_caps"]
        )

        # Append to report file
        report_path = os.path.join(self.repo_path, "GOVERNOR_REPORT.md")
        with open(report_path, "a") as f:
            f.write(report.to_markdown())

        return report

    async def _execute_reset(self) -> None:
        """Execute reset protocol."""
        print("[Judge] Executing reset protocol...")

        # Purge queue
        try:
            await self.queue.purge()
            print("[Judge] Queue purged")
        except Exception as e:
            print(f"[Judge] Queue purge failed: {e}")

        # Reset spec requirements
        spec = self._load_spec()
        if spec:
            for req in spec.requirements:
                if req.status != RequirementStatus.COMPLETED:
                    req.status = RequirementStatus.PENDING
                    req.assigned_tasks = []
            self._save_spec(spec)
            print("[Judge] Spec reset")

        # Trigger replan (would need to call planner)
        print("[Judge] Reset complete - Executing Planner...")
        try:
            from planner import Planner
            planner = Planner(self.config, repo_path=self.repo_path)
            result = await planner.run()
            print(f"[Judge] Planner generated {result['tasks_generated']} tasks")
        except Exception as e:
            print(f"[Judge] Planned execution failed: {e}")

        # Reset judge state so we don't immediately reset again
        self.state.no_progress_since = None
        self.state.consecutive_failures = 0

    async def run(self) -> None:
        """
        Main entry point - initialize, run loop, cleanup.
        """
        await self.initialize()

        try:
            await self.run_loop()
        finally:
            await self.shutdown()


async def main():
    """CLI entry point for judge."""
    import argparse

    parser = argparse.ArgumentParser(description="MX Swarm Judge")
    parser.add_argument("--config", default="mx.config.json", help="Config file path")
    parser.add_argument("--repo", default=".", help="Repository path")
    parser.add_argument("--once", action="store_true", help="Run one evaluation and exit")
    args = parser.parse_args()

    config = MXConfig.load(args.config)
    judge = Judge(config, repo_path=args.repo)

    if args.once:
        await judge.initialize()
        try:
            report = await judge.run_once()
            print(f"\nDecision: {report.decision.value}")
            print(f"Reason: {report.reason}")
            print(f"Metrics: {json.dumps(report.metrics, indent=2)}")
        finally:
            await judge.shutdown()
    else:
        await judge.run()


if __name__ == "__main__":
    asyncio.run(main())
