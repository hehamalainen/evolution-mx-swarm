"""Control-plane telemetry views for CLI/operator surfaces."""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from shared.schemas import TaskContract
from shared.schemas.teamplan import TeamPlan
from shared.utils.capability_registry import get_capability_registry
from shared.utils.metrics import MetricsCollector, get_budget_snapshot
from shared.utils.routing import build_shadow_lanes

TERMINAL_STATES = {"settled_ack", "settled_nack", "dead_letter"}


def _parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        normalized = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    except Exception:
        return None


def _load_teamplan(repo_path: str = ".") -> TeamPlan | None:
    path = os.path.join(repo_path, "TEAMPLAN.json")
    if not os.path.exists(path):
        return None
    return TeamPlan.load(path)


def _metrics_for_repo(repo_path: str, metrics_collector: MetricsCollector | None = None) -> MetricsCollector:
    metrics = metrics_collector or MetricsCollector()
    runtime_dir = os.path.join(repo_path, ".runtime")
    metrics.task_state_event_path = os.path.join(runtime_dir, "task_state_events.jsonl")
    metrics.trace_span_path = os.path.join(runtime_dir, "trace_spans.jsonl")
    metrics.submission_event_path = os.path.join(runtime_dir, "submission_events.jsonl")
    metrics.reconciliation_path = os.path.join(runtime_dir, "aether_reconciliation.json")
    metrics.routing_decision_path = os.path.join(runtime_dir, "routing_decisions.jsonl")
    metrics.llm_usage_event_path = os.path.join(runtime_dir, "llm_usage_events.jsonl")
    metrics.budget_event_path = os.path.join(runtime_dir, "budget_events.jsonl")
    metrics.governor_event_path = os.path.join(runtime_dir, "governor_events.jsonl")
    return metrics


def _load_task_snapshots(repo_path: str = ".") -> dict[str, TaskContract]:
    snapshot_dir = Path(repo_path) / ".runtime" / "task_snapshots"
    if not snapshot_dir.exists():
        return {}
    tasks: dict[str, TaskContract] = {}
    for path in snapshot_dir.glob("*.json"):
        try:
            payload = json.loads(path.read_text())
            task_payload = payload.get("task", payload)
            if isinstance(task_payload, dict):
                task = TaskContract.from_dict(task_payload)
                tasks[task.id] = task
        except Exception:
            continue
    return tasks


