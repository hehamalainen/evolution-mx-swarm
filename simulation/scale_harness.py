"""Scaling harnesses for MX control-plane and worker-load experiments."""

from __future__ import annotations

import asyncio
import json
import math
import random
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from shared.schemas import (
    AcceptanceCriteria,
    CapabilityVector,
    ModelTier,
    ScaleCapabilityDelta,
    ScaleFailureVerdict,
    ScaleMetricsSummary,
    ScaleRunManifest,
    ScaleRunReport,
    ScaleScenarioConfig,
    TaskConstraints,
    TaskContext,
    TaskContract,
    TaskState,
    TaskStream,
    TaskType,
    build_lane_key,
)
from shared.utils.routing import build_shadow_lanes, route_task_shadow_with_lanes
from shared.utils.shadow_governor import evaluate_shadow_governor

EMA_ALPHA = 0.15
SECONDS_PER_TICK = 5.0


@dataclass(slots=True)
class LaneBehavior:
    kind: str
    success_rate: float
    transient_failure_rate: float
    escalation_rate: float
    latency_ticks_range: tuple[int, int]
    cost_range: tuple[float, float]


@dataclass(slots=True)
class SimulatedTask:
    contract: TaskContract
    state: TaskState
    dependency_ids: list[str]
    hold_count: int = 0
    started_tick: int | None = None
    finish_tick: int | None = None
    lane_key: str | None = None
    outcome: str | None = None
    terminal_reason: str | None = None
    settlement_latency_ticks: int | None = None


def _behavior_profiles() -> dict[str, LaneBehavior]:
    return {
        "deterministic": LaneBehavior(
            kind="deterministic",
            success_rate=0.97,
            transient_failure_rate=0.01,
            escalation_rate=0.01,
            latency_ticks_range=(1, 2),
            cost_range=(0.02, 0.05),
        ),
        "noise": LaneBehavior(
            kind="noise",
            success_rate=0.68,
            transient_failure_rate=0.16,
            escalation_rate=0.08,
            latency_ticks_range=(2, 5),
            cost_range=(0.04, 0.09),
        ),
        "sampled": LaneBehavior(
            kind="sampled",
            success_rate=0.9,
            transient_failure_rate=0.03,
            escalation_rate=0.04,
            latency_ticks_range=(4, 8),
            cost_range=(0.08, 0.18),
        ),
    }


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def build_scale_rig_plans(
    *,
    logical_lanes: int,
    rig_count: int = 20,
    toolchain: str = "python",
) -> list[dict[str, Any]]:
    """Generate deterministic rig plans sized to the requested logical lane count."""
    streams_per_rig = 2
    total_streams = rig_count * streams_per_rig
    shards_per_stream = max(1, math.ceil(logical_lanes / total_streams))

    rig_plans: list[dict[str, Any]] = []
    for rig_index in range(rig_count):
        rig_id = f"rig-{rig_index:02d}"
        streams = []
        for stream_name, stream_type, task_class in (
            ("feature", "execution", "feature"),
            ("qa", "qa_verification", "qa"),
        ):
            shards = []
            for shard_index in range(shards_per_stream):
                shards.append(
                    {
                        "shard_id": f"{stream_name}.{shard_index:02d}",
                        "target_task_classes": [task_class],
                        "supported_toolchains": [toolchain],
                        "max_mutable_files": 3 if stream_name == "feature" else 2,
                        "concurrency_cap": 1,
                        "freeze_condition_threshold": 3,
                        "allowed_model_tiers": [ModelTier.LOCAL_OPEN_WEIGHT.value],
                    }
                )
            streams.append(
                {
                    "stream_id": f"{rig_id}-{stream_name}",
                    "stream_type": stream_type,
                    "qa_density_ratio": 0.3 if stream_name == "qa" else 0.15,
                    "shards": shards,
                }
            )
        rig_plans.append(
            {
                "rig_id": rig_id,
                "goal_cluster_node_ids": [f"goal-{rig_index:02d}"],
                "frontier_budget_cap_usd": 5.0,
                "governance_owner_id": f"governor:{rig_id}",
                "streams": streams,
            }
        )
    return rig_plans


