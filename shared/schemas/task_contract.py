"""TaskContract schema - the core message payload for workers."""

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
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


@dataclass
class TaskContext:
    """Context provided to worker for task execution."""
    prd_refs: list[str] = field(default_factory=list)  # References to PRD sections
    files_hint: list[str] = field(default_factory=list)  # Suggested files to modify
    do_not_touch: list[str] = field(default_factory=list)  # Files to avoid


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
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    priority: int = 5  # 1-10, higher is more urgent
    parent_task_id: str | None = None  # For subtasks
    spec_requirement_ids: list[str] = field(default_factory=list)  # Links to SPEC.json

    # TEAMPLAN routing fields (required for Org Builder)
    stream: TaskStream = TaskStream.FEATURE  # Which worker pool handles this
    tier: RiskTier = RiskTier.TIER_1         # Risk tier (0/1/2) for merge policy
    spec_ref: str | None = None           # SPEC reference e.g. "RG-001"

    # Execution state (updated by worker)
    attempt_count: int = 0
    last_error: str | None = None
    assigned_worker: str | None = None

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
    ) -> "TaskContract":
        """Factory method to create a new TaskContract."""
        return cls(
            id=str(uuid.uuid4())[:8],
            type=task_type,
            goal=goal,
            context=context or TaskContext(),
            acceptance=acceptance or AcceptanceCriteria(),
            constraints=constraints or TaskConstraints(),
            priority=priority,
            parent_task_id=parent_task_id,
            spec_requirement_ids=spec_requirement_ids or [],
            stream=stream,
            tier=tier,
            spec_ref=spec_ref,
        )

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
            # Execution state
            "attempt_count": self.attempt_count,
            "last_error": self.last_error,
            "assigned_worker": self.assigned_worker,
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

        return cls(
            id=data["id"],
            type=TaskType(data["type"]),
            goal=data["goal"],
            context=TaskContext(
                prd_refs=data.get("context", {}).get("prd_refs", []),
                files_hint=data.get("context", {}).get("files_hint", []),
                do_not_touch=data.get("context", {}).get("do_not_touch", []),
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
            created_at=data.get("created_at", datetime.utcnow().isoformat()),
            priority=data.get("priority", 5),
            parent_task_id=data.get("parent_task_id"),
            spec_requirement_ids=data.get("spec_requirement_ids", []),
            stream=stream,
            tier=tier,
            spec_ref=data.get("spec_ref"),
            attempt_count=data.get("attempt_count", 0),
            last_error=data.get("last_error"),
            assigned_worker=data.get("assigned_worker"),
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
