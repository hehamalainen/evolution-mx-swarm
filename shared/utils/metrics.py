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

import json
import os
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

TASK_STATE_EVENTS_PATH = os.path.join(".runtime", "task_state_events.jsonl")
TRACE_SPANS_PATH = os.path.join(".runtime", "trace_spans.jsonl")
SUBMISSION_EVENTS_PATH = os.path.join(".runtime", "submission_events.jsonl")
AETHER_RECON_PATH = os.path.join(".runtime", "aether_reconciliation_backlog.json")
TERMINAL_TASK_STATES = {"settled_ack", "settled_nack", "dead_letter"}
DECISION_LOG_PATH = os.path.join(".runtime", "autonomous_decisions.jsonl")
ROUTING_DECISIONS_PATH = os.path.join(".runtime", "routing_decisions.jsonl")
LLM_USAGE_EVENTS_PATH = os.path.join(".runtime", "llm_usage_events.jsonl")
BUDGET_EVENTS_PATH = os.path.join(".runtime", "budget_events.jsonl")
GOVERNOR_EVENTS_PATH = os.path.join(".runtime", "governor_events.jsonl")

METRIC_DICTIONARY: dict[str, dict[str, str]] = {
    "false_success_rate": {
        "formula": "false_success_events / max(settled_ack_events, 1)",
        "unit": "ratio",
        "description": "Declared success events that skipped required verification/canonical states.",
    },
    "settlement_correctness": {
        "formula": "(terminal_settlement_events - duplicate_settlement_events) / max(terminal_settlement_events, 1)",
        "unit": "ratio",
        "description": "Uniqueness and integrity of terminal settlement transitions.",
    },
    "dlq_rate": {
        "formula": "dead_letter_events / max(terminal_settlement_events, 1)",
        "unit": "ratio",
        "description": "Dead-letter proportion over all terminal settlements.",
    },
    "p95_claim_to_start_ms": {
        "formula": "p95(executing_ts - claimed_ts) by (task_id, attempt_id)",
        "unit": "ms",
        "description": "Queue-claim to execution-start latency.",
    },
    "p99_settlement_latency_ms": {
        "formula": "p99(terminal_ts - claimed_ts) by (task_id, attempt_id)",
        "unit": "ms",
        "description": "End-to-end task settlement latency from claim to terminal state.",
    },
    "duplicate_settlement_events": {
        "formula": "sum(max(terminal_events_per_attempt - 1, 0))",
        "unit": "count",
        "description": "Extra terminal transitions per task attempt.",
    },
    "aether_reconciliation_backlog_p95_clearance_min": {
        "formula": "p95(updated_at - created_at) for settled reconciliation items",
        "unit": "minutes",
        "description": "Time to clear pending-ledger items once canonical status resolves.",
    },
    "frontier_spend_ratio": {
        "formula": "frontier_spend_today_usd / max(total_spend_today_usd, 1e-9)",
        "unit": "ratio",
        "description": "Share of total daily LLM spend consumed by the frontier lane.",
    },
    "cost_per_success_usd": {
        "formula": "total_spend_today_usd / max(settled_ack_events, 1)",
        "unit": "usd",
        "description": "Effective daily spend per successful settled artifact.",
    },
    "trace_continuity_ratio": {
        "formula": "traces_with_services(planner,worker,vcs_or_aether) / max(total_traces,1)",
        "unit": "ratio",
        "description": "Cross-service trace continuity coverage.",
    },
}

FORMULA_THRESHOLDS: dict[str, dict[str, float | str]] = {
    "false_success_rate": {"op": "==", "target": 0.0},
    "settlement_correctness": {"op": ">=", "target": 0.9999},
    "dlq_rate": {"op": "<", "target": 0.02},
    "p95_claim_to_start_ms": {"op": "<=", "target": 2000.0},
    "p99_settlement_latency_ms": {"op": "<=", "target": 30000.0},
    "duplicate_settlement_events": {"op": "==", "target": 0.0},
    "aether_reconciliation_backlog_p95_clearance_min": {"op": "<=", "target": 15.0},
}


def _as_budget_value(config_or_dict: Any, key: str, default: float | int | str) -> Any:
    if isinstance(config_or_dict, dict):
        return config_or_dict.get(key, default)
    return getattr(config_or_dict, key, default)


def _same_utc_day(timestamp: datetime, reference: datetime) -> bool:
    return timestamp.astimezone(UTC).date() == reference.astimezone(UTC).date()