def _seed_capability_snapshot(
    lanes: list[Any],
    rng: random.Random,
    *,
    deterministic_pct: float,
    noise_pct: float,
) -> tuple[dict[str, CapabilityVector], dict[str, LaneBehavior]]:
    profiles = _behavior_profiles()
    snapshot: dict[str, CapabilityVector] = {}
    behavior_by_lane: dict[str, LaneBehavior] = {}

    lane_keys = [lane.lane_key for lane in lanes]
    lane_count = len(lane_keys)
    deterministic_count = int(lane_count * deterministic_pct)
    noise_count = int(lane_count * noise_pct)

    for index, lane_key in enumerate(lane_keys):
        if index < deterministic_count:
            behavior = profiles["deterministic"]
        elif index < deterministic_count + noise_count:
            behavior = profiles["noise"]
        else:
            behavior = profiles["sampled"]
        behavior_by_lane[lane_key] = behavior

        parts = lane_key.split(":")
        snapshot[build_lane_key(
            rig_id=parts[0],
            stream_id=parts[1],
            shard_id=parts[2],
            model_tier=parts[3],
            provider_model="seed",
        )] = CapabilityVector(
            pass_rate=max(0.1, min(0.99, rng.uniform(behavior.success_rate - 0.05, behavior.success_rate + 0.03))),
            cost_per_success_usd=rng.uniform(*behavior.cost_range),
            settlement_latency_ema_s=rng.uniform(*behavior.latency_ticks_range) * SECONDS_PER_TICK,
            escalation_rate=max(0.0, behavior.escalation_rate + rng.uniform(-0.01, 0.02)),
            transient_failure_rate=max(0.0, behavior.transient_failure_rate + rng.uniform(-0.01, 0.02)),
            task_class_strengths={"feature": behavior.success_rate, "qa": max(0.4, behavior.success_rate - 0.08)},
            toolchain_strengths={"python": behavior.success_rate},
        )
    return snapshot, behavior_by_lane


def _task_stream(task_class: str) -> TaskStream:
    return TaskStream.QA if task_class == "qa" else TaskStream.FEATURE


def _task_type(task_class: str) -> TaskType:
    return TaskType.TEST if task_class == "qa" else TaskType.FEATURE


def _make_files_hint(task_class: str, index: int, *, invalid_contract: bool) -> list[str]:
    if invalid_contract:
        return [f"drivers/generated_{index:04d}.rs"]
    if task_class == "qa":
        return [f"tests/generated/test_feature_{index:04d}.py"]
    return [f"tools/generated/feature_{index:04d}.py"]


def _make_acceptance(task_class: str, index: int, *, invalid_contract: bool) -> AcceptanceCriteria:
    if invalid_contract:
        return AcceptanceCriteria(checks=[f"cargo test generated_case_{index:04d}"])
    if task_class == "qa":
        return AcceptanceCriteria(checks=[f"pytest tests/generated/test_feature_{index:04d}.py -q"])
    return AcceptanceCriteria(checks=[f"pytest tests/generated/test_feature_{index:04d}.py -q"])


