"""
Coordination Metrics - Efficiency and Quality Measurement

This module provides lightweight proxy metrics for measuring
coordination health without requiring complex real-time analysis.

Metrics tracked:
1. Overhead - spend per shipped SPEC item vs baseline
2. Message density - agent deliberation volume per task
3. Redundancy - overlapping edits / duplicate PR intents
4. Coordination efficiency - shipped items per $ and per CI-minute
5. Error amplification - retry cascades + fix/rollback ratio
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class TaskMetrics:
    """Metrics for a single task execution."""
    task_id: str
    stream: str
    tier: int
    spec_ref: str | None

    # Timing
    started_at: datetime
    completed_at: datetime | None = None
    duration_seconds: float = 0.0

    # Cost
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    estimated_cost_usd: float = 0.0

    # Outcome
    success: bool = False
    retries: int = 0
    files_touched: int = 0
    lines_changed: int = 0

    # PR metrics
    pr_created: bool = False
    pr_merged: bool = False
    ci_passes: int = 0
    ci_fails: int = 0
    merge_conflicts: int = 0


@dataclass
class CoordinationMetrics:
    """
    Aggregated coordination health metrics.

    These are proxy metrics - not perfect but useful for
    detecting coordination issues and tracking improvement.
    """

    # Time window for rolling metrics
    window_hours: int = 1

    # Core efficiency
    spec_items_completed: int = 0
    total_spend_usd: float = 0.0
    total_ci_minutes: float = 0.0

    # Overhead proxy: spend per shipped item
    overhead_usd_per_item: float = 0.0
    overhead_baseline_usd: float = 5.0  # Expected cost per item
    overhead_ratio: float = 1.0  # actual/baseline

    # Message density proxy: LLM calls per task
    avg_llm_calls_per_task: float = 0.0
    total_tasks: int = 0

    # Redundancy proxy: conflict and duplicate indicators
    files_touched_multiple_times: int = 0
    duplicate_pr_intents: int = 0
    merge_conflicts_total: int = 0
    redundancy_score: float = 0.0  # 0-1, lower is better

    # Coordination efficiency: shipped per resource
    items_per_dollar: float = 0.0
    items_per_ci_minute: float = 0.0
    efficiency_score: float = 0.0  # 0-1, higher is better

    # Error amplification: cascading failures
    retry_cascades: int = 0  # Tasks that maxed retries
    fix_prs: int = 0  # PRs that fix previous PRs
    rollback_prs: int = 0  # PRs that revert previous PRs
    error_amplification_score: float = 0.0  # 0-1, lower is better

    # Trend indicators
    trend_overhead: str = "stable"  # improving, stable, degrading
    trend_efficiency: str = "stable"
    trend_quality: str = "stable"

    def to_dict(self) -> dict:
        """Convert to dictionary for dashboard."""
        return {
            "window_hours": self.window_hours,
            "spec_items_completed": self.spec_items_completed,
            "total_spend_usd": round(self.total_spend_usd, 2),
            "overhead": {
                "usd_per_item": round(self.overhead_usd_per_item, 2),
                "baseline_usd": self.overhead_baseline_usd,
                "ratio": round(self.overhead_ratio, 2),
                "status": "good" if self.overhead_ratio < 1.2 else "warning" if self.overhead_ratio < 1.5 else "critical"
            },
            "message_density": {
                "avg_llm_calls": round(self.avg_llm_calls_per_task, 1),
                "total_tasks": self.total_tasks
            },
            "redundancy": {
                "files_multi_touch": self.files_touched_multiple_times,
                "duplicate_intents": self.duplicate_pr_intents,
                "conflicts": self.merge_conflicts_total,
                "score": round(self.redundancy_score, 2),
                "status": "good" if self.redundancy_score < 0.2 else "warning" if self.redundancy_score < 0.4 else "critical"
            },
            "efficiency": {
                "items_per_dollar": round(self.items_per_dollar, 3),
                "items_per_ci_minute": round(self.items_per_ci_minute, 3),
                "score": round(self.efficiency_score, 2),
                "status": "good" if self.efficiency_score > 0.7 else "warning" if self.efficiency_score > 0.4 else "critical"
            },
            "error_amplification": {
                "retry_cascades": self.retry_cascades,
                "fix_prs": self.fix_prs,
                "rollback_prs": self.rollback_prs,
                "score": round(self.error_amplification_score, 2),
                "status": "good" if self.error_amplification_score < 0.15 else "warning" if self.error_amplification_score < 0.3 else "critical"
            },
            "trends": {
                "overhead": self.trend_overhead,
                "efficiency": self.trend_efficiency,
                "quality": self.trend_quality
            },
            "overall_health": self._compute_overall_health()
        }

    def _compute_overall_health(self) -> str:
        """Compute overall coordination health."""
        # Simple weighted average
        scores = [
            (1 - min(self.overhead_ratio - 1, 1) / 1, 0.25),  # Overhead (inverted)
            (1 - self.redundancy_score, 0.25),  # Redundancy (inverted)
            (self.efficiency_score, 0.25),  # Efficiency
            (1 - self.error_amplification_score, 0.25)  # Error amp (inverted)
        ]
        weighted = sum(score * weight for score, weight in scores)

        if weighted > 0.75:
            return "healthy"
        elif weighted > 0.5:
            return "fair"
        else:
            return "degraded"


class MetricsCollector:
    """
    Collects and aggregates coordination metrics.

    This is a lightweight in-memory collector. For production,
    this would be backed by a time-series database.
    """

    def __init__(self, baseline_cost_per_item: float = 5.0):
        self.baseline_cost = baseline_cost_per_item
        self.task_metrics: list[TaskMetrics] = []
        self.file_touch_counts: dict[str, int] = defaultdict(int)
        self.pr_intents: dict[str, int] = defaultdict(int)  # goal hash -> count
        self.historical_metrics: list[CoordinationMetrics] = []

    def record_task(self, metrics: TaskMetrics):
        """Record metrics for a completed task."""
        self.task_metrics.append(metrics)

        # Track file touches for redundancy detection
        # (In production, would get actual files from task)

        # Keep only last 1000 tasks
        if len(self.task_metrics) > 1000:
            self.task_metrics = self.task_metrics[-1000:]

    def record_file_touch(self, file_path: str, task_id: str):
        """Record that a file was touched by a task."""
        self.file_touch_counts[file_path] += 1

    def record_pr_intent(self, goal_hash: str):
        """Record a PR intent (for duplicate detection)."""
        self.pr_intents[goal_hash] += 1

    def compute_metrics(self, window_hours: int = 1) -> CoordinationMetrics:
        """Compute coordination metrics for the given time window."""
        cutoff = datetime.utcnow() - timedelta(hours=window_hours)
        recent_tasks = [t for t in self.task_metrics if t.started_at > cutoff]

        metrics = CoordinationMetrics(window_hours=window_hours)

        if not recent_tasks:
            return metrics

        # Basic counts
        metrics.total_tasks = len(recent_tasks)
        metrics.spec_items_completed = sum(
            1 for t in recent_tasks if t.success and t.spec_ref
        )

        # Cost metrics
        metrics.total_spend_usd = sum(t.estimated_cost_usd for t in recent_tasks)

        # Overhead calculation
        if metrics.spec_items_completed > 0:
            metrics.overhead_usd_per_item = metrics.total_spend_usd / metrics.spec_items_completed
            metrics.overhead_ratio = metrics.overhead_usd_per_item / self.baseline_cost
        else:
            metrics.overhead_ratio = 1.0

        # Message density
        total_llm_calls = sum(t.llm_calls for t in recent_tasks)
        metrics.avg_llm_calls_per_task = total_llm_calls / metrics.total_tasks

        # Redundancy metrics
        metrics.files_touched_multiple_times = sum(
            1 for count in self.file_touch_counts.values() if count > 1
        )
        metrics.duplicate_pr_intents = sum(
            count - 1 for count in self.pr_intents.values() if count > 1
        )
        metrics.merge_conflicts_total = sum(t.merge_conflicts for t in recent_tasks)

        # Compute redundancy score (0-1, lower is better)
        total_files = len(self.file_touch_counts)
        if total_files > 0:
            multi_touch_ratio = metrics.files_touched_multiple_times / total_files
            conflict_ratio = metrics.merge_conflicts_total / max(metrics.total_tasks, 1)
            metrics.redundancy_score = min(1.0, (multi_touch_ratio + conflict_ratio) / 2)

        # Efficiency metrics
        if metrics.total_spend_usd > 0:
            metrics.items_per_dollar = metrics.spec_items_completed / metrics.total_spend_usd

        # CI time (estimate 5 min per task with CI)
        ci_tasks = sum(1 for t in recent_tasks if t.ci_passes > 0 or t.ci_fails > 0)
        metrics.total_ci_minutes = ci_tasks * 5
        if metrics.total_ci_minutes > 0:
            metrics.items_per_ci_minute = metrics.spec_items_completed / metrics.total_ci_minutes

        # Compute efficiency score
        # Normalize to 0-1 based on expected rates
        items_per_dollar_normalized = min(1.0, metrics.items_per_dollar / 0.2)  # Expect 0.2 items/$
        items_per_ci_normalized = min(1.0, metrics.items_per_ci_minute / 0.1)  # Expect 0.1 items/min
        metrics.efficiency_score = (items_per_dollar_normalized + items_per_ci_normalized) / 2

        # Error amplification
        metrics.retry_cascades = sum(
            1 for t in recent_tasks if t.retries >= 3
        )
        # Would need PR analysis for fix/rollback detection
        # For now, estimate based on retry patterns
        metrics.fix_prs = metrics.retry_cascades // 3
        metrics.rollback_prs = sum(1 for t in recent_tasks if t.merge_conflicts > 2)

        total_outcomes = metrics.total_tasks
        if total_outcomes > 0:
            error_events = metrics.retry_cascades + metrics.fix_prs + metrics.rollback_prs
            metrics.error_amplification_score = min(1.0, error_events / total_outcomes)

        # Compute trends (compare to previous period)
        if self.historical_metrics:
            prev = self.historical_metrics[-1]
            metrics.trend_overhead = self._compute_trend(
                prev.overhead_ratio, metrics.overhead_ratio, inverse=True
            )
            metrics.trend_efficiency = self._compute_trend(
                prev.efficiency_score, metrics.efficiency_score
            )
            metrics.trend_quality = self._compute_trend(
                prev.error_amplification_score, metrics.error_amplification_score, inverse=True
            )

        # Store for trend computation
        self.historical_metrics.append(metrics)
        if len(self.historical_metrics) > 24:  # Keep 24 hours
            self.historical_metrics = self.historical_metrics[-24:]

        return metrics

    def _compute_trend(self, old_value: float, new_value: float, inverse: bool = False) -> str:
        """Compute trend direction."""
        if old_value == 0:
            return "stable"

        change = (new_value - old_value) / old_value

        if inverse:
            change = -change

        if change > 0.1:
            return "improving"
        elif change < -0.1:
            return "degrading"
        return "stable"

    def get_dashboard_data(self) -> dict:
        """Get metrics formatted for dashboard display."""
        metrics = self.compute_metrics(window_hours=1)
        return metrics.to_dict()


# Global collector instance
_collector: MetricsCollector | None = None


def get_metrics_collector() -> MetricsCollector:
    """Get or create the global metrics collector."""
    global _collector
    if _collector is None:
        _collector = MetricsCollector()
    return _collector
