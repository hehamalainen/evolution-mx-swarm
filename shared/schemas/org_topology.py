"""Organization topology contracts for MX Swarm.

Dataclasses are used on the execution hot path.
Pydantic models are reserved for compiler/boundary plans.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

from pydantic import BaseModel, Field


class RoutingHoldReason(str, Enum):
    DEPENDENCY_BLOCKED = "DEPENDENCY_BLOCKED"
    NO_ELIGIBLE_LANE = "NO_ELIGIBLE_LANE"
    ECONOMIC_FREEZE = "ECONOMIC_FREEZE"
    LOW_UTILITY_HOLD = "LOW_UTILITY_HOLD"
    REPLAN_REQUIRED = "REPLAN_REQUIRED"


class LaneStatus(str, Enum):
    ACTIVE = "ACTIVE"
    FROZEN = "FROZEN"
    THROTTLED = "THROTTLED"
    DRAINING = "DRAINING"


@dataclass(slots=True)
class CapabilityVector:
    """Strictly event-sourced evidence for a stable execution lane."""

    pass_rate: float = 0.5
    cost_per_success_usd: float = 0.0
    settlement_latency_ema_s: float = 0.0
    escalation_rate: float = 0.0
    transient_failure_rate: float = 0.0
    false_success_penalty_multiplier: float = 1.0
    task_class_strengths: dict[str, float] = field(default_factory=dict)
    toolchain_strengths: dict[str, float] = field(default_factory=dict)
    last_updated_ts: float = field(default_factory=time.time)


@dataclass(slots=True)
class WorkerLane:
    """Stable composite execution identity for lane-level learning."""

    lane_key: str
    model_tier: str
    target_task_classes: set[str]
    supported_toolchains: set[str]
    max_mutable_files: int
    capabilities: CapabilityVector
    status: LaneStatus = LaneStatus.ACTIVE
    concurrency_cap: int = 1
    active_workers: int = 0
    cost_envelope_usd: float = 0.0
    current_spend_usd: float = 0.0


@dataclass(slots=True)
class Shard:
    shard_id: str
    lanes: dict[str, WorkerLane]
    concurrency_cap: int
    freeze_condition_threshold: int
    status: LaneStatus = LaneStatus.ACTIVE


@dataclass(slots=True)
class Stream:
    stream_id: str
    stream_type: str
    shards: dict[str, Shard]
    qa_density_ratio: float


@dataclass(slots=True)
class Rig:
    rig_id: str
    goal_cluster_node_ids: set[str]
    streams: dict[str, Stream]
    frontier_budget_cap_usd: float
    current_spend_usd: float = 0.0
    governance_owner_id: str = "governor"
    status: LaneStatus = LaneStatus.ACTIVE


def build_lane_key(
    *,
    rig_id: str,
    stream_id: str,
    shard_id: str,
    model_tier: str,
    provider_model: str,
) -> str:
    """Build a stable lane key used by the capability registry."""
    normalized = [
        str(rig_id or "default").strip() or "default",
        str(stream_id or "feature").strip() or "feature",
        str(shard_id or "default").strip() or "default",
        str(model_tier or "local_open_weight").strip() or "local_open_weight",
        str(provider_model or "unknown").strip() or "unknown",
    ]
    return ":".join(normalized)


class ShardPlan(BaseModel):
    shard_id: str
    target_task_classes: list[str] = Field(default_factory=list)
    supported_toolchains: list[str] = Field(default_factory=list)
    max_mutable_files: int = 1
    concurrency_cap: int = 1
    freeze_condition_threshold: int = 3
    allowed_model_tiers: list[str] = Field(default_factory=list)


class StreamPlan(BaseModel):
    stream_id: str
    stream_type: str
    qa_density_ratio: float = 0.0
    shards: list[ShardPlan] = Field(default_factory=list)


class RigPlan(BaseModel):
    rig_id: str
    goal_cluster_node_ids: list[str] = Field(default_factory=list)
    frontier_budget_cap_usd: float = 0.0
    governance_owner_id: str
    streams: list[StreamPlan] = Field(default_factory=list)