def generate_synthetic_tasks(
    *,
    lanes: list[Any],
    logical_tasks: int,
    scenario: ScaleScenarioConfig,
    rng: random.Random,
) -> dict[str, SimulatedTask]:
    tasks: dict[str, SimulatedTask] = {}
    feature_lanes = [lane for lane in lanes if ":feature:" in lane.lane_key]
    qa_lanes = [lane for lane in lanes if ":qa:" in lane.lane_key]

    task_ids: list[str] = []
    for index in range(logical_tasks):
        task_class = "qa" if qa_lanes and rng.random() < 0.2 else "feature"
        lane = rng.choice(qa_lanes if task_class == "qa" else feature_lanes)
        invalid_contract = rng.random() < scenario.invalid_contract_pct
        dependency_count = 0
        if task_ids and rng.random() < 0.35:
            dependency_count = rng.randint(1, min(2, len(task_ids)))
        dependency_ids = rng.sample(task_ids, dependency_count) if dependency_count else []

        contract = TaskContract.create(
            task_type=_task_type(task_class),
            goal=f"Scale harness {task_class} task {index:04d}",
            context=TaskContext(files_hint=_make_files_hint(task_class, index, invalid_contract=invalid_contract)),
            acceptance=_make_acceptance(task_class, index, invalid_contract=invalid_contract),
            constraints=TaskConstraints(),
            stream=_task_stream(task_class),
            rig_id=lane.lane_key.split(":")[0],
            shard_key=lane.lane_key.split(":")[2],
            model_tier=ModelTier.LOCAL_OPEN_WEIGHT,
            prerequisite_task_ids=dependency_ids,
        )
        state = TaskState.BLOCKED if dependency_ids else TaskState.QUEUED
        tasks[contract.id] = SimulatedTask(
            contract=contract,
            state=state,
            dependency_ids=dependency_ids,
        )
        task_ids.append(contract.id)
    return tasks


def _apply_capability_update(
    capability: CapabilityVector,
    *,
    outcome: str,
    task_class: str,
    toolchain: str,
    latency_seconds: float,
    cost_usd: float,
) -> None:
    success = 1.0 if outcome == "success" else 0.0
    transient = 1.0 if outcome == "transient" else 0.0
    escalated = 1.0 if outcome == "escalated" else 0.0

    capability.pass_rate = ((1 - EMA_ALPHA) * capability.pass_rate) + (EMA_ALPHA * success)
    capability.escalation_rate = ((1 - EMA_ALPHA) * capability.escalation_rate) + (EMA_ALPHA * escalated)
    capability.transient_failure_rate = (
        ((1 - EMA_ALPHA) * capability.transient_failure_rate) + (EMA_ALPHA * transient)
    )
    if success:
        capability.cost_per_success_usd = (
            ((1 - EMA_ALPHA) * capability.cost_per_success_usd) + (EMA_ALPHA * cost_usd)
        )
        capability.settlement_latency_ema_s = (
            ((1 - EMA_ALPHA) * capability.settlement_latency_ema_s) + (EMA_ALPHA * latency_seconds)
        )
    current_task_strength = capability.task_class_strengths.get(task_class, 0.5)
    capability.task_class_strengths[task_class] = ((1 - EMA_ALPHA) * current_task_strength) + (EMA_ALPHA * success)
    current_toolchain_strength = capability.toolchain_strengths.get(toolchain, 0.5)
    capability.toolchain_strengths[toolchain] = ((1 - EMA_ALPHA) * current_toolchain_strength) + (EMA_ALPHA * success)
    capability.last_updated_ts = datetime.now(UTC).timestamp()


def _sample_outcome(
    behavior: LaneBehavior,
    scenario: ScaleScenarioConfig,
    rng: random.Random,
) -> str:
    roll = rng.random()
    if roll < behavior.success_rate:
        return "success"
    roll -= behavior.success_rate
    if roll < max(behavior.transient_failure_rate, scenario.transient_failure_pct):
        return "transient"
    roll -= max(behavior.transient_failure_rate, scenario.transient_failure_pct)
    if roll < max(behavior.escalation_rate, scenario.escalation_failure_pct):
        return "escalated"
    return "deterministic_failure"


def _reason_for_outcome(outcome: str) -> str | None:
    mapping = {
        "transient": "ERR_TASK_FAILED",
        "escalated": "ERR_REFUSAL",
        "deterministic_failure": "ERR_CHECK_FAIL",
    }
    return mapping.get(outcome)


