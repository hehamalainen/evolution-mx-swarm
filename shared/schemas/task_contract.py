"""TaskContract schema - the core message payload for workers."""

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum


class TaskType(str, Enum):
    FEATURE = "feature"
    BUGFIX = "bugfix"
    TEST = "test"
    REFACTOR = "refactor"
    OPS = "ops"
    DOC = "doc"
    SECURITY = "security"
    INTEGRATION = "integration"


class MergePolicy(str, Enum):
    MANUAL = "manual"
    AUTO_ON_PASS = "auto_on_pass"
    REQUIRE_REVIEW = "require_review"
    AUTO_WITH_FEATURE_FLAG = "auto_with_feature_flag"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RiskTier(int, Enum):
    """Risk tier for TEAMPLAN merge policy."""
    TIER_0 = 0  # Low risk - auto merge on green
    TIER_1 = 1  # Medium risk - auto merge with feature flag
    TIER_2 = 2  # High risk - manual review required


class TaskStream(str, Enum):
    """Stream assignment for task routing."""
    FEATURE = "feature"
    QA = "qa"
    OPS = "ops"
    SECURITY = "security"
    DOCS = "docs"
    INTEGRATION = "integration"


class ModelTier(str, Enum):
    """Model pool tier used for routing and qualification."""

    LOCAL_OPEN_WEIGHT = "local_open_weight"
    FRONTIER = "frontier"


class RoutingTarget(str, Enum):
    """Routing destination chosen by deterministic control logic."""

    LOCAL = "local"
    FRONTIER = "frontier"
    REPLAN = "replan"
    HUMAN_REVIEW = "human_review"


DEFAULT_STREAM_TOOLS = {
    TaskStream.FEATURE: ["read", "write", "bash", "grep", "ls"],
    TaskStream.QA: ["read", "bash", "grep", "pytest", "ls"],
    TaskStream.OPS: ["read", "write", "bash", "grep", "docker", "ls"],
    TaskStream.SECURITY: ["read", "bash", "grep", "audit", "ls"],
    TaskStream.DOCS: ["read", "write", "grep", "ls"],
    TaskStream.INTEGRATION: ["read", "write", "bash", "grep", "git", "ls"],
}


def _default_shard_key(stream: TaskStream, task_type: TaskType, model_tier: ModelTier) -> str:
    """Derive a stable shard key for staged queue partitioning."""
    return f"{stream.value}.{task_type.value}.{model_tier.value}"


class TaskState(str, Enum):
    """Authoritative task lifecycle states for queue settlement integrity."""

    QUEUED = "queued"
    BLOCKED = "blocked"
    CLAIMED = "claimed"
    EXECUTING = "executing"
    ARTIFACT_READY = "artifact_ready"
    VERIFIED = "verified"
    SUBMITTED_PENDING_LEDGER = "submitted_pending_ledger"
    SETTLED_ACK = "settled_ack"
    SETTLED_NACK = "settled_nack"
    DEAD_LETTER = "dead_letter"


class NackReason(str, Enum):
    """Machine-readable failure taxonomy used by queue settlement and telemetry."""

    ERR_PREREQUISITE_FAILED = "ERR_PREREQUISITE_FAILED"
    ERR_SYNTAX = "ERR_SYNTAX"
    ERR_NO_DIFF = "ERR_NO_DIFF"
    ERR_REFUSAL = "ERR_REFUSAL"
    ERR_CHECK_FAIL = "ERR_CHECK_FAIL"
    ERR_RATE_LIMIT = "ERR_RATE_LIMIT"
    ERR_QUEUE_LEASE = "ERR_QUEUE_LEASE"
    ERR_LEDGER_UNAVAILABLE = "ERR_LEDGER_UNAVAILABLE"
    ERR_POST_LANDING_VERIFY = "ERR_POST_LANDING_VERIFY"
    ERR_TASK_FAILED = "ERR_TASK_FAILED"
    ERR_RECON_DIVERGENCE = "ERR_RECON_DIVERGENCE"
    ERR_BARRIER_TIMEOUT = "ERR_BARRIER_TIMEOUT"
    ERR_BARRIER_ABORTED = "ERR_BARRIER_ABORTED"


