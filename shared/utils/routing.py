"""Deterministic routing decisions for Gate 3 escalation and PR4 shadow scheduling."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from shared.schemas import ModelTier, NackReason, RoutingTarget, TaskContract, TaskState
from shared.schemas.org_topology import (
    CapabilityVector,
    RigPlan,
    RoutingHoldReason,
    WorkerLane,
    build_lane_key,
)

CAPABILITY_FAILURE_REASONS = {
    NackReason.ERR_NO_DIFF.value,
    NackReason.ERR_SYNTAX.value,
    NackReason.ERR_REFUSAL.value,
}
OPS_FAILURE_REASONS = {
    NackReason.ERR_QUEUE_LEASE.value,
    NackReason.ERR_LEDGER_UNAVAILABLE.value,
    NackReason.ERR_RATE_LIMIT.value,
    NackReason.ERR_TASK_FAILED.value,
    NackReason.ERR_RECON_DIVERGENCE.value,
}
FAILURE_TERMINAL_STATES = {"settled_nack", "dead_letter"}
LANGUAGE_HINTS = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".toml": "toml",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".sh": "shell",
}
W_PASS = 1.0
W_TASK_CLASS = 0.8
W_TOOLCHAIN = 0.4
W_COST = 0.35
W_ESCALATION = 0.8
W_LOAD = 0.25
MIN_SHADOW_UTILITY = 0.2


@dataclass(frozen=True)
class RoutingDecision:
    task_id: str
    trace_id: str | None
    target: RoutingTarget
    reason: str
    latest_reason_code: str
    consecutive_failures: int
    failure_signature: str | None
    attempt_ids: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "trace_id": self.trace_id,
            "target": self.target.value,
            "reason": self.reason,
            "latest_reason_code": self.latest_reason_code,
            "consecutive_failures": self.consecutive_failures,
            "failure_signature": self.failure_signature,
            "attempt_ids": self.attempt_ids,
        }


@dataclass(frozen=True)
class ShadowRoutingDecision:
    task_id: str
    trace_id: str | None
    actual_lane_key: str
    actual_stream: str
    actual_model_tier: str
    recommended_lane_key: str | None
    recommended_stream: str | None
    recommended_model_tier: str | None
    matched: bool
    hold_reason: RoutingHoldReason | None
    candidate_count: int
    utility_score: float | None
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "trace_id": self.trace_id,
            "actual_lane_key": self.actual_lane_key,
            "actual_stream": self.actual_stream,
            "actual_model_tier": self.actual_model_tier,
            "recommended_lane_key": self.recommended_lane_key,
            "recommended_stream": self.recommended_stream,
            "recommended_model_tier": self.recommended_model_tier,
            "matched": self.matched,
            "hold_reason": self.hold_reason.value if self.hold_reason else None,
            "candidate_count": self.candidate_count,
            "utility_score": self.utility_score,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class _ScoredLane:
    lane: WorkerLane
    utility: float


def normalize_failure_signature(value: str | None) -> str | None:
    if not value:
        return None
    normalized = " ".join(value.split()).strip().lower()
    if not normalized:
        return None
    return normalized[:240]


def extract_task_failures(task_id: str, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matches = [
        event
        for event in events
        if str(event.get("task_id", "")).strip() == task_id
        and str(event.get("state_to", "")).strip().lower() in FAILURE_TERMINAL_STATES
        and str(event.get("reason", "")).strip()
    ]
    matches.sort(key=lambda item: str(item.get("timestamp", "")), reverse=True)
    return matches


def route_task_failures(
    task_id: str,
    events: list[dict[str, Any]],
    *,
    consecutive_threshold: int = 3,
) -> RoutingDecision | None:
    failures = extract_task_failures(task_id, events)
    if len(failures) < consecutive_threshold:
        return None

    streak = [failures[0]]
    newest_reason = str(failures[0].get("reason", "")).strip()
    for event in failures[1:]:
        reason = str(event.get("reason", "")).strip()
        if reason == newest_reason:
            streak.append(event)
            continue
        break

    mixed_reasons = {str(event.get("reason", "")).strip() for event in failures[:consecutive_threshold]}
    consecutive_count = len(streak)
    latest_signature = normalize_failure_signature(str(failures[0].get("detail", "") or ""))
    attempt_ids = [str(event.get("attempt_id", "")).strip() for event in streak if event.get("attempt_id")]

    if consecutive_count >= consecutive_threshold and newest_reason in CAPABILITY_FAILURE_REASONS:
        return RoutingDecision(
            task_id=task_id,
            trace_id=str(failures[0].get("trace_id") or "") or None,
            target=RoutingTarget.FRONTIER,
            reason=f"{newest_reason} repeated {consecutive_count} consecutive times; route out of local lane.",
            latest_reason_code=newest_reason,
            consecutive_failures=consecutive_count,
            failure_signature=latest_signature,
            attempt_ids=attempt_ids,
        )

    if consecutive_count >= consecutive_threshold and newest_reason == NackReason.ERR_CHECK_FAIL.value:
        signatures = {
            normalize_failure_signature(str(event.get("detail", "") or ""))
            for event in streak[:consecutive_threshold]
        }
        if len(signatures) == 1:
            return RoutingDecision(
                task_id=task_id,
                trace_id=str(failures[0].get("trace_id") or "") or None,
                target=RoutingTarget.REPLAN,
                reason="ERR_CHECK_FAIL repeated with stable failure signature; task decomposition should be replanned.",
                latest_reason_code=newest_reason,
                consecutive_failures=consecutive_count,
                failure_signature=latest_signature,
                attempt_ids=attempt_ids,
            )

    if consecutive_count >= consecutive_threshold and newest_reason in OPS_FAILURE_REASONS:
        return RoutingDecision(
            task_id=task_id,
            trace_id=str(failures[0].get("trace_id") or "") or None,
            target=RoutingTarget.HUMAN_REVIEW,
            reason=f"{newest_reason} repeated {consecutive_count} times; requires operator or infrastructure review.",
            latest_reason_code=newest_reason,
            consecutive_failures=consecutive_count,
            failure_signature=latest_signature,
            attempt_ids=attempt_ids,
        )

    if len(mixed_reasons) >= 2 and len(failures) >= consecutive_threshold:
        return RoutingDecision(
            task_id=task_id,
            trace_id=str(failures[0].get("trace_id") or "") or None,
            target=RoutingTarget.REPLAN,
            reason="Mixed failure reasons across recent attempts indicate task contract drift; replan instead of escalating blindly.",
            latest_reason_code=newest_reason,
            consecutive_failures=1,
            failure_signature=latest_signature,
            attempt_ids=[
                str(event.get("attempt_id", "")).strip()
                for event in failures[:consecutive_threshold]
                if event.get("attempt_id")
            ],
        )

    return None


def _normalize_stream_name(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"doc", "docs"}:
        return "docs"
    if normalized in {"qa", "test"}:
        return "qa"
    if normalized in {"sec", "security"}:
        return "security"
    if normalized in {"integration", "integrations"}:
        return "integration"
    if normalized in {"ops", "operations"}:
        return "ops"
    return "feature"


def _logical_stream_name(stream_plan: Any, shard_plan: Any) -> str:
    stream_type = str(getattr(stream_plan, "stream_type", "") or "").strip().lower()
    if stream_type == "qa_verification":
        return "qa"
    if stream_type == "security_audit":
        return "security"
    if stream_type == "ops":
        return "ops"

    target_classes = {str(value).strip().lower() for value in getattr(shard_plan, "target_task_classes", [])}
    if target_classes == {"doc"}:
        return "docs"
    if "integration" in target_classes and "feature" not in target_classes:
        return "integration"
    if target_classes == {"ops"}:
        return "ops"
    return "feature"


def _estimate_mutable_files(task: TaskContract) -> int:
    files = [
        str(path).strip()
        for path in task.context.files_hint
        if str(path).strip() and str(path).strip() not in set(task.context.do_not_touch)
    ]
    return max(1, len(set(files)))


def _infer_toolchain(task: TaskContract) -> str | None:
    hints = [str(path).strip().lower() for path in task.context.files_hint if str(path).strip()]
    for hint in hints:
        for suffix, toolchain in LANGUAGE_HINTS.items():
            if hint.endswith(suffix):
                return toolchain
        if hint.endswith("dockerfile"):
            return "docker"

    checks = " ".join(task.acceptance.checks).lower()
    if "pytest" in checks or "ruff" in checks:
        return "python"
    if "npm" in checks or "pnpm" in checks or "vitest" in checks:
        return "typescript"
    return None


def _estimate_expected_cost_usd(bucket: str) -> float:
    normalized = str(bucket or "").strip().lower()
    if normalized == "high":
        return 1.0
    if normalized == "medium":
        return 0.25
    return 0.05


def _estimate_coupling_density(task: TaskContract) -> float:
    dependency_pressure = 0.2 * len(task.prerequisite_task_ids)
    mutable_pressure = 0.1 * max(_estimate_mutable_files(task) - 1, 0)
    return min(1.0, dependency_pressure + mutable_pressure)


def _build_actual_lane_key(task: TaskContract) -> str:
    return build_lane_key(
        rig_id=task.rig_id,
        stream_id=_normalize_stream_name(task.stream.value if hasattr(task.stream, "value") else str(task.stream)),
        shard_id=task.shard_key or "default",
        model_tier=task.model_tier.value if hasattr(task.model_tier, "value") else str(task.model_tier),
        provider_model="live",
    )


def _merge_capabilities(matches: list[CapabilityVector]) -> CapabilityVector:
    if not matches:
        return CapabilityVector()
    if len(matches) == 1:
        source = matches[0]
        return CapabilityVector(
            pass_rate=source.pass_rate,
            cost_per_success_usd=source.cost_per_success_usd,
            settlement_latency_ema_s=source.settlement_latency_ema_s,
            escalation_rate=source.escalation_rate,
            false_success_penalty_multiplier=source.false_success_penalty_multiplier,
            task_class_strengths=dict(source.task_class_strengths),
            toolchain_strengths=dict(source.toolchain_strengths),
            last_updated_ts=source.last_updated_ts,
        )

    merged = CapabilityVector()
    merged.pass_rate = sum(item.pass_rate for item in matches) / len(matches)
    merged.cost_per_success_usd = sum(item.cost_per_success_usd for item in matches) / len(matches)
    merged.settlement_latency_ema_s = sum(item.settlement_latency_ema_s for item in matches) / len(matches)
    merged.escalation_rate = sum(item.escalation_rate for item in matches) / len(matches)
    merged.false_success_penalty_multiplier = (
        sum(item.false_success_penalty_multiplier for item in matches) / len(matches)
    )

    task_keys = {key for item in matches for key in item.task_class_strengths}
    merged.task_class_strengths = {
        key: sum(item.task_class_strengths.get(key, 0.5) for item in matches) / len(matches)
        for key in task_keys
    }
    toolchain_keys = {key for item in matches for key in item.toolchain_strengths}
    merged.toolchain_strengths = {
        key: sum(item.toolchain_strengths.get(key, 0.5) for item in matches) / len(matches)
        for key in toolchain_keys
    }
    merged.last_updated_ts = max(item.last_updated_ts for item in matches)
    return merged


def _select_capability(
    snapshot: Mapping[str, CapabilityVector],
    *,
    rig_id: str,
    stream_name: str,
    shard_id: str,
    model_tier: str,
) -> CapabilityVector:
    exact_prefix = f"{rig_id}:{stream_name}:{shard_id}:{model_tier}:"
    exact_matches = [value for key, value in snapshot.items() if key.startswith(exact_prefix)]
    if exact_matches:
        return _merge_capabilities(exact_matches)

    stream_matches: list[CapabilityVector] = []
    for key, value in snapshot.items():
        parts = key.split(":")
        if len(parts) < 5:
            continue
        if parts[1] == stream_name and parts[3] == model_tier:
            stream_matches.append(value)
    if stream_matches:
        return _merge_capabilities(stream_matches)
    return CapabilityVector()


def build_shadow_lanes(
    rig_plan_payloads: list[dict[str, Any]] | None,
    capability_snapshot: Mapping[str, CapabilityVector],
) -> list[WorkerLane]:
    if not rig_plan_payloads:
        return []

    lanes: list[WorkerLane] = []
    for payload in rig_plan_payloads:
        rig_plan = RigPlan.model_validate(payload)
        for stream_plan in rig_plan.streams:
            for shard_plan in stream_plan.shards:
                logical_stream = _logical_stream_name(stream_plan, shard_plan)
                for model_tier in shard_plan.allowed_model_tiers or [ModelTier.LOCAL_OPEN_WEIGHT.value]:
                    lane_key = build_lane_key(
                        rig_id=rig_plan.rig_id,
                        stream_id=logical_stream,
                        shard_id=shard_plan.shard_id,
                        model_tier=model_tier,
                        provider_model="shadow",
                    )
                    cost_envelope = rig_plan.frontier_budget_cap_usd if model_tier == ModelTier.FRONTIER.value else 1_000_000.0
                    lanes.append(
                        WorkerLane(
                            lane_key=lane_key,
                            model_tier=model_tier,
                            target_task_classes={str(value).strip().lower() for value in shard_plan.target_task_classes},
                            supported_toolchains={str(value).strip().lower() for value in shard_plan.supported_toolchains},
                            max_mutable_files=shard_plan.max_mutable_files,
                            capabilities=_select_capability(
                                capability_snapshot,
                                rig_id=rig_plan.rig_id,
                                stream_name=logical_stream,
                                shard_id=shard_plan.shard_id,
                                model_tier=model_tier,
                            ),
                            concurrency_cap=shard_plan.concurrency_cap,
                            cost_envelope_usd=cost_envelope,
                        )
                    )
    return lanes


def _filter_candidate_lanes(task: TaskContract, lanes: list[WorkerLane]) -> tuple[list[WorkerLane], RoutingHoldReason | None]:
    if task.routing_target == RoutingTarget.REPLAN:
        return [], RoutingHoldReason.REPLAN_REQUIRED
    if task.prerequisite_task_ids and task.state == TaskState.BLOCKED:
        return [], RoutingHoldReason.DEPENDENCY_BLOCKED

    task_class = task.type.value if hasattr(task.type, "value") else str(task.type)
    toolchain = _infer_toolchain(task)
    mutable_files = _estimate_mutable_files(task)
    requires_frontier = (
        task.routing_target == RoutingTarget.FRONTIER
        or task.model_tier == ModelTier.FRONTIER
        or (task.frontier_eligible and task.frontier_attempt_count > 0)
    )
    expected_cost = _estimate_expected_cost_usd(task.estimated_cost_bucket)

    candidates: list[WorkerLane] = []
    economic_blocked = False
    for lane in lanes:
        if lane.status.value != "ACTIVE":
            continue
        if lane.active_workers >= lane.concurrency_cap:
            continue
        if task_class and lane.target_task_classes and task_class not in lane.target_task_classes:
            continue
        if toolchain and lane.supported_toolchains and toolchain not in lane.supported_toolchains:
            continue
        if mutable_files > lane.max_mutable_files:
            continue
        if requires_frontier and lane.model_tier != ModelTier.FRONTIER.value:
            continue
        if not requires_frontier and lane.model_tier == ModelTier.FRONTIER.value and not task.frontier_eligible:
            continue
        if lane.cost_envelope_usd > 0 and (lane.current_spend_usd + expected_cost) > lane.cost_envelope_usd:
            economic_blocked = True
            continue
        candidates.append(lane)

    if candidates:
        return candidates, None
    if economic_blocked:
        return [], RoutingHoldReason.ECONOMIC_FREEZE
    return [], RoutingHoldReason.NO_ELIGIBLE_LANE


def _score_candidate_lanes(task: TaskContract, lanes: list[WorkerLane]) -> list[_ScoredLane]:
    task_class = task.type.value if hasattr(task.type, "value") else str(task.type)
    toolchain = _infer_toolchain(task)
    coupling_density = _estimate_coupling_density(task)

    scored: list[_ScoredLane] = []
    for lane in lanes:
        capability = lane.capabilities
        effective_pass_rate = capability.pass_rate * capability.false_success_penalty_multiplier
        task_class_strength = capability.task_class_strengths.get(task_class, 0.5)
        toolchain_strength = capability.toolchain_strengths.get(toolchain, 0.5) if toolchain else 0.5
        load_penalty = lane.active_workers / max(lane.concurrency_cap, 1)
        utility = (
            (W_PASS * effective_pass_rate)
            + (W_TASK_CLASS * task_class_strength)
            + (W_TOOLCHAIN * toolchain_strength)
            - (W_COST * capability.cost_per_success_usd)
            - (W_ESCALATION * capability.escalation_rate * coupling_density)
            - (W_LOAD * load_penalty)
        )
        scored.append(_ScoredLane(lane=lane, utility=utility))
    scored.sort(key=lambda item: item.utility, reverse=True)
    return scored


def route_task_shadow(
    task: TaskContract,
    rig_plan_payloads: list[dict[str, Any]] | None,
    capability_snapshot: Mapping[str, CapabilityVector],
) -> ShadowRoutingDecision | None:
    if not rig_plan_payloads:
        return None

    actual_stream = _normalize_stream_name(task.stream.value if hasattr(task.stream, "value") else str(task.stream))
    actual_model_tier = task.model_tier.value if hasattr(task.model_tier, "value") else str(task.model_tier)
    actual_lane_key = _build_actual_lane_key(task)
    lanes = build_shadow_lanes(rig_plan_payloads, capability_snapshot)
    candidates, hold_reason = _filter_candidate_lanes(task, lanes)

    if not candidates:
        reason = (
            "Task is currently blocked on prerequisite settlement."
            if hold_reason == RoutingHoldReason.DEPENDENCY_BLOCKED
            else "Task contract is marked for replan and should not be lane-routed."
            if hold_reason == RoutingHoldReason.REPLAN_REQUIRED
            else "No lane cleared the economic envelope for this task."
            if hold_reason == RoutingHoldReason.ECONOMIC_FREEZE
            else "No lane matched the strict task class/toolchain/mutable-scope filters."
        )
        return ShadowRoutingDecision(
            task_id=task.id,
            trace_id=task.trace_id,
            actual_lane_key=actual_lane_key,
            actual_stream=actual_stream,
            actual_model_tier=actual_model_tier,
            recommended_lane_key=None,
            recommended_stream=None,
            recommended_model_tier=None,
            matched=False,
            hold_reason=hold_reason,
            candidate_count=0,
            utility_score=None,
            reason=reason,
        )

    scored = _score_candidate_lanes(task, candidates)
    best = scored[0]
    recommended_stream = _normalize_stream_name(best.lane.lane_key.split(":")[1] if ":" in best.lane.lane_key else "feature")
    matched = recommended_stream == actual_stream and best.lane.model_tier == actual_model_tier
    if best.utility < MIN_SHADOW_UTILITY:
        return ShadowRoutingDecision(
            task_id=task.id,
            trace_id=task.trace_id,
            actual_lane_key=actual_lane_key,
            actual_stream=actual_stream,
            actual_model_tier=actual_model_tier,
            recommended_lane_key=None,
            recommended_stream=None,
            recommended_model_tier=None,
            matched=False,
            hold_reason=RoutingHoldReason.LOW_UTILITY_HOLD,
            candidate_count=len(candidates),
            utility_score=best.utility,
            reason="Candidate lanes exist, but historical utility is below the safe routing threshold.",
        )

    return ShadowRoutingDecision(
        task_id=task.id,
        trace_id=task.trace_id,
        actual_lane_key=actual_lane_key,
        actual_stream=actual_stream,
        actual_model_tier=actual_model_tier,
        recommended_lane_key=best.lane.lane_key,
        recommended_stream=recommended_stream,
        recommended_model_tier=best.lane.model_tier,
        matched=matched,
        hold_reason=None,
        candidate_count=len(candidates),
        utility_score=best.utility,
        reason=(
            f"Shadow scheduler prefers {recommended_stream}/{best.lane.model_tier} based on lane evidence "
            f"and task fit; {'matches' if matched else 'differs from'} the live assignment."
        ),
    )