def _latest_by_task(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for event in events:
        task_id = str(event.get("task_id") or "").strip()
        if task_id:
            latest[task_id] = event
    return latest


def _latest_shadow_by_task(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for event in events:
        if str(event.get("actor_id") or "") != "shadow_scheduler":
            continue
        task_id = str(event.get("task_id") or "").strip()
        if task_id:
            latest[task_id] = event
    return latest


def _derive_task_rig(
    task: TaskContract,
    shadow_decision: dict[str, Any] | None,
) -> str:
    if shadow_decision:
        lane_key = str(shadow_decision.get("recommended_lane_key") or "").strip()
        if lane_key:
            return lane_key.split(":")[0]
    return str(task.rig_id or "default")


def _sink_backpressure_label(backlog: int) -> str:
    if backlog <= 0:
        return "LOW"
    if backlog < 5:
        return "MEDIUM"
    return "HIGH"


def _status_precedence(status: str) -> int:
    order = {
        "FROZEN": 4,
        "DRAINING": 3,
        "THROTTLED": 2,
        "QA_ELEVATED": 1,
        "ACTIVE": 0,
    }
    return order.get(status, -1)


def _derive_rig_statuses(
    rig_ids: list[str],
    governor_events: list[dict[str, Any]],
) -> dict[str, str]:
    statuses = dict.fromkeys(rig_ids, "ACTIVE")
    for event in governor_events:
        if str(event.get("scope_type") or "") != "rig":
            continue
        rig_id = str(event.get("scope_id") or "").strip()
        if not rig_id:
            continue
        recommendation_type = str(event.get("recommendation_type") or "").strip().upper()
        status = str(event.get("status") or "ACTIVE").strip().upper()
        if recommendation_type == "QA_DENSITY_SURGE":
            status = "QA_ELEVATED"
        if _status_precedence(status) > _status_precedence(statuses.get(rig_id, "ACTIVE")):
            statuses[rig_id] = status
    return statuses


def compute_rig_telemetry(
    repo_path: str = ".",
    *,
    teamplan: TeamPlan | None = None,
    metrics_collector: MetricsCollector | None = None,
) -> list[dict[str, Any]]:
    metrics = _metrics_for_repo(repo_path, metrics_collector)
    plan = teamplan or _load_teamplan(repo_path)
    if plan is None or not plan.rig_plans:
        return []

    capability_snapshot = get_capability_registry(repo_path=repo_path).get_routing_snapshot()
    lanes = build_shadow_lanes(plan.rig_plans, capability_snapshot)
    lane_by_rig: dict[str, list[Any]] = defaultdict(list)
    for lane in lanes:
        rig_id = lane.lane_key.split(":")[0]
        lane_by_rig[rig_id].append(lane)

    task_events = metrics.read_task_state_events(limit=50000)
    latest_task_events = _latest_by_task(task_events)
    shadow_decisions = _latest_shadow_by_task(metrics.read_routing_decisions(limit=50000))
    task_snapshots = _load_task_snapshots(repo_path=repo_path)

    task_to_rig: dict[str, str] = {}
    queue_counts: Counter[str] = Counter()
    blocked_counts: Counter[str] = Counter()
    blocked_dependency_counts: Counter[str] = Counter()

    for task_id, task in task_snapshots.items():
        rig_id = _derive_task_rig(task, shadow_decisions.get(task_id))
        task_to_rig[task_id] = rig_id
        latest_event = latest_task_events.get(task_id, {})
        latest_state = str(latest_event.get("state_to") or task.state.value).strip().lower()
        if latest_state in TERMINAL_STATES:
            continue
        queue_counts[rig_id] += 1
        if latest_state == "blocked":
            blocked_counts[rig_id] += 1
            if str(latest_event.get("reason") or "").strip().upper() == "DEPENDENCY_WAIT":
                blocked_dependency_counts[rig_id] += 1

    terminal_counts: Counter[str] = Counter()
    acceptance_fail_counts: Counter[str] = Counter()
    false_success_counts: Counter[str] = Counter()
    by_attempt: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in task_events:
        task_id = str(event.get("task_id") or "").strip()
        attempt_id = str(event.get("attempt_id") or "").strip()
        if task_id and attempt_id and task_id in task_to_rig:
            by_attempt[(task_id, attempt_id)].append(event)

    for (task_id, _attempt_id), attempt_events in by_attempt.items():
        rig_id = task_to_rig[task_id]
        states = {str(event.get("state_to") or "").strip().lower() for event in attempt_events}
        terminal_events = [
            event for event in attempt_events if str(event.get("state_to") or "").strip().lower() in TERMINAL_STATES
        ]
        if not terminal_events:
            continue
        terminal_events.sort(key=lambda item: str(item.get("timestamp") or ""))
        latest_terminal = terminal_events[-1]
        terminal_counts[rig_id] += 1
        if str(latest_terminal.get("reason") or "").strip() == "ERR_CHECK_FAIL":
            acceptance_fail_counts[rig_id] += 1
        if (
            str(latest_terminal.get("state_to") or "").strip().lower() == "settled_ack"
            and "verified" not in states
            and "submitted_pending_ledger" not in states
        ):
            false_success_counts[rig_id] += 1

    now = datetime.now(UTC)
    llm_usage_events = metrics.read_llm_usage_events(limit=50000)
    frontier_spend: dict[str, float] = defaultdict(float)
    for event in llm_usage_events:
        timestamp = _parse_iso8601(str(event.get("timestamp") or ""))
        if timestamp is None or timestamp.date() != now.date():
            continue
        if str(event.get("model_tier") or "") != "frontier":
            continue
        task_id = str(event.get("task_id") or "").strip()
        event_rig_id = task_to_rig.get(task_id)
        if event_rig_id:
            frontier_spend[event_rig_id] += float(event.get("estimated_cost_usd") or 0.0)

    governor_events = metrics.read_governor_events(limit=5000)
    rig_ids = sorted({lane_key.split(":")[0] for lane_key in lane_by_rig} | set(task_to_rig.values()))
    rig_statuses = _derive_rig_statuses(rig_ids, governor_events)

    telemetry: list[dict[str, Any]] = []
    for rig_plan in plan.rig_plans:
        rig_id = str(rig_plan.get("rig_id") or "").strip()
        lanes_for_rig = lane_by_rig.get(rig_id, [])
        if not rig_id:
            continue
        width = sum(int(lane.concurrency_cap) for lane in lanes_for_rig)
        if lanes_for_rig:
            pass_rate = sum(float(lane.capabilities.pass_rate) for lane in lanes_for_rig) / len(lanes_for_rig)
            cost_per_success = (
                sum(float(lane.capabilities.cost_per_success_usd) for lane in lanes_for_rig) / len(lanes_for_rig)
            )
            escalation_rate = (
                sum(float(lane.capabilities.escalation_rate) for lane in lanes_for_rig) / len(lanes_for_rig)
            )
            transient_failure_rate = (
                sum(float(lane.capabilities.transient_failure_rate) for lane in lanes_for_rig)
                / len(lanes_for_rig)
            )
        else:
            pass_rate = 0.5
            cost_per_success = 0.0
            escalation_rate = 0.0
            transient_failure_rate = 0.0

        task_count = int(queue_counts.get(rig_id, 0))
        blocked_tasks = int(blocked_counts.get(rig_id, 0))
        blocked_dependency_ratio = blocked_tasks / max(task_count, 1)
        terminal_count = int(terminal_counts.get(rig_id, 0))
        acceptance_failure_rate = acceptance_fail_counts.get(rig_id, 0) / max(terminal_count, 1)
        false_success_rate = false_success_counts.get(rig_id, 0) / max(terminal_count, 1)

        telemetry.append(
            {
                "rig_id": rig_id,
                "task_count": task_count,
                "width": width,
                "pass_rate": pass_rate,
                "cost_per_success_usd": round(cost_per_success, 6),
                "escalation_rate": escalation_rate,
                "transient_failure_rate": transient_failure_rate,
                "blocked_tasks": blocked_tasks,
                "blocked_dependency_tasks": int(blocked_dependency_counts.get(rig_id, 0)),
                "blocked_dependency_ratio": blocked_dependency_ratio,
                "acceptance_failure_rate": acceptance_failure_rate,
                "false_success_rate": false_success_rate,
                "frontier_spend_usd": round(float(frontier_spend.get(rig_id, 0.0)), 6),
                "frontier_budget_cap_usd": float(rig_plan.get("frontier_budget_cap_usd") or 0.0),
                "status": rig_statuses.get(rig_id, "ACTIVE"),
                "lane_count": len(lanes_for_rig),
            }
        )

    telemetry.sort(key=lambda item: (item["task_count"], item["width"]), reverse=True)
    return telemetry


def build_control_plane_snapshot(
    *,
    repo_path: str = ".",
    budget_config: Any,
    limit_rigs: int = 5,
) -> dict[str, Any]:
    metrics = _metrics_for_repo(repo_path)
    teamplan = _load_teamplan(repo_path)
    rig_telemetry = compute_rig_telemetry(repo_path, teamplan=teamplan, metrics_collector=metrics)
    budget_snapshot = get_budget_snapshot(budget_config)
    observability = metrics.compute_event_observability(window_minutes=60)
    routing_events = metrics.read_routing_decisions(limit=50000)
    governor_events = metrics.read_governor_events(limit=50000)

    hold_reasons = Counter(
        str(event.get("hold_reason") or "").strip()
        for event in routing_events
        if str(event.get("actor_id") or "") == "shadow_scheduler" and str(event.get("hold_reason") or "").strip()
    )
    total_holds = sum(hold_reasons.values())
    hold_distribution = [
        {
            "reason": reason,
            "count": count,
            "pct": (count / total_holds) if total_holds > 0 else 0.0,
        }
        for reason, count in hold_reasons.most_common()
    ]

    lanes_active = lanes_frozen = lanes_throttled = lanes_draining = 0
    for rig in rig_telemetry:
        lane_count = int(rig.get("lane_count") or 0)
        status = str(rig.get("status") or "ACTIVE").upper()
        if status == "FROZEN":
            lanes_frozen += lane_count
        elif status == "DRAINING":
            lanes_draining += lane_count
        elif status in {"THROTTLED", "QA_ELEVATED"}:
            lanes_throttled += lane_count
        else:
            lanes_active += lane_count

    recent_governor = sorted(
        governor_events,
        key=lambda item: str(item.get("timestamp") or ""),
        reverse=True,
    )[:5]

    macro = {
        "verified_prs_per_hour": observability["metrics"].get("tasks_completed_total", 0),
        "false_success_pct": observability["metrics"].get("false_success_rate", 0.0) * 100.0,
        "sink_backpressure": _sink_backpressure_label(
            int(observability["metrics"].get("aether_reconciliation_backlog", 0) or 0)
        ),
        "transient_noise_pct": (
            (
                sum(float(rig.get("transient_failure_rate") or 0.0) for rig in rig_telemetry)
                / len(rig_telemetry)
            )
            * 100.0
            if rig_telemetry
            else 0.0
        ),
        "total_spend_usd": budget_snapshot["total_spend_today_usd"],
        "cost_per_success_usd": budget_snapshot["cost_per_success_usd"],
        "frontier_ratio_pct": budget_snapshot["frontier_spend_ratio"] * 100.0,
        "lanes_active": lanes_active,
        "lanes_frozen": lanes_frozen,
        "lanes_throttled": lanes_throttled,
        "lanes_draining": lanes_draining,
        "dependency_blocked_tasks": sum(int(rig["blocked_dependency_tasks"]) for rig in rig_telemetry),
    }

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "macro": macro,
        "rigs": rig_telemetry[:limit_rigs],
        "hold_distribution": hold_distribution,
        "recent_governor": recent_governor,
        "budget": budget_snapshot,
        "observability": observability["metrics"],
    }


def render_mxtop(snapshot: dict[str, Any]) -> str:
    macro = snapshot["macro"]
    rigs = snapshot["rigs"]
    holds = snapshot["hold_distribution"]
    governor = snapshot["recent_governor"]

    lines = []
    lines.append("=" * 80)
    lines.append("MX SWARM CONTROL PLANE")
    lines.append("=" * 80)
    lines.append(
        "MACRO "
        f"| Verified/hr: {macro['verified_prs_per_hour']} "
        f"| False-Success: {macro['false_success_pct']:.2f}% "
        f"| Transient Noise: {macro['transient_noise_pct']:.2f}% "
        f"| Sink Backpressure: {macro['sink_backpressure']}"
    )
    lines.append(
        "ECONOMICS "
        f"| Spend: ${macro['total_spend_usd']:.4f} "
        f"| Cost/Success: ${macro['cost_per_success_usd']:.4f} "
        f"| Frontier Ratio: {macro['frontier_ratio_pct']:.2f}%"
    )
    lines.append(
        "GOVERNANCE "
        f"| Active Lanes: {macro['lanes_active']} "
        f"| Frozen: {macro['lanes_frozen']} "
        f"| Throttled: {macro['lanes_throttled']} "
        f"| Draining: {macro['lanes_draining']} "
        f"| Dep Blocked: {macro['dependency_blocked_tasks']}"
    )
    lines.append("-" * 80)
    lines.append("RIG TELEMETRY")
    lines.append("RIG_ID                    TASKS  WID  PASS%  NOISE%  COST/SUCC  ESC%   STATUS")
    for rig in rigs:
        lines.append(
            f"{rig['rig_id'][:24]:24} "
            f"{rig['task_count']:>5} "
            f"{rig['width']:>4} "
            f"{rig['pass_rate'] * 100:>5.1f}% "
            f"{rig['transient_failure_rate'] * 100:>6.1f}% "
            f"${rig['cost_per_success_usd']:<9.4f} "
            f"{rig['escalation_rate'] * 100:>5.1f}% "
            f"{rig['status']}"
        )
    if not rigs:
        lines.append("(no rig telemetry yet)")
    lines.append("-" * 80)
    lines.append("HOLD REASON DISTRIBUTION")
    if holds:
        for item in holds[:5]:
            lines.append(
                f"{item['reason'][:28]:28} {item['count']:>5}  {item['pct'] * 100:>5.1f}%"
            )
    else:
        lines.append("(no shadow holds yet)")
    lines.append("-" * 80)
    lines.append("RECENT GOVERNOR RECOMMENDATIONS")
    if governor:
        for event in governor:
            lines.append(
                f"{str(event.get('recommendation_type') or ''):18} "
                f"{str(event.get('scope_id') or ''):24} "
                f"{str(event.get('status') or ''):12} "
                f"{str(event.get('reason') or '')}"
            )
    else:
        lines.append("(no governor recommendations yet)")
    return "\n".join(lines)