def _parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(1.0, q)) * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _evaluate_threshold(value: float | int, op: str, target: float) -> bool:
    if op == "==":
        return float(value) == target
    if op == "<":
        return float(value) < target
    if op == "<=":
        return float(value) <= target
    if op == ">":
        return float(value) > target
    if op == ">=":
        return float(value) >= target
    return False


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
        self.task_state_event_path = TASK_STATE_EVENTS_PATH
        self.trace_span_path = TRACE_SPANS_PATH
        self.submission_event_path = SUBMISSION_EVENTS_PATH
        self.reconciliation_path = AETHER_RECON_PATH
        self.routing_decision_path = ROUTING_DECISIONS_PATH
        self.llm_usage_event_path = LLM_USAGE_EVENTS_PATH
        self.budget_event_path = BUDGET_EVENTS_PATH
        self.governor_event_path = GOVERNOR_EVENTS_PATH

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

    def _append_jsonl(self, path: str, payload: dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as handle:
            handle.write(json.dumps(payload) + "\n")

    def emit_trace_span(
        self,
        trace_id: str,
        service: str,
        operation: str,
        actor_id: str,
        status: str = "ok",
        task_id: str | None = None,
        attempt_id: str | None = None,
        parent_span_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> str:
        span_id = f"span-{uuid.uuid4().hex[:16]}"
        payload = {
            "span_id": span_id,
            "trace_id": trace_id,
            "parent_span_id": parent_span_id,
            "service": service,
            "operation": operation,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "status": status,
            "actor_id": actor_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "attributes": attributes or {},
        }
        self._append_jsonl(self.trace_span_path, payload)
        return span_id

    def emit_decision(
        self,
        task_id: str,
        reasoning: str,
        choice: str,
        impact_level: str = "medium",
        context: dict[str, Any] | None = None,
    ) -> None:
        """
        Record an autonomous decision for 'Decision Transparency'.

        This logs the 'why' behind agent actions to reduce human audit overhead.
        """
        payload = {
            "decision_id": f"dec-{uuid.uuid4().hex[:16]}",
            "task_id": task_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "reasoning": reasoning,
            "choice": choice,
            "impact_level": impact_level,
            "context": context or {},
        }
        self._append_jsonl(DECISION_LOG_PATH, payload)

    def read_task_state_events(self, limit: int = 20000) -> list[dict[str, Any]]:
        if not os.path.exists(self.task_state_event_path):
            return []
        with open(self.task_state_event_path) as handle:
            lines = handle.readlines()[-limit:]
        events: list[dict[str, Any]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                events.append(payload)
        return events

    def read_trace_spans(self, limit: int = 50000) -> list[dict[str, Any]]:
        if not os.path.exists(self.trace_span_path):
            return []
        with open(self.trace_span_path) as handle:
            lines = handle.readlines()[-limit:]
        spans: list[dict[str, Any]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                spans.append(payload)
        return spans

    def emit_submission_event(
        self,
        *,
        task_id: str,
        trace_id: str,
        attempt_id: str,
        branch_name: str,
        submission_id: str,
        submission_type: str,
        provider: str,
        url: str | None,
        status: str,
        goal: str,
        affected_paths: list[str],
        local_mode: bool,
        created_at: str,
        settled_at: str | None = None,
        reason: str | None = None,
        canonical_status: str | None = None,
        landing_verification_status: str | None = None,
        landing_verification_detail: str | None = None,
        source: str | None = None,
        source_run_id: str | None = None,
        source_task_id: str | None = None,
        source_agent_id: str | None = None,
        source_attempt_id: str | None = None,
        intent_type: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "event_id": f"sub-{uuid.uuid4().hex[:16]}",
            "task_id": task_id,
            "trace_id": trace_id,
            "attempt_id": attempt_id,
            "branch_name": branch_name,
            "submission_id": submission_id,
            "submission_type": submission_type,
            "provider": provider,
            "url": url,
            "status": status,
            "goal": goal,
            "affected_paths": affected_paths,
            "local_mode": local_mode,
            "created_at": created_at,
            "settled_at": settled_at,
            "reason": reason,
            "canonical_status": canonical_status,
            "landing_verification_status": landing_verification_status,
            "landing_verification_detail": landing_verification_detail,
            "source": source,
            "source_run_id": source_run_id,
            "source_task_id": source_task_id,
            "source_agent_id": source_agent_id,
            "source_attempt_id": source_attempt_id,
            "intent_type": intent_type,
        }
        self._append_jsonl(self.submission_event_path, payload)
        return payload

    def read_submission_events(self, limit: int = 5000) -> list[dict[str, Any]]:
        if not os.path.exists(self.submission_event_path):
            return []
        with open(self.submission_event_path) as handle:
            lines = handle.readlines()[-limit:]
        events: list[dict[str, Any]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                events.append(payload)
        return events

    def emit_routing_decision(
        self,
        *,
        task_id: str,
        trace_id: str | None,
        target: str,
        reason: str,
        latest_reason_code: str,
        consecutive_failures: int,
        attempt_ids: list[str],
        failure_signature: str | None = None,
        actor_id: str = "judge",
        mode: str = "deterministic_failure",
        actual_lane_key: str | None = None,
        actual_stream: str | None = None,
        actual_model_tier: str | None = None,
        recommended_lane_key: str | None = None,
        recommended_stream: str | None = None,
        recommended_model_tier: str | None = None,
        matched: bool | None = None,
        candidate_count: int | None = None,
        utility_score: float | None = None,
        hold_reason: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "event_id": f"route-{uuid.uuid4().hex[:16]}",
            "task_id": task_id,
            "trace_id": trace_id,
            "target": target,
            "reason": reason,
            "latest_reason_code": latest_reason_code,
            "consecutive_failures": consecutive_failures,
            "attempt_ids": attempt_ids,
            "failure_signature": failure_signature,
            "actor_id": actor_id,
            "mode": mode,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        if actual_lane_key is not None:
            payload["actual_lane_key"] = actual_lane_key
        if actual_stream is not None:
            payload["actual_stream"] = actual_stream
        if actual_model_tier is not None:
            payload["actual_model_tier"] = actual_model_tier
        if recommended_lane_key is not None:
            payload["recommended_lane_key"] = recommended_lane_key
        if recommended_stream is not None:
            payload["recommended_stream"] = recommended_stream
        if recommended_model_tier is not None:
            payload["recommended_model_tier"] = recommended_model_tier
        if matched is not None:
            payload["matched"] = matched
        if candidate_count is not None:
            payload["candidate_count"] = candidate_count
        if utility_score is not None:
            payload["utility_score"] = utility_score
        if hold_reason is not None:
            payload["hold_reason"] = hold_reason
        self._append_jsonl(self.routing_decision_path, payload)
        return payload

    def emit_llm_usage_event(
        self,
        *,
        trace_id: str,
        task_id: str,
        attempt_id: str,
        actor_id: str,
        provider: str,
        model: str,
        model_tier: str,
        routing_target: str,
        estimated_cost_bucket: str,
        input_tokens: int,
        output_tokens: int,
        estimated_cost_usd: float,
    ) -> dict[str, Any]:
        payload = {
            "event_id": f"llm-{uuid.uuid4().hex[:16]}",
            "trace_id": trace_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "actor_id": actor_id,
            "provider": provider,
            "model": model,
            "model_tier": model_tier,
            "routing_target": routing_target,
            "estimated_cost_bucket": estimated_cost_bucket,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "estimated_cost_usd": estimated_cost_usd,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        self._append_jsonl(self.llm_usage_event_path, payload)
        return payload

    def read_llm_usage_events(self, limit: int = 20000) -> list[dict[str, Any]]:
        if not os.path.exists(self.llm_usage_event_path):
            return []
        with open(self.llm_usage_event_path) as handle:
            lines = handle.readlines()[-limit:]
        events: list[dict[str, Any]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                events.append(payload)
        return events

    def emit_budget_event(
        self,
        *,
        event_type: str,
        trace_id: str | None,
        task_id: str | None,
        attempt_id: str | None,
        actor_id: str,
        lane: str,
        detail: str,
        attributes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "event_id": f"budget-{uuid.uuid4().hex[:16]}",
            "event_type": event_type,
            "trace_id": trace_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "actor_id": actor_id,
            "lane": lane,
            "detail": detail,
            "attributes": attributes or {},
            "timestamp": datetime.now(UTC).isoformat(),
        }
        self._append_jsonl(self.budget_event_path, payload)
        return payload

    def read_budget_events(self, limit: int = 20000) -> list[dict[str, Any]]:
        if not os.path.exists(self.budget_event_path):
            return []
        with open(self.budget_event_path) as handle:
            lines = handle.readlines()[-limit:]
        events: list[dict[str, Any]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                events.append(payload)
        return events

    def emit_governor_event(
        self,
        *,
        actor_id: str,
        recommendation_type: str,
        scope_type: str,
        scope_id: str,
        severity: str,
        status: str,
        reason: str,
        attributes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "event_id": f"governor-{uuid.uuid4().hex[:16]}",
            "actor_id": actor_id,
            "recommendation_type": recommendation_type,
            "scope_type": scope_type,
            "scope_id": scope_id,
            "severity": severity,
            "status": status,
            "reason": reason,
            "attributes": attributes or {},
            "timestamp": datetime.now(UTC).isoformat(),
        }
        self._append_jsonl(self.governor_event_path, payload)
        return payload

    def read_governor_events(self, limit: int = 5000) -> list[dict[str, Any]]:
        if not os.path.exists(self.governor_event_path):
            return []
        with open(self.governor_event_path) as handle:
            lines = handle.readlines()[-limit:]
        events: list[dict[str, Any]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                events.append(payload)
        return events

    def read_routing_decisions(self, limit: int = 5000) -> list[dict[str, Any]]:
        if not os.path.exists(self.routing_decision_path):
            return []
        with open(self.routing_decision_path) as handle:
            lines = handle.readlines()[-limit:]
        events: list[dict[str, Any]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                events.append(payload)
        return events

    def get_recent_submission_events(
        self,
        limit: int = 20,
        *,
        settled_only: bool = True,
    ) -> list[dict[str, Any]]:
        events = self.read_submission_events(limit=max(limit * 10, 200))
        if settled_only:
            events = [event for event in events if event.get("settled_at")]

        def _sort_key(event: dict[str, Any]) -> tuple[int, str]:
            timestamp = str(event.get("settled_at") or event.get("created_at") or "")
            return (1 if timestamp else 0, timestamp)

        events.sort(key=_sort_key, reverse=True)
        return events[:limit]

    def get_submission_summary(self) -> dict[str, Any]:
        events = self.read_submission_events(limit=5000)
        now = datetime.now(UTC)
        settled_events = [event for event in events if event.get("settled_at")]
        recent_settlement_count = 0
        latest_success: dict[str, Any] | None = None

        success_statuses = {"settled_ack", "accepted", "merged", "closed"}
        for event in settled_events:
            settled_ts = _parse_iso8601(str(event.get("settled_at", "")))
            if settled_ts and settled_ts >= now - timedelta(hours=24):
                recent_settlement_count += 1

            status = str(event.get("status", "")).strip().lower()
            if status not in success_statuses:
                continue
            if latest_success is None:
                latest_success = event
                continue

            current_ts = _parse_iso8601(str(event.get("settled_at", ""))) or datetime.min.replace(tzinfo=UTC)
            best_ts = _parse_iso8601(str(latest_success.get("settled_at", ""))) or datetime.min.replace(tzinfo=UTC)
            if current_ts > best_ts:
                latest_success = event

        return {
            "latest_successful_branch": latest_success.get("branch_name") if latest_success else None,
            "latest_successful_submission_id": latest_success.get("submission_id") if latest_success else None,
            "latest_successful_submission_url": latest_success.get("url") if latest_success else None,
            "latest_successful_trace_id": latest_success.get("trace_id") if latest_success else None,
            "latest_settlement_timestamp": latest_success.get("settled_at") if latest_success else None,
            "recent_settlement_count": recent_settlement_count,
        }

    def get_metric_dictionary(self) -> dict[str, dict[str, str]]:
        return METRIC_DICTIONARY

    def get_budget_snapshot(self, budget_config: Any) -> dict[str, Any]:
        now = datetime.now(UTC)
        usage_events = self.read_llm_usage_events(limit=50000)
        budget_events = self.read_budget_events(limit=50000)
        task_events = self.read_task_state_events(limit=50000)

        today_usage = []
        for event in usage_events:
            timestamp = _parse_iso8601(str(event.get("timestamp") or ""))
            if timestamp and _same_utc_day(timestamp, now):
                today_usage.append(event)

        total_spend_today = sum(float(event.get("estimated_cost_usd") or 0.0) for event in today_usage)
        frontier_spend_today = sum(
            float(event.get("estimated_cost_usd") or 0.0)
            for event in today_usage
            if str(event.get("model_tier") or "") == "frontier"
        )
        control_plane_spend_today = sum(
            float(event.get("estimated_cost_usd") or 0.0)
            for event in today_usage
            if str(event.get("actor_id") or "") in {"planner", "judge", "governor"}
        )
        bulk_lane_spend_today = max(0.0, total_spend_today - frontier_spend_today)

        one_hour_ago = now - timedelta(hours=1)
        recent_budget_events = []
        today_budget_events = []
        for event in budget_events:
            timestamp = _parse_iso8601(str(event.get("timestamp") or ""))
            if not timestamp:
                continue
            if _same_utc_day(timestamp, now):
                today_budget_events.append(event)
            if timestamp >= one_hour_ago:
                recent_budget_events.append(event)

        frontier_claims_last_hour = sum(
            1
            for event in recent_budget_events
            if event.get("event_type") == "frontier_claim_started"
        )
        active_frontier_claims_map: dict[tuple[str, str], str] = {}
        for event in today_budget_events:
            if str(event.get("lane") or "") != "frontier":
                continue
            key = (
                str(event.get("task_id") or ""),
                str(event.get("attempt_id") or ""),
            )
            active_frontier_claims_map[key] = str(event.get("event_type") or "")
        active_frontier_claims = sum(
            1 for event_type in active_frontier_claims_map.values() if event_type == "frontier_claim_started"
        )

        successful_attempts: set[tuple[str, str]] = set()
        for event in task_events:
            state_to = str(event.get("state_to") or "")
            timestamp = _parse_iso8601(str(event.get("timestamp") or ""))
            if state_to != "settled_ack" or not timestamp or not _same_utc_day(timestamp, now):
                continue
            successful_attempts.add(
                (str(event.get("task_id") or ""), str(event.get("attempt_id") or ""))
            )
        successful_settlements = len(successful_attempts)
        cost_per_success_usd = (
            total_spend_today / successful_settlements if successful_settlements > 0 else total_spend_today
        )

        daily_cap = float(_as_budget_value(budget_config, "daily_usd_cap", 0.0))
        frontier_daily_cap = float(_as_budget_value(budget_config, "frontier_daily_usd_cap", 0.0))
        throttle_threshold_pct = float(_as_budget_value(budget_config, "throttle_threshold_pct", 85))
        freeze_threshold_pct = float(_as_budget_value(budget_config, "freeze_threshold_pct", 100))
        cost_per_success_ceiling_usd = float(
            _as_budget_value(budget_config, "cost_per_success_ceiling_usd", 0.0)
        )
        freeze_mode = str(_as_budget_value(budget_config, "freeze_mode", "frontier_only"))

        total_budget_pct = (total_spend_today / daily_cap * 100.0) if daily_cap > 0 else 0.0
        frontier_budget_pct = (
            frontier_spend_today / frontier_daily_cap * 100.0 if frontier_daily_cap > 0 else 0.0
        )

        return {
            "total_spend_today_usd": round(total_spend_today, 6),
            "frontier_spend_today_usd": round(frontier_spend_today, 6),
            "control_plane_spend_today_usd": round(control_plane_spend_today, 6),
            "bulk_lane_spend_today_usd": round(bulk_lane_spend_today, 6),
            "frontier_claims_last_hour": frontier_claims_last_hour,
            "active_frontier_claims": active_frontier_claims,
            "successful_settlements_today": successful_settlements,
            "cost_per_success_usd": round(cost_per_success_usd, 6),
            "daily_cap_usd": daily_cap,
            "frontier_daily_cap_usd": frontier_daily_cap,
            "total_budget_pct": round(total_budget_pct, 2),
            "frontier_budget_pct": round(frontier_budget_pct, 2),
            "throttle_threshold_pct": throttle_threshold_pct,
            "freeze_threshold_pct": freeze_threshold_pct,
            "cost_per_success_ceiling_usd": cost_per_success_ceiling_usd,
            "freeze_mode": freeze_mode,
            "frontier_spend_ratio": round(
                (frontier_spend_today / total_spend_today) if total_spend_today > 0 else 0.0,
                6,
            ),
            "frontier_frozen": frontier_budget_pct >= freeze_threshold_pct if frontier_daily_cap > 0 else False,
            "total_frozen": total_budget_pct >= freeze_threshold_pct if daily_cap > 0 else False,
        }

    def build_formula_cards(self, values: dict[str, Any]) -> list[dict[str, Any]]:
        cards: list[dict[str, Any]] = []
        for metric, spec in METRIC_DICTIONARY.items():
            value = values.get(metric)
            threshold = FORMULA_THRESHOLDS.get(metric)
            threshold_state: dict[str, Any] | None = None
            if threshold is not None and isinstance(value, (int, float)):
                op = str(threshold.get("op", ""))
                target = float(threshold.get("target", 0.0))
                threshold_state = {
                    "op": op,
                    "target": target,
                    "pass": _evaluate_threshold(value, op, target),
                }
            cards.append(
                {
                    "metric": metric,
                    "value": value,
                    "formula": spec.get("formula"),
                    "unit": spec.get("unit"),
                    "description": spec.get("description"),
                    "threshold": threshold_state,
                }
            )
        return cards

    def _read_reconciliation_items(self) -> list[dict[str, Any]]:
        if not os.path.exists(self.reconciliation_path):
            return []
        try:
            with open(self.reconciliation_path) as handle:
                payload = json.load(handle)
            if isinstance(payload, list):
                return [item for item in payload if isinstance(item, dict)]
        except Exception:
            return []
        return []

    def compute_event_observability(self, window_minutes: int = 60) -> dict[str, Any]:
        now = datetime.now(UTC)
        window_start = now - timedelta(minutes=window_minutes)
        events = self.read_task_state_events()

        parsed_events: list[dict[str, Any]] = []
        for event in events:
            ts = _parse_iso8601(str(event.get("timestamp", "")))
            if ts is None:
                continue
            event["timestamp_dt"] = ts
            parsed_events.append(event)

        parsed_events.sort(key=lambda entry: entry["timestamp_dt"])
        window_events = [entry for entry in parsed_events if entry["timestamp_dt"] >= window_start]

        terminal_events = [
            entry for entry in window_events if str(entry.get("state_to", "")).lower() in TERMINAL_TASK_STATES
        ]
        settled_ack = [
            entry for entry in window_events if str(entry.get("state_to", "")).lower() == "settled_ack"
        ]
        settled_nack = [
            entry for entry in window_events if str(entry.get("state_to", "")).lower() == "settled_nack"
        ]
        dead_letter = [
            entry for entry in window_events if str(entry.get("state_to", "")).lower() == "dead_letter"
        ]

        by_attempt: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for entry in window_events:
            task_id = str(entry.get("task_id", ""))
            attempt_id = str(entry.get("attempt_id", ""))
            if task_id and attempt_id:
                by_attempt[(task_id, attempt_id)].append(entry)

        duplicate_settlement_events = 0
        false_success_events = 0
        claim_to_start_ms: list[float] = []
        settlement_latency_ms: list[float] = []

        for attempt_events in by_attempt.values():
            terminal_for_attempt = [
                entry for entry in attempt_events
                if str(entry.get("state_to", "")).lower() in TERMINAL_TASK_STATES
            ]
            if len(terminal_for_attempt) > 1:
                duplicate_settlement_events += len(terminal_for_attempt) - 1

            claimed = next(
                (
                    entry["timestamp_dt"]
                    for entry in attempt_events
                    if str(entry.get("state_to", "")).lower() == "claimed"
                ),
                None,
            )
            executing = next(
                (
                    entry["timestamp_dt"]
                    for entry in attempt_events
                    if str(entry.get("state_to", "")).lower() == "executing"
                ),
                None,
            )
            terminal_ts = next(
                (
                    entry["timestamp_dt"]
                    for entry in attempt_events
                    if str(entry.get("state_to", "")).lower() in TERMINAL_TASK_STATES
                ),
                None,
            )

            if claimed is not None and executing is not None and executing >= claimed:
                claim_to_start_ms.append((executing - claimed).total_seconds() * 1000.0)
            if claimed is not None and terminal_ts is not None and terminal_ts >= claimed:
                settlement_latency_ms.append((terminal_ts - claimed).total_seconds() * 1000.0)

            seen_states = {str(entry.get("state_to", "")).lower() for entry in attempt_events}
            for success_event in terminal_for_attempt:
                if str(success_event.get("state_to", "")).lower() != "settled_ack":
                    continue
                if "verified" not in seen_states and "submitted_pending_ledger" not in seen_states:
                    false_success_events += 1

        terminal_count = len(terminal_events)
        ack_count = len(settled_ack)
        nack_count = len(settled_nack)
        dlq_count = len(dead_letter)

        reconciliation = self._read_reconciliation_items()
        pending_reconciliation = [item for item in reconciliation if item.get("status") == "pending"]
        settled_reconciliation = [
            item
            for item in reconciliation
            if str(item.get("status", "")).lower() in {"settled_ack", "settled_nack", "dead_letter"}
        ]
        recon_clearance_minutes: list[float] = []
        for item in settled_reconciliation:
            created = _parse_iso8601(str(item.get("created_at", "")))
            updated = _parse_iso8601(str(item.get("updated_at", "")))
            if created is None or updated is None or updated < created:
                continue
            recon_clearance_minutes.append((updated - created).total_seconds() / 60.0)

        trace_spans = self.read_trace_spans()
        trace_services: dict[str, set[str]] = defaultdict(set)
        for span in trace_spans:
            trace_id = str(span.get("trace_id", "")).strip()
            service = str(span.get("service", "")).strip()
            if trace_id and service:
                trace_services[trace_id].add(service)
        continuity_count = sum(
            1 for services in trace_services.values()
            if {"planner", "worker"}.issubset(services)
            and (("aether" in services) or ("github" in services) or ("vcs" in services))
        )
        trace_total = len(trace_services)

        event_metrics = {
            "window_minutes": window_minutes,
            "events_ingested": len(window_events),
            "tasks_completed_total": ack_count,
            "tasks_nacked_total": nack_count,
            "tasks_dead_letter_total": dlq_count,
            "terminal_settlement_events": terminal_count,
            "duplicate_settlement_events": duplicate_settlement_events,
            "false_success_events": false_success_events,
            "false_success_rate": false_success_events / max(ack_count, 1),
            "settlement_correctness": (terminal_count - duplicate_settlement_events) / max(terminal_count, 1),
            "dlq_rate": dlq_count / max(terminal_count, 1),
            "p95_claim_to_start_ms": _quantile(claim_to_start_ms, 0.95),
            "p99_settlement_latency_ms": _quantile(settlement_latency_ms, 0.99),
            "aether_reconciliation_backlog": len(pending_reconciliation),
            "aether_reconciliation_backlog_p95_clearance_min": _quantile(recon_clearance_minutes, 0.95),
            "trace_total": trace_total,
            "trace_continuity_count": continuity_count,
            "trace_continuity_ratio": continuity_count / max(trace_total, 1),
            "trace_services": sorted({service for services in trace_services.values() for service in services}),
        }
        formula_cards = self.build_formula_cards(event_metrics)
        gate_verdict = all(
            card.get("threshold", {}).get("pass", True)
            for card in formula_cards
            if isinstance(card.get("threshold"), dict)
        )

        return {
            "source": self.task_state_event_path,
            "metrics": event_metrics,
            "metric_dictionary": self.get_metric_dictionary(),
            "formula_cards": formula_cards,
            "gate_a_ready": gate_verdict,
        }

    def compute_metrics(self, window_hours: int = 1) -> CoordinationMetrics:
        """Compute coordination metrics for the given time window."""
        cutoff = datetime.now(UTC) - timedelta(hours=window_hours)
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
        payload = metrics.to_dict()
        payload["event_sourced"] = self.compute_event_observability(window_minutes=60)
        return payload


# Global collector instance
_collector: MetricsCollector | None = None


def get_metrics_collector() -> MetricsCollector:
    """Get or create the global metrics collector."""
    global _collector
    if _collector is None:
        _collector = MetricsCollector()
    return _collector


def append_trace_span(
    trace_id: str,
    service: str,
    operation: str,
    actor_id: str,
    status: str = "ok",
    task_id: str | None = None,
    attempt_id: str | None = None,
    parent_span_id: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> str:
    """Append a trace span to the shared trace stream."""
    return get_metrics_collector().emit_trace_span(
        trace_id=trace_id,
        service=service,
        operation=operation,
        actor_id=actor_id,
        status=status,
        task_id=task_id,
        attempt_id=attempt_id,
        parent_span_id=parent_span_id,
        attributes=attributes,
    )


def append_submission_event(
    *,
    task_id: str,
    trace_id: str,
    attempt_id: str,
    branch_name: str,
    submission_id: str,
    submission_type: str,
    provider: str,
    url: str | None,
    status: str,
    goal: str,
    affected_paths: list[str],
    local_mode: bool,
    created_at: str,
    settled_at: str | None = None,
    reason: str | None = None,
    canonical_status: str | None = None,
    landing_verification_status: str | None = None,
    landing_verification_detail: str | None = None,
    source: str | None = None,
    source_run_id: str | None = None,
    source_task_id: str | None = None,
    source_agent_id: str | None = None,
    source_attempt_id: str | None = None,
    intent_type: str | None = None,
) -> dict[str, Any]:
    """Append a submission/settlement event to the shared event stream."""
    return get_metrics_collector().emit_submission_event(
        task_id=task_id,
        trace_id=trace_id,
        attempt_id=attempt_id,
        branch_name=branch_name,
        submission_id=submission_id,
        submission_type=submission_type,
        provider=provider,
        url=url,
        status=status,
        goal=goal,
        affected_paths=affected_paths,
        local_mode=local_mode,
        created_at=created_at,
        settled_at=settled_at,
        reason=reason,
        canonical_status=canonical_status,
        landing_verification_status=landing_verification_status,
        landing_verification_detail=landing_verification_detail,
        source=source,
        source_run_id=source_run_id,
        source_task_id=source_task_id,
        source_agent_id=source_agent_id,
        source_attempt_id=source_attempt_id,
        intent_type=intent_type,
    )


def append_decision_event(
    task_id: str,
    reasoning: str,
    choice: str,
    impact_level: str = "medium",
    context: dict[str, Any] | None = None,
) -> None:
    """Record an autonomous decision for 'Decision Transparency'."""
    get_metrics_collector().emit_decision(
        task_id=task_id,
        reasoning=reasoning,
        choice=choice,
        impact_level=impact_level,
        context=context,
    )


def append_routing_decision(
    *,
    task_id: str,
    trace_id: str | None,
    target: str,
    reason: str,
    latest_reason_code: str,
    consecutive_failures: int,
    attempt_ids: list[str],
    failure_signature: str | None = None,
    actor_id: str = "judge",
    mode: str = "deterministic_failure",
    actual_lane_key: str | None = None,
    actual_stream: str | None = None,
    actual_model_tier: str | None = None,
    recommended_lane_key: str | None = None,
    recommended_stream: str | None = None,
    recommended_model_tier: str | None = None,
    matched: bool | None = None,
    candidate_count: int | None = None,
    utility_score: float | None = None,
    hold_reason: str | None = None,
) -> dict[str, Any]:
    return get_metrics_collector().emit_routing_decision(
        task_id=task_id,
        trace_id=trace_id,
        target=target,
        reason=reason,
        latest_reason_code=latest_reason_code,
        consecutive_failures=consecutive_failures,
        attempt_ids=attempt_ids,
        failure_signature=failure_signature,
        actor_id=actor_id,
        mode=mode,
        actual_lane_key=actual_lane_key,
        actual_stream=actual_stream,
        actual_model_tier=actual_model_tier,
        recommended_lane_key=recommended_lane_key,
        recommended_stream=recommended_stream,
        recommended_model_tier=recommended_model_tier,
        matched=matched,
        candidate_count=candidate_count,
        utility_score=utility_score,
        hold_reason=hold_reason,
    )


def append_llm_usage_event(
    *,
    trace_id: str,
    task_id: str,
    attempt_id: str,
    actor_id: str,
    provider: str,
    model: str,
    model_tier: str,
    routing_target: str,
    estimated_cost_bucket: str,
    input_tokens: int,
    output_tokens: int,
    estimated_cost_usd: float,
) -> dict[str, Any]:
    return get_metrics_collector().emit_llm_usage_event(
        trace_id=trace_id,
        task_id=task_id,
        attempt_id=attempt_id,
        actor_id=actor_id,
        provider=provider,
        model=model,
        model_tier=model_tier,
        routing_target=routing_target,
        estimated_cost_bucket=estimated_cost_bucket,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost_usd=estimated_cost_usd,
    )


def append_budget_event(
    *,
    event_type: str,
    trace_id: str | None,
    task_id: str | None,
    attempt_id: str | None,
    actor_id: str,
    lane: str,
    detail: str,
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return get_metrics_collector().emit_budget_event(
        event_type=event_type,
        trace_id=trace_id,
        task_id=task_id,
        attempt_id=attempt_id,
        actor_id=actor_id,
        lane=lane,
        detail=detail,
        attributes=attributes,
    )


def append_governor_event(
    *,
    actor_id: str,
    recommendation_type: str,
    scope_type: str,
    scope_id: str,
    severity: str,
    status: str,
    reason: str,
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return get_metrics_collector().emit_governor_event(
        actor_id=actor_id,
        recommendation_type=recommendation_type,
        scope_type=scope_type,
        scope_id=scope_id,
        severity=severity,
        status=status,
        reason=reason,
        attributes=attributes,
    )


def get_budget_snapshot(budget_config: Any) -> dict[str, Any]:
    return get_metrics_collector().get_budget_snapshot(budget_config)