@dataclass
class TaskContext:
    """Context provided to worker for task execution."""
    prd_refs: list[str] = field(default_factory=list)  # References to PRD sections
    files_hint: list[str] = field(default_factory=list)  # Suggested files to modify
    do_not_touch: list[str] = field(default_factory=list)  # Files to avoid
    tools: list[str] = field(default_factory=list)  # Whitelist of allowed tools


@dataclass
class AcceptanceCriteria:
    """Acceptance criteria for task completion."""
    checks: list[str] = field(default_factory=list)  # Commands to run (must pass)
    criteria: list[str] = field(default_factory=list)  # Human-readable criteria


@dataclass
class TaskConstraints:
    """Constraints and policies for task execution."""
    timebox_minutes: int = 60
    risk: RiskLevel = RiskLevel.MEDIUM
    merge_policy: MergePolicy = MergePolicy.REQUIRE_REVIEW
    retry_max: int = 3


@dataclass
class TaskContract:
    """
    The core work unit in MX Swarm.

    Workers only execute TaskContracts. Each contract defines:
    - What to do (goal)
    - How to verify (acceptance)
    - Constraints (timebox, risk, retry policy)
    - Routing (stream, tier, spec_ref)
    """
    id: str
    type: TaskType
    goal: str  # One sentence objective
    context: TaskContext
    acceptance: AcceptanceCriteria
    constraints: TaskConstraints

    # Metadata
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    priority: int = 5  # 1-10, higher is more urgent
    parent_task_id: str | None = None  # For subtasks
    spec_requirement_ids: list[str] = field(default_factory=list)  # Links to SPEC.json

    # TEAMPLAN routing fields (required for Org Builder)
    stream: TaskStream = TaskStream.FEATURE  # Which worker pool handles this
    tier: RiskTier = RiskTier.TIER_1         # Risk tier (0/1/2) for merge policy
    spec_ref: str | None = None           # SPEC reference e.g. "RG-001"
    rig_id: str = "default"
    model_tier: ModelTier = ModelTier.LOCAL_OPEN_WEIGHT
    shard_key: str | None = None
    routing_target: RoutingTarget = RoutingTarget.LOCAL
    routing_reason: str | None = None
    escalation_reason: str | None = None
    estimated_cost_bucket: str = "low"
    frontier_eligible: bool = False
    frontier_attempt_count: int = 0
    prerequisite_requirement_ids: list[str] = field(default_factory=list)
    prerequisite_task_ids: list[str] = field(default_factory=list)

    # Execution state (updated by worker)
    trace_id: str = field(default_factory=lambda: f"trace-{uuid.uuid4().hex[:16]}")
    attempt_id: str = "attempt-0"
    intent_anchor_id: str = field(default_factory=lambda: f"intent-{uuid.uuid4().hex[:12]}")
    state: TaskState = TaskState.QUEUED
    attempt_count: int = 0
    last_error: str | None = None
    previous_failure_reason: NackReason | None = None
    previous_failure_detail: str | None = None
    previous_attempt_id: str | None = None
    previous_attempt_number: int | None = None
    assigned_worker: str | None = None
    submission_ledger_id: str | None = None
    settlement_reason: NackReason | None = None

    # Contention/Cohort metadata fields
    contention_mode: bool | None = None
    contention_group_id: str | None = None
    contention_policy: str | None = None
    contention_target_paths: list[str] | None = None
    contention_base_head_id: str | None = None
    contention_expected_tasks: list[str] | None = None
    defer_evaluation: bool | None = None

    @classmethod
    def create(
        cls,
        task_type: TaskType,
        goal: str,
        context: TaskContext | None = None,
        acceptance: AcceptanceCriteria | None = None,
        constraints: TaskConstraints | None = None,
        priority: int = 5,
        parent_task_id: str | None = None,
        spec_requirement_ids: list[str] | None = None,
        stream: TaskStream = TaskStream.FEATURE,
        tier: RiskTier = RiskTier.TIER_1,
        spec_ref: str | None = None,
        rig_id: str = "default",
        model_tier: ModelTier = ModelTier.LOCAL_OPEN_WEIGHT,
        shard_key: str | None = None,
        routing_target: RoutingTarget = RoutingTarget.LOCAL,
        routing_reason: str | None = None,
        escalation_reason: str | None = None,
        estimated_cost_bucket: str = "low",
        frontier_eligible: bool = False,
        frontier_attempt_count: int = 0,
        prerequisite_requirement_ids: list[str] | None = None,
        prerequisite_task_ids: list[str] | None = None,
        contention_mode: bool | None = None,
        contention_group_id: str | None = None,
        contention_policy: str | None = None,
        contention_target_paths: list[str] | None = None,
        contention_base_head_id: str | None = None,
        contention_expected_tasks: list[str] | None = None,
        defer_evaluation: bool | None = None,
    ) -> "TaskContract":
        """Factory method to create a new TaskContract."""
        ctx = context or TaskContext()
        # Auto-populate tools from stream defaults if none provided
        if not ctx.tools:
            ctx.tools = DEFAULT_STREAM_TOOLS.get(stream, ["read", "write", "bash", "grep", "ls"])

        return cls(
            id=str(uuid.uuid4())[:8],
            type=task_type,
            goal=goal,
            context=ctx,
            acceptance=acceptance or AcceptanceCriteria(),
            constraints=constraints or TaskConstraints(),
            priority=priority,
            parent_task_id=parent_task_id,
            spec_requirement_ids=spec_requirement_ids or [],
            stream=stream,
            tier=tier,
            spec_ref=spec_ref,
            rig_id=rig_id,
            model_tier=model_tier,
            shard_key=shard_key or _default_shard_key(stream, task_type, model_tier),
            routing_target=routing_target,
            routing_reason=routing_reason,
            escalation_reason=escalation_reason,
            estimated_cost_bucket=estimated_cost_bucket,
            frontier_eligible=frontier_eligible,
            frontier_attempt_count=frontier_attempt_count,
            prerequisite_requirement_ids=prerequisite_requirement_ids or [],
            prerequisite_task_ids=prerequisite_task_ids or [],
            contention_mode=contention_mode,
            contention_group_id=contention_group_id,
            contention_policy=contention_policy,
            contention_target_paths=contention_target_paths,
            contention_base_head_id=contention_base_head_id,
            contention_expected_tasks=contention_expected_tasks,
            defer_evaluation=defer_evaluation,
        )

    @staticmethod
    def _valid_transitions() -> dict[TaskState, set[TaskState]]:
        """Allowed state transitions for deterministic settlement integrity."""
        return {
            TaskState.QUEUED: {TaskState.BLOCKED, TaskState.CLAIMED},
            TaskState.BLOCKED: {TaskState.QUEUED, TaskState.CLAIMED, TaskState.SETTLED_NACK, TaskState.DEAD_LETTER},
            TaskState.CLAIMED: {TaskState.EXECUTING, TaskState.SETTLED_NACK, TaskState.DEAD_LETTER},
            TaskState.EXECUTING: {TaskState.ARTIFACT_READY, TaskState.SETTLED_NACK, TaskState.DEAD_LETTER},
            TaskState.ARTIFACT_READY: {TaskState.VERIFIED, TaskState.SETTLED_NACK, TaskState.DEAD_LETTER},
            TaskState.VERIFIED: {
                TaskState.SUBMITTED_PENDING_LEDGER,
                TaskState.SETTLED_ACK,
                TaskState.SETTLED_NACK,
                TaskState.DEAD_LETTER,
            },
            TaskState.SUBMITTED_PENDING_LEDGER: {TaskState.SETTLED_ACK, TaskState.SETTLED_NACK, TaskState.DEAD_LETTER},
            TaskState.SETTLED_ACK: set(),
            TaskState.SETTLED_NACK: set(),
            TaskState.DEAD_LETTER: set(),
        }

    def can_transition_to(self, next_state: TaskState) -> bool:
        """Check whether lifecycle transition is valid."""
        return next_state in self._valid_transitions().get(self.state, set())

    def transition_to(self, next_state: TaskState) -> None:
        """Transition to a new lifecycle state with invariant validation."""
        if not self.can_transition_to(next_state):
            raise ValueError(
                f"Invalid task state transition for {self.id}: {self.state.value} -> {next_state.value}"
            )
        self.state = next_state

    def to_dict(self) -> dict:
        """Serialize to dictionary."""
        return {
            "id": self.id,
            "type": self.type.value,
            "goal": self.goal,
            "context": {
                "prd_refs": self.context.prd_refs,
                "files_hint": self.context.files_hint,
                "do_not_touch": self.context.do_not_touch,
                "tools": self.context.tools,
            },
            "acceptance": {
                "checks": self.acceptance.checks,
                "criteria": self.acceptance.criteria,
            },
            "constraints": {
                "timebox_minutes": self.constraints.timebox_minutes,
                "risk": self.constraints.risk.value,
                "merge_policy": self.constraints.merge_policy.value,
                "retry_max": self.constraints.retry_max,
            },
            "created_at": self.created_at,
            "priority": self.priority,
            "parent_task_id": self.parent_task_id,
            "spec_requirement_ids": self.spec_requirement_ids,
            # TEAMPLAN routing fields
            "stream": self.stream.value,
            "tier": self.tier.value,
            "spec_ref": self.spec_ref,
            "rig_id": self.rig_id,
            "model_tier": self.model_tier.value,
            "shard_key": self.shard_key,
            "routing_target": self.routing_target.value,
            "routing_reason": self.routing_reason,
            "escalation_reason": self.escalation_reason,
            "estimated_cost_bucket": self.estimated_cost_bucket,
            "frontier_eligible": self.frontier_eligible,
            "frontier_attempt_count": self.frontier_attempt_count,
            "prerequisite_requirement_ids": self.prerequisite_requirement_ids,
            "prerequisite_task_ids": self.prerequisite_task_ids,
            # Execution state
            "trace_id": self.trace_id,
            "attempt_id": self.attempt_id,
            "intent_anchor_id": self.intent_anchor_id,
            "state": self.state.value,
            "attempt_count": self.attempt_count,
            "last_error": self.last_error,
            "previous_failure_reason": (
                self.previous_failure_reason.value if self.previous_failure_reason else None
            ),
            "previous_failure_detail": self.previous_failure_detail,
            "previous_attempt_id": self.previous_attempt_id,
            "previous_attempt_number": self.previous_attempt_number,
            "assigned_worker": self.assigned_worker,
            "submission_ledger_id": self.submission_ledger_id,
            "settlement_reason": self.settlement_reason.value if self.settlement_reason else None,
            "contention_mode": self.contention_mode,
            "contention_group_id": self.contention_group_id,
            "contention_policy": self.contention_policy,
            "contention_target_paths": self.contention_target_paths,
            "contention_base_head_id": self.contention_base_head_id,
            "contention_expected_tasks": self.contention_expected_tasks,
            "defer_evaluation": self.defer_evaluation,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TaskContract":
        """Deserialize from dictionary."""
        # Parse stream with backward compatibility
        stream_val = data.get("stream", "feature")
        try:
            stream = TaskStream(stream_val)
        except ValueError:
            stream = TaskStream.FEATURE

        # Parse tier with backward compatibility
        tier_val = data.get("tier", 1)
        try:
            tier = RiskTier(tier_val)
        except ValueError:
            tier = RiskTier.TIER_1

        state_val = data.get("state", TaskState.QUEUED.value)
        try:
            state = TaskState(state_val)
        except ValueError:
            state = TaskState.QUEUED

        settlement_reason_val = data.get("settlement_reason")
        settlement_reason: NackReason | None = None
        if isinstance(settlement_reason_val, str):
            try:
                settlement_reason = NackReason(settlement_reason_val)
            except ValueError:
                settlement_reason = None

        model_tier_val = data.get("model_tier", ModelTier.LOCAL_OPEN_WEIGHT.value)
        try:
            model_tier = ModelTier(model_tier_val)
        except ValueError:
            model_tier = ModelTier.LOCAL_OPEN_WEIGHT

        routing_target_val = data.get("routing_target", RoutingTarget.LOCAL.value)
        try:
            routing_target = RoutingTarget(routing_target_val)
        except ValueError:
            routing_target = RoutingTarget.LOCAL

        previous_failure_reason_val = data.get("previous_failure_reason")
        previous_failure_reason: NackReason | None = None
        if isinstance(previous_failure_reason_val, str):
            try:
                previous_failure_reason = NackReason(previous_failure_reason_val)
            except ValueError:
                previous_failure_reason = None

        return cls(
            id=data["id"],
            type=TaskType(data["type"]),
            goal=data["goal"],
            context=TaskContext(
                prd_refs=data.get("context", {}).get("prd_refs", []),
                files_hint=data.get("context", {}).get("files_hint", []),
                do_not_touch=data.get("context", {}).get("do_not_touch", []),
                tools=data.get("context", {}).get("tools", []),
            ),
            acceptance=AcceptanceCriteria(
                checks=data.get("acceptance", {}).get("checks", []),
                criteria=data.get("acceptance", {}).get("criteria", []),
            ),
            constraints=TaskConstraints(
                timebox_minutes=data.get("constraints", {}).get("timebox_minutes", 60),
                risk=RiskLevel(data.get("constraints", {}).get("risk", "medium")),
                merge_policy=MergePolicy(data.get("constraints", {}).get("merge_policy", "require_review")),
                retry_max=data.get("constraints", {}).get("retry_max", 3),
            ),
            created_at=data.get("created_at", datetime.now(UTC).isoformat()),
            priority=data.get("priority", 5),
            parent_task_id=data.get("parent_task_id"),
            spec_requirement_ids=data.get("spec_requirement_ids", []),
            stream=stream,
            tier=tier,
            spec_ref=data.get("spec_ref"),
            rig_id=data.get("rig_id", "default"),
            model_tier=model_tier,
            shard_key=data.get("shard_key") or _default_shard_key(stream, TaskType(data["type"]), model_tier),
            routing_target=routing_target,
            routing_reason=data.get("routing_reason"),
            escalation_reason=data.get("escalation_reason"),
            estimated_cost_bucket=str(data.get("estimated_cost_bucket", "low")),
            frontier_eligible=bool(data.get("frontier_eligible", False)),
            frontier_attempt_count=int(data.get("frontier_attempt_count", 0)),
            prerequisite_requirement_ids=list(data.get("prerequisite_requirement_ids", [])),
            prerequisite_task_ids=list(data.get("prerequisite_task_ids", [])),
            trace_id=data.get("trace_id", f"trace-{uuid.uuid4().hex[:16]}"),
            attempt_id=data.get(
                "attempt_id",
                f"attempt-{int(data.get('attempt_count', 0))}",
            ),
            intent_anchor_id=data.get("intent_anchor_id", f"intent-{uuid.uuid4().hex[:12]}"),
            state=state,
            attempt_count=data.get("attempt_count", 0),
            last_error=data.get("last_error"),
            previous_failure_reason=previous_failure_reason,
            previous_failure_detail=data.get("previous_failure_detail"),
            previous_attempt_id=data.get("previous_attempt_id"),
            previous_attempt_number=data.get("previous_attempt_number"),
            assigned_worker=data.get("assigned_worker"),
            submission_ledger_id=data.get("submission_ledger_id"),
            settlement_reason=settlement_reason,
            contention_mode=data.get("contention_mode"),
            contention_group_id=data.get("contention_group_id"),
            contention_policy=data.get("contention_policy"),
            contention_target_paths=data.get("contention_target_paths"),
            contention_base_head_id=data.get("contention_base_head_id"),
            contention_expected_tasks=data.get("contention_expected_tasks"),
            defer_evaluation=data.get("defer_evaluation"),
        )

    def to_json(self) -> str:
        """Serialize to JSON string."""
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_json(cls, json_str: str) -> "TaskContract":
        """Deserialize from JSON string."""
        return cls.from_dict(json.loads(json_str))

    def branch_name(self) -> str:
        """Generate branch name for this task."""
        slug = self.goal[:30].lower().replace(" ", "-").replace("/", "-")
        slug = "".join(c for c in slug if c.isalnum() or c == "-")
        return f"task/{self.id}-{slug}"
