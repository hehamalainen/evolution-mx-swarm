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
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

from shared.schemas import (
    Spec,
    RequirementStatus,
)
from shared.adapters import QueueAdapter, create_queue_adapter
from shared.utils import (
    MXConfig,
    LLMClient,
    Message,
    create_llm_client,
    GitHubAPI,
    MockGitHubAPI,
)
from shared.utils.metrics import get_metrics_collector, TaskMetrics
from boss import (
    EscalationEngine, EscalationType, EscalationSeverity,
    Governor, GovernorState, TelemetrySnapshot, Adjustment, AdjustmentType, TriggerType
)
from shared.schemas.teamplan import TeamPlan


class JudgeDecision(str, Enum):
    CONTINUE = "continue"  # Keep going, optionally replan some tasks
    RESET = "reset"  # Full reset - restart workers, recompile plan
    STOP = "stop"  # DoD met, stop swarm
    ESCALATE = "escalate"  # Ask human for help


class GovernorState(str, Enum):
    NORMAL = "NORMAL"
    THROTTLED = "THROTTLED"
    FROZEN = "FROZEN"


@dataclass
class GovernorReport:
    """Report from a governor check."""
    timestamp: str
    state: GovernorState
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
    reason: str
    metrics: dict
    actions_taken: list[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        """Generate markdown for JUDGE_REPORT.md."""
        return f"""
## Judge Report - {self.timestamp}

**Decision:** {self.decision.value.upper()}

**Reason:** {self.reason}

### Metrics
| Metric | Value |
|--------|-------|
| Requirements Total | {self.metrics.get('requirements_total', 0)} |
| Requirements Completed | {self.metrics.get('requirements_completed', 0)} |
| Completion % | {self.metrics.get('completion_percent', 0)}% |
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
    last_run: Optional[str] = None
    last_progress: float = 0.0
    no_progress_since: Optional[str] = None
    consecutive_failures: int = 0
    total_evaluations: int = 0
    governor_state: GovernorState = GovernorState.NORMAL
    last_governor_check: Optional[str] = None


class Judge:
    """
    Judge service for MX Swarm.

    Evaluates progress, makes decisions, and orchestrates the swarm.
    """

    def __init__(
        self,
        config: MXConfig,
        queue: Optional[QueueAdapter] = None,
        llm: Optional[LLMClient] = None,
        repo_path: str = ".",
    ):
        self.config = config
        self.queue = queue or create_queue_adapter(config.queue.to_dict())
        self.llm = llm or create_llm_client(config.llm.to_dict())
        self.repo_path = repo_path
        self.state = JudgeState()
        self._github: Optional[GitHubAPI] = None
        self._running = False
        self.metrics_collector = get_metrics_collector()
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

    @property
    def github(self) -> GitHubAPI:
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

    def _load_spec(self) -> Optional[Spec]:
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
            if datetime.utcnow() - no_progress_time > timeout:
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

    async def evaluate(self) -> JudgeReport:
        """
        Run one evaluation cycle.

        Returns:
            JudgeReport with decision and metrics
        """
        self.state.total_evaluations += 1
        timestamp = datetime.utcnow().isoformat()
        actions_taken = []

        # Load spec
        spec = self._load_spec()
        if spec is None:
            return JudgeReport(
                timestamp=timestamp,
                decision=JudgeDecision.ESCALATE,
                reason="No SPEC.json found - planner may not have run",
                metrics={},
                actions_taken=["Escalate: Waiting for SPEC"],
            )

        # Update spec from merged PRs
        updates = await self._update_spec_from_prs(spec)
        if updates > 0:
            self._save_spec(spec)
            actions_taken.append(f"Updated {updates} requirements from merged PRs")

        # Compute metrics
        metrics = await self._compute_metrics(spec)

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
        reason: str

        # Check DoD
        if await self._check_dod_met(spec, metrics):
            decision = JudgeDecision.STOP
            reason = "Definition of Done met - all requirements completed"
            actions_taken.append("Setting swarm to stop")

        # Check if should escalate
        elif await self._should_escalate(metrics):
            decision = JudgeDecision.ESCALATE
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
            reason = "Triggering reset due to stalled progress or high failure rate"
            actions_taken.append("Initiating reset protocol")

        # Continue
        else:
            decision = JudgeDecision.CONTINUE
            reason = f"Progress at {current_progress:.1f}%, continuing execution"

            # Check if we need more tasks
            if metrics.get("queue_depth", 0) == 0 and current_progress < 100:
                actions_taken.append("Queue empty - may need replanning")

        # Create and save report
        report = JudgeReport(
            timestamp=timestamp,
            decision=decision,
            reason=reason,
            metrics=metrics,
            actions_taken=actions_taken,
        )

        self._append_report(report)

        return report

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
                print(f"[Judge] Decision: {report.decision.value} - {report.reason}")

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
        # This would ideally be async, but Governor expects a synchronous provider for now
        # We can use the metrics collector and cached PR metrics
        metrics = self.metrics_collector.compute_metrics(window_hours=1)
        
        # We'll use a simplified snapshot for now
        return TelemetrySnapshot(
            ci_health_percent=self.state.last_progress, # Placeholder
            tasks_completed_1h=metrics.total_tasks,
            tasks_nacked_1h=metrics.retry_cascades,
            queue_depth=0, # Need real queue depth
            prs_open_total=0 # Need real PR count
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
            timestamp=datetime.utcnow().isoformat(),
            state=GovernorState(gov_state["mode"]),
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