def _compute_rig_telemetry(
    *,
    rig_plans: list[dict[str, Any]],
    lanes_by_key: dict[str, Any],
    tasks: dict[str, SimulatedTask],
    terminal_outcomes: dict[str, str],
    frontier_spend: Counter[str],
) -> list[dict[str, Any]]:
    task_count_by_rig: Counter[str] = Counter()
    blocked_by_rig: Counter[str] = Counter()
    terminal_by_rig: Counter[str] = Counter()
    acceptance_fail_by_rig: Counter[str] = Counter()
    false_success_by_rig: Counter[str] = Counter()

    for sim_task in tasks.values():
        rig_id = sim_task.contract.rig_id
        if sim_task.state not in {TaskState.SETTLED_ACK, TaskState.SETTLED_NACK, TaskState.DEAD_LETTER}:
            task_count_by_rig[rig_id] += 1
            if sim_task.state == TaskState.BLOCKED:
                blocked_by_rig[rig_id] += 1
        else:
            terminal_by_rig[rig_id] += 1
            if sim_task.terminal_reason == "ERR_CHECK_FAIL":
                acceptance_fail_by_rig[rig_id] += 1
            if terminal_outcomes.get(sim_task.contract.id) == "false_success":
                false_success_by_rig[rig_id] += 1

    telemetry: list[dict[str, Any]] = []
    for rig_plan in rig_plans:
        rig_id = str(rig_plan["rig_id"])
        rig_lanes = [lane for lane in lanes_by_key.values() if lane.lane_key.startswith(f"{rig_id}:")]
        width = sum(int(lane.concurrency_cap) for lane in rig_lanes)
        pass_rate = statistics.fmean(lane.capabilities.pass_rate for lane in rig_lanes) if rig_lanes else 0.5
        telemetry.append(
            {
                "rig_id": rig_id,
                "task_count": int(task_count_by_rig[rig_id]),
                "width": width,
                "pass_rate": pass_rate,
                "blocked_dependency_ratio": blocked_by_rig[rig_id] / max(task_count_by_rig[rig_id], 1),
                "acceptance_failure_rate": acceptance_fail_by_rig[rig_id] / max(terminal_by_rig[rig_id], 1),
                "false_success_rate": false_success_by_rig[rig_id] / max(terminal_by_rig[rig_id], 1),
                "frontier_spend_usd": float(frontier_spend[rig_id]),
                "frontier_budget_cap_usd": float(rig_plan.get("frontier_budget_cap_usd", 0.0)),
            }
        )
    return telemetry


def run_hybrid_control_plane_simulation(
    *,
    logical_lanes: int = 1000,
    logical_tasks: int | None = None,
    seed: int = 7,
) -> ScaleRunReport:
    wall_clock_started = time.perf_counter()
    rng = random.Random(seed)
    logical_tasks = logical_tasks or (logical_lanes * 2)
    manifest = ScaleRunManifest(
        run_id=f"scale-control-plane-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ').lower()}",
        phase="control_plane_hybrid",
        seed=seed,
        logical_lanes=logical_lanes,
        logical_workers=0,
        active_concurrency=0,
        sink_mode="simulated",
        model_mix={"local_open_weight": 1.0},
        topology={"rig_count": 20, "toolchain": "python"},
    )

    rig_plans = build_scale_rig_plans(logical_lanes=logical_lanes)
    seed_snapshot, _ = _seed_capability_snapshot(
        build_shadow_lanes(rig_plans, {}),
        rng,
        deterministic_pct=manifest.lane_mix.deterministic_pct,
        noise_pct=manifest.lane_mix.noise_pct,
    )
    lanes = build_shadow_lanes(rig_plans, seed_snapshot)
    lanes_by_key = {lane.lane_key: lane for lane in lanes}
    behavior_by_lane = {}
    for lane in lanes:
        parts = lane.lane_key.split(":")
        seed_key = build_lane_key(
            rig_id=parts[0],
            stream_id=parts[1],
            shard_id=parts[2],
            model_tier=parts[3],
            provider_model="seed",
        )
        capability = seed_snapshot[seed_key]
        if capability.pass_rate >= 0.9:
            behavior_by_lane[lane.lane_key] = _behavior_profiles()["deterministic"]
        elif capability.pass_rate >= 0.75:
            behavior_by_lane[lane.lane_key] = _behavior_profiles()["sampled"]
        else:
            behavior_by_lane[lane.lane_key] = _behavior_profiles()["noise"]

    tasks = generate_synthetic_tasks(
        lanes=lanes,
        logical_tasks=logical_tasks,
        scenario=manifest.scenario,
        rng=rng,
    )

    hold_reasons: Counter[str] = Counter()
    queue_depths: list[int] = []
    settlement_latencies: list[float] = []
    total_cost_usd = 0.0
    retry_cascade_count = 0
    frontier_spend: Counter[str] = Counter()
    terminal_counter: Counter[str] = Counter()
    governor_counter: Counter[str] = Counter()
    governor_status_history: dict[str, list[str]] = defaultdict(list)
    in_flight: set[str] = set()
    last_progress_tick = 0

    max_ticks = max(200, logical_tasks * 2)
    for tick in range(max_ticks):
        # Complete in-flight work
        completed = [
            sim_task
            for sim_task in tasks.values()
            if sim_task.finish_tick is not None and sim_task.finish_tick <= tick and sim_task.contract.id in in_flight
        ]
        for sim_task in completed:
            in_flight.discard(sim_task.contract.id)
            lane = lanes_by_key[sim_task.lane_key or ""]
            lane.active_workers = max(0, lane.active_workers - 1)
            outcome = sim_task.outcome or "deterministic_failure"
            reason = _reason_for_outcome(outcome)
            if outcome == "success":
                sim_task.state = TaskState.SETTLED_ACK
            elif outcome == "transient":
                sim_task.state = TaskState.SETTLED_NACK
            else:
                sim_task.state = TaskState.DEAD_LETTER
            sim_task.terminal_reason = reason
            task_class = "qa" if sim_task.contract.type == TaskType.TEST else "feature"
            toolchain = "python"
            latency_seconds = float(sim_task.settlement_latency_ticks or 1) * SECONDS_PER_TICK
            settlement_latencies.append(latency_seconds)
            cost_usd = rng.uniform(*behavior_by_lane[lane.lane_key].cost_range)
            total_cost_usd += cost_usd
            _apply_capability_update(
                lane.capabilities,
                outcome=outcome,
                task_class=task_class,
                toolchain=toolchain,
                latency_seconds=latency_seconds,
                cost_usd=cost_usd,
            )
            terminal_counter[sim_task.state.value] += 1
            last_progress_tick = tick
            if outcome == "escalated":
                retry_cascade_count += 1

        # Resolve blocked tasks
        for sim_task in tasks.values():
            if sim_task.state != TaskState.BLOCKED:
                continue
            prereq_states = [tasks[task_id].state for task_id in sim_task.dependency_ids if task_id in tasks]
            if prereq_states and all(state == TaskState.SETTLED_ACK for state in prereq_states):
                sim_task.state = TaskState.QUEUED
                last_progress_tick = tick
            elif any(state in {TaskState.SETTLED_NACK, TaskState.DEAD_LETTER} for state in prereq_states):
                sim_task.state = TaskState.DEAD_LETTER
                sim_task.terminal_reason = "ERR_PREREQUISITE_FAILED"
                terminal_counter[sim_task.state.value] += 1
                last_progress_tick = tick

        # Route queued tasks
        queued = [task for task in tasks.values() if task.state == TaskState.QUEUED and task.contract.id not in in_flight]
        rng.shuffle(queued)
        routed_this_tick = 0
        for sim_task in queued:
            decision = route_task_shadow_with_lanes(sim_task.contract, lanes)
            if decision.hold_reason is not None:
                hold_reasons[decision.hold_reason.value] += 1
                sim_task.hold_count += 1
                if decision.hold_reason.value == "DEPENDENCY_BLOCKED":
                    sim_task.state = TaskState.BLOCKED
                elif sim_task.hold_count >= 3:
                    sim_task.state = TaskState.DEAD_LETTER
                    sim_task.terminal_reason = decision.hold_reason.value
                    terminal_counter[sim_task.state.value] += 1
                continue

            lane = lanes_by_key[decision.recommended_lane_key or ""]
            if lane.active_workers >= lane.concurrency_cap:
                hold_reasons["NO_CAPACITY"] += 1
                continue

            behavior = behavior_by_lane[lane.lane_key]
            lane.active_workers += 1
            in_flight.add(sim_task.contract.id)
            sim_task.state = TaskState.EXECUTING
            sim_task.started_tick = tick
            sim_task.lane_key = lane.lane_key
            sim_task.outcome = _sample_outcome(behavior, manifest.scenario, rng)
            sim_task.settlement_latency_ticks = rng.randint(*behavior.latency_ticks_range)
            if rng.random() < manifest.scenario.settlement_delay_pct:
                sim_task.settlement_latency_ticks += 2
            sim_task.finish_tick = tick + sim_task.settlement_latency_ticks
            routed_this_tick += 1

        queue_depths.append(sum(1 for task in tasks.values() if task.state in {TaskState.QUEUED, TaskState.BLOCKED}))

        if tick % 5 == 0:
            telemetry = _compute_rig_telemetry(
                rig_plans=rig_plans,
                lanes_by_key=lanes_by_key,
                tasks=tasks,
                terminal_outcomes={task_id: task.outcome or "" for task_id, task in tasks.items()},
                frontier_spend=frontier_spend,
            )
            recommendations = evaluate_shadow_governor(telemetry, {"frontier_frozen": False})
            for rec in recommendations:
                governor_counter[rec.recommendation_type] += 1
                governor_status_history[rec.scope_id].append(rec.status)

        if all(task.state in {TaskState.SETTLED_ACK, TaskState.SETTLED_NACK, TaskState.DEAD_LETTER} for task in tasks.values()):
            break
        if routed_this_tick == 0 and not in_flight and (tick - last_progress_tick) > 15:
            break

    zombies = sum(1 for task in tasks.values() if task.state == TaskState.BLOCKED)
    verified_landings = sum(1 for task in tasks.values() if task.state == TaskState.SETTLED_ACK)
    terminal_tasks = sum(1 for task in tasks.values() if task.state in {TaskState.SETTLED_ACK, TaskState.SETTLED_NACK, TaskState.DEAD_LETTER})
    queue_p95 = statistics.quantiles(queue_depths, n=20)[18] if len(queue_depths) >= 20 else float(max(queue_depths or [0]))
    settle_p95 = (
        statistics.quantiles(settlement_latencies, n=20)[18]
        if len(settlement_latencies) >= 20
        else float(max(settlement_latencies or [0.0]))
    )
    oscillations = sum(
        1
        for statuses in governor_status_history.values()
        if len(statuses) >= 4 and len(set(statuses[-4:])) > 2
    )
    batch_wall_clock_seconds = round(time.perf_counter() - wall_clock_started, 6)
    capability_values = [lane.capabilities for lane in lanes]
    metrics = ScaleMetricsSummary(
        verified_landings=verified_landings,
        total_terminal_tasks=terminal_tasks,
        false_success_rate=0.0,
        blocked_dependency_ratio=sum(1 for task in tasks.values() if task.state == TaskState.BLOCKED) / max(len(tasks), 1),
        convergence_time_seconds=float((max((task.finish_tick or 0) for task in tasks.values()) + 1) * SECONDS_PER_TICK),
        batch_wall_clock_seconds=batch_wall_clock_seconds,
        throughput_tasks_per_minute=round(
            terminal_tasks / max(batch_wall_clock_seconds / 60.0, 1e-9),
            6,
        ),
        governor_interventions=sum(governor_counter.values()),
        cost_per_success_usd=total_cost_usd / max(verified_landings, 1),
        queue_depth_p95=float(queue_p95),
        sink_settlement_latency_p95_s=float(settle_p95),
        retry_cascade_count=retry_cascade_count,
        dlq_volume=terminal_counter[TaskState.DEAD_LETTER.value],
        duplicate_terminal_settlements=0,
        zombie_blocked_tasks=zombies,
        sink_truth_divergence_count=0,
        unstable_governor_oscillation_count=oscillations,
    )

    verdict_reasons: list[str] = []
    if metrics.false_success_rate > 0:
        verdict_reasons.append("false_success_detected")
    if zombies > 0:
        verdict_reasons.append("zombie_blocked_tasks_detected")
    if metrics.duplicate_terminal_settlements > 0:
        verdict_reasons.append("duplicate_terminal_settlements_detected")
    if oscillations > 0:
        verdict_reasons.append("governor_oscillation_detected")
    if terminal_tasks < len(tasks):
        verdict_reasons.append("run_failed_to_converge")

    report = ScaleRunReport(
        manifest=manifest,
        metrics=metrics,
        verdict=ScaleFailureVerdict(passed=not verdict_reasons, reasons=verdict_reasons),
        capability_delta=ScaleCapabilityDelta(
            lanes_observed=len(lanes),
            lanes_updated=len([cap for cap in capability_values if cap.last_updated_ts > 0]),
            avg_pass_rate=statistics.fmean(cap.pass_rate for cap in capability_values),
            avg_escalation_rate=statistics.fmean(cap.escalation_rate for cap in capability_values),
            avg_transient_failure_rate=statistics.fmean(cap.transient_failure_rate for cap in capability_values),
        ),
        hold_reason_distribution=dict(hold_reasons),
        latency_by_lane={
            lane.lane_key: round(lane.capabilities.settlement_latency_ema_s, 3)
            for lane in lanes[: min(25, len(lanes))]
        },
        queue_depth_distribution={
            "min": min(queue_depths or [0]),
            "p50": int(statistics.median(queue_depths)) if queue_depths else 0,
            "p95": int(queue_p95),
            "max": max(queue_depths or [0]),
        },
        governor_summary={
            "recommendation_counts": dict(governor_counter),
            "rigs_observed": len(rig_plans),
        },
        sink_summary={
            "mode": "simulated",
            "verified_landings": verified_landings,
            "terminal_tasks": terminal_tasks,
        },
        notes=[
            "Hybrid control-plane run uses real routing, dependency blocking, capability updates, and passive governor recommendations.",
            "Sampled lanes are seeded from empirical local-proof-like latency/cost envelopes, not live model calls.",
        ],
    )
    return report


async def run_isolated_worker_load(
    *,
    logical_workers: int,
    active_concurrency: int,
    slice_key: str,
    timeout_seconds: int,
    repo_root: Path,
    source_root: Path | None = None,
    sink_mode: str = "mock_github",
) -> ScaleRunReport:
    wall_clock_started = time.perf_counter()
    manifest = ScaleRunManifest(
        run_id=f"scale-worker-load-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ').lower()}",
        phase="worker_load_canned",
        seed=0,
        logical_lanes=0,
        logical_workers=logical_workers,
        active_concurrency=active_concurrency,
        sink_mode=sink_mode,
        model_mix={"canned_fixture": 1.0},
        topology={"slice": slice_key, "isolation_mode": "per-proof"},
    )

    report_root = repo_root / "reports" / "scale_worker_load" / manifest.run_id
    report_root.mkdir(parents=True, exist_ok=True)
    fixture_path = repo_root / "proof_fixtures" / "jsonpeek_canned_completion.json"
    worker_artifacts: list[str] = []
    verified_landings = 0
    failures: list[str] = []
    latencies: list[float] = []

    async def _run_one(index: int) -> None:
        nonlocal verified_landings
        run_id = f"{manifest.run_id}-w{index:03d}"
        worker_report_root = report_root / "workers" / run_id
        proof_script = "run_aether_live_proof.py" if sink_mode == "aether" else "run_mock_sink_live_proof.py"
        artifact_name = "aether_live_proof.json" if sink_mode == "aether" else "mock_sink_live_proof.json"
        cmd = [
            sys.executable,
            str(repo_root / "scripts" / proof_script),
            "--slice",
            slice_key,
            "--timeout-seconds",
            str(timeout_seconds),
            "--run-id",
            run_id,
            "--report-root",
            str(worker_report_root),
            "--canned-completion-file",
            str(fixture_path),
        ]
        if source_root is not None:
            cmd.extend(["--source-root", str(source_root)])
            cmd.append("--source-is-template-repo")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(repo_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            failures.append(f"{run_id}: exited {proc.returncode}")
            if stderr:
                failures.append(stderr.decode("utf-8", errors="replace").strip())
            return

        latest_dir = max(worker_report_root.iterdir(), key=lambda item: item.name)
        artifact = latest_dir / artifact_name
        worker_artifacts.append(str(artifact))
        payload = json.loads(artifact.read_text(encoding="utf-8"))
        if str(payload.get("post_landing_verify") or "") == "pass":
            verified_landings += 1
        checkpoints = payload.get("stage_checkpoints") or {}
        started = checkpoints.get("task_selected", {}).get("timestamp")
        finished = checkpoints.get("terminal_status_seen", {}).get("timestamp")
        if started and finished:
            start_dt = _parse_timestamp(str(started))
            finish_dt = _parse_timestamp(str(finished))
            if start_dt is not None and finish_dt is not None:
                latencies.append((finish_dt - start_dt).total_seconds())

    semaphore = asyncio.Semaphore(active_concurrency)

    async def _bounded(index: int) -> None:
        async with semaphore:
            await _run_one(index)

    await asyncio.gather(*[_bounded(index) for index in range(logical_workers)])
    batch_wall_clock_seconds = round(time.perf_counter() - wall_clock_started, 6)

    metrics = ScaleMetricsSummary(
        verified_landings=verified_landings,
        total_terminal_tasks=logical_workers,
        false_success_rate=0.0,
        blocked_dependency_ratio=0.0,
        convergence_time_seconds=max(latencies or [0.0]),
        batch_wall_clock_seconds=batch_wall_clock_seconds,
        throughput_tasks_per_minute=round(
            logical_workers / max(batch_wall_clock_seconds / 60.0, 1e-9),
            6,
        ),
        governor_interventions=0,
        cost_per_success_usd=0.0,
        queue_depth_p95=0.0,
        sink_settlement_latency_p95_s=(
            statistics.quantiles(latencies, n=20)[18] if len(latencies) >= 20 else max(latencies or [0.0])
        ),
        retry_cascade_count=0,
        dlq_volume=len(failures),
        duplicate_terminal_settlements=0,
        zombie_blocked_tasks=0,
        sink_truth_divergence_count=0,
        unstable_governor_oscillation_count=0,
    )
    report = ScaleRunReport(
        manifest=manifest,
        metrics=metrics,
        verdict=ScaleFailureVerdict(passed=not failures, reasons=failures),
        capability_delta=ScaleCapabilityDelta(),
        governor_summary={"mode": "not_applicable"},
        sink_summary={"mode": sink_mode, "verified_landings": verified_landings},
        artifacts={"worker_artifacts": json.dumps(worker_artifacts[:25])},
        notes=[
            "Worker load harness exercises real worker plumbing through isolated canned worker proofs.",
            "This first version stresses branch/workspace/sink plumbing rather than a shared in-memory queue.",
        ],
    )
    return report
