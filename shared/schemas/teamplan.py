"""
TEAMPLAN.json Schema - Org-as-Code Contract

The TEAMPLAN defines the entire organizational structure, governance,
and operational policies for a swarm execution. It's deterministic,
machine-readable, and the runtime MUST obey it.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class Topology(str, Enum):
    """Organizational topology type."""
    STREAMS = "streams"  # v1: flat streams by role
    PODS = "pods"        # v2: tightly coupled epic pods


class RiskTier(int, Enum):
    """Risk classification for tasks and PRs."""
    LOW = 0       # Auto-merge on green CI
    MEDIUM = 1    # Auto-merge with feature flag
    HIGH = 2      # Manual review required


class MergePolicy(str, Enum):
    """How PRs should be merged by risk tier."""
    AUTO_ON_GREEN = "auto_on_green"
    AUTO_ON_GREEN_FEATURE_FLAG = "auto_on_green_feature_flag"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"


class GovernorMode(str, Enum):
    """Governor operational mode."""
    NORMAL = "NORMAL"
    THROTTLED = "THROTTLED"
    FROZEN = "FROZEN"


@dataclass
class StreamConfig:
    """Configuration for a worker stream (role-based pool)."""
    name: str                          # stream identifier
    role: str                          # role prompt key
    target_workers: int                # desired worker count
    max_workers: int                   # hard cap
    tools: list[str] = field(default_factory=list)  # allowed tool permissions
    priority: int = 1                  # queue priority (higher = more urgent)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "role": self.role,
            "target_workers": self.target_workers,
            "max_workers": self.max_workers,
            "tools": self.tools,
            "priority": self.priority
        }

    @classmethod
    def from_dict(cls, data: dict) -> "StreamConfig":
        return cls(
            name=data["name"],
            role=data["role"],
            target_workers=data["target_workers"],
            max_workers=data["max_workers"],
            tools=data.get("tools", []),
            priority=data.get("priority", 1)
        )


@dataclass
class PodConfig:
    """Configuration for a pod (tightly coupled epic team)."""
    name: str                          # pod identifier
    epic_ref: str                      # SPEC epic reference
    roles: dict[str, int]              # role -> worker count
    max_workers: int                   # total pod cap
    shared_context: list[str] = field(default_factory=list)  # shared files/modules

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "epic_ref": self.epic_ref,
            "roles": self.roles,
            "max_workers": self.max_workers,
            "shared_context": self.shared_context
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PodConfig":
        return cls(
            name=data["name"],
            epic_ref=data["epic_ref"],
            roles=data["roles"],
            max_workers=data["max_workers"],
            shared_context=data.get("shared_context", [])
        )


@dataclass
class CapsConfig:
    """Governance caps - hard limits on swarm activity."""
    pr_cap_per_hour: int = 15          # max PRs created per hour
    open_pr_cap: int = 25              # max concurrent open PRs
    max_parallel_ci: int = 10          # max CI jobs running
    max_active_tasks: int = 60         # max tasks in progress

    def to_dict(self) -> dict:
        return {
            "pr_cap_per_hour": self.pr_cap_per_hour,
            "open_pr_cap": self.open_pr_cap,
            "max_parallel_ci": self.max_parallel_ci,
            "max_active_tasks": self.max_active_tasks
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CapsConfig":
        return cls(
            pr_cap_per_hour=data.get("pr_cap_per_hour", 15),
            open_pr_cap=data.get("open_pr_cap", 25),
            max_parallel_ci=data.get("max_parallel_ci", 10),
            max_active_tasks=data.get("max_active_tasks", 60)
        )


@dataclass
class RiskMergePolicy:
    """Merge policy by risk tier."""
    tier0: MergePolicy = MergePolicy.AUTO_ON_GREEN
    tier1: MergePolicy = MergePolicy.AUTO_ON_GREEN_FEATURE_FLAG
    tier2: MergePolicy = MergePolicy.MANUAL_REVIEW_REQUIRED

    def to_dict(self) -> dict:
        return {
            "tier0": self.tier0.value,
            "tier1": self.tier1.value,
            "tier2": self.tier2.value
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RiskMergePolicy":
        return cls(
            tier0=MergePolicy(data.get("tier0", "auto_on_green")),
            tier1=MergePolicy(data.get("tier1", "auto_on_green_feature_flag")),
            tier2=MergePolicy(data.get("tier2", "manual_review_required"))
        )

    def get_policy(self, tier: int) -> MergePolicy:
        """Get merge policy for a risk tier."""
        if tier == 0:
            return self.tier0
        elif tier == 1:
            return self.tier1
        return self.tier2


@dataclass
class TaskPolicy:
    """Constraints on individual tasks."""
    requires_spec_ref: bool = True     # tasks must reference SPEC item
    max_files_touched: int = 12        # max files per task
    max_modules_touched: int = 3       # max modules per task
    max_task_minutes: int = 45         # task timebox
    require_tests: bool = True         # tasks must include tests
    require_evidence: bool = True      # tasks must provide evidence

    def to_dict(self) -> dict:
        return {
            "requires_spec_ref": self.requires_spec_ref,
            "max_files_touched": self.max_files_touched,
            "max_modules_touched": self.max_modules_touched,
            "max_task_minutes": self.max_task_minutes,
            "require_tests": self.require_tests,
            "require_evidence": self.require_evidence
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TaskPolicy":
        return cls(
            requires_spec_ref=data.get("requires_spec_ref", True),
            max_files_touched=data.get("max_files_touched", 12),
            max_modules_touched=data.get("max_modules_touched", 3),
            max_task_minutes=data.get("max_task_minutes", 45),
            require_tests=data.get("require_tests", True),
            require_evidence=data.get("require_evidence", True)
        )


@dataclass
class RetryPolicy:
    """Task retry and dead-letter queue policy."""
    max_retries: int = 3               # max retry attempts
    backoff_seconds: list[int] = field(default_factory=lambda: [60, 300, 900])
    dlq_enabled: bool = True           # send to dead-letter after max retries

    def to_dict(self) -> dict:
        return {
            "max_retries": self.max_retries,
            "backoff_seconds": self.backoff_seconds,
            "dlq_enabled": self.dlq_enabled
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RetryPolicy":
        return cls(
            max_retries=data.get("max_retries", 3),
            backoff_seconds=data.get("backoff_seconds", [60, 300, 900]),
            dlq_enabled=data.get("dlq_enabled", True)
        )


@dataclass
class BudgetPolicy:
    """Budget constraints and alerts."""
    usd_per_day: float = 50.0          # daily hard cap
    usd_per_hour_soft: float = 10.0    # soft hourly limit (triggers alert)
    hard_stop: bool = True             # stop all work when cap hit
    burn_alert_thresholds: list[int] = field(default_factory=lambda: [50, 75, 90])

    def to_dict(self) -> dict:
        return {
            "usd_per_day": self.usd_per_day,
            "usd_per_hour_soft": self.usd_per_hour_soft,
            "hard_stop": self.hard_stop,
            "burn_alert_thresholds": self.burn_alert_thresholds
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BudgetPolicy":
        return cls(
            usd_per_day=data.get("usd_per_day", 50.0),
            usd_per_hour_soft=data.get("usd_per_hour_soft", 10.0),
            hard_stop=data.get("hard_stop", True),
            burn_alert_thresholds=data.get("burn_alert_thresholds", [50, 75, 90])
        )


@dataclass
class ReorgPolicy:
    """Policy for dynamic reorganization."""
    interval_minutes: int = 60         # how often to evaluate reorg
    hysteresis_minutes: int = 30       # threshold must persist this long
    max_worker_delta_per_interval: int = 20  # max workers added/removed
    prefer_caps_before_reorg: bool = True    # adjust caps first
    allow_stream_merge: bool = False   # can merge underutilized streams
    allow_stream_split: bool = False   # can split overloaded streams

    def to_dict(self) -> dict:
        return {
            "interval_minutes": self.interval_minutes,
            "hysteresis_minutes": self.hysteresis_minutes,
            "max_worker_delta_per_interval": self.max_worker_delta_per_interval,
            "prefer_caps_before_reorg": self.prefer_caps_before_reorg,
            "allow_stream_merge": self.allow_stream_merge,
            "allow_stream_split": self.allow_stream_split
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ReorgPolicy":
        return cls(
            interval_minutes=data.get("interval_minutes", 60),
            hysteresis_minutes=data.get("hysteresis_minutes", 30),
            max_worker_delta_per_interval=data.get("max_worker_delta_per_interval", 20),
            prefer_caps_before_reorg=data.get("prefer_caps_before_reorg", True),
            allow_stream_merge=data.get("allow_stream_merge", False),
            allow_stream_split=data.get("allow_stream_split", False)
        )


@dataclass
class ResetTriggers:
    """Thresholds that trigger automatic reset/replan."""
    ci_health_lt: int = 85             # CI health below this %
    nack_rate_gt: int = 25             # task rejection rate above this %
    stall_minutes_gt: int = 60         # no progress for this long
    conflict_rate_gt: int = 30         # merge conflict rate above this %
    retry_cascade_count: int = 5       # consecutive retries trigger reset

    def to_dict(self) -> dict:
        return {
            "ci_health_lt": self.ci_health_lt,
            "nack_rate_gt": self.nack_rate_gt,
            "stall_minutes_gt": self.stall_minutes_gt,
            "conflict_rate_gt": self.conflict_rate_gt,
            "retry_cascade_count": self.retry_cascade_count
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ResetTriggers":
        return cls(
            ci_health_lt=data.get("ci_health_lt", 85),
            nack_rate_gt=data.get("nack_rate_gt", 25),
            stall_minutes_gt=data.get("stall_minutes_gt", 60),
            conflict_rate_gt=data.get("conflict_rate_gt", 30),
            retry_cascade_count=data.get("retry_cascade_count", 5)
        )


@dataclass
class ObservabilityConfig:
    """What metrics to emit and track."""
    emit_task_events: bool = True
    emit_pr_events: bool = True
    emit_ci_events: bool = True
    emit_governor_events: bool = True
    emit_budget_events: bool = True
    emit_coordination_metrics: bool = True
    metrics_interval_seconds: int = 30

    def to_dict(self) -> dict:
        return {
            "emit_task_events": self.emit_task_events,
            "emit_pr_events": self.emit_pr_events,
            "emit_ci_events": self.emit_ci_events,
            "emit_governor_events": self.emit_governor_events,
            "emit_budget_events": self.emit_budget_events,
            "emit_coordination_metrics": self.emit_coordination_metrics,
            "metrics_interval_seconds": self.metrics_interval_seconds
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ObservabilityConfig":
        return cls(
            emit_task_events=data.get("emit_task_events", True),
            emit_pr_events=data.get("emit_pr_events", True),
            emit_ci_events=data.get("emit_ci_events", True),
            emit_governor_events=data.get("emit_governor_events", True),
            emit_budget_events=data.get("emit_budget_events", True),
            emit_coordination_metrics=data.get("emit_coordination_metrics", True),
            metrics_interval_seconds=data.get("metrics_interval_seconds", 30)
        )


@dataclass
class TeamPlan:
    """
    The master org-as-code contract.

    This defines EVERYTHING about how the swarm operates:
    - Who works (streams/pods)
    - What they can do (caps, policies)
    - How they're governed (merge, retry, budget)
    - When to adapt (reorg, reset triggers)
    """
    version: str = "0.1"
    topology: Topology = Topology.STREAMS

    # Organization structure
    streams: list[StreamConfig] = field(default_factory=list)
    pods: list[PodConfig] = field(default_factory=list)

    # Governance
    caps: CapsConfig = field(default_factory=CapsConfig)
    risk_merge_policy: RiskMergePolicy = field(default_factory=RiskMergePolicy)
    task_policy: TaskPolicy = field(default_factory=TaskPolicy)
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    budget_policy: BudgetPolicy = field(default_factory=BudgetPolicy)

    # Adaptation
    reorg_policy: ReorgPolicy = field(default_factory=ReorgPolicy)
    reset_triggers: ResetTriggers = field(default_factory=ResetTriggers)

    # Observability
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)

    # Metadata
    prd_hash: str | None = None     # hash of source PRD
    spec_hash: str | None = None    # hash of generated SPEC
    created_at: str | None = None
    created_by: str = "org_builder"

    # Computed scores (from PRD analysis)
    complexity_score: float = 0.0      # 0-1
    coupling_score: float = 0.0        # 0-1
    risk_score: float = 0.0            # 0-1

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now(timezone.utc).isoformat()

    def total_target_workers(self) -> int:
        """Total target workers across all streams/pods."""
        if self.topology == Topology.STREAMS:
            return sum(s.target_workers for s in self.streams)
        return sum(p.max_workers for p in self.pods)

    def total_max_workers(self) -> int:
        """Total max workers across all streams/pods."""
        if self.topology == Topology.STREAMS:
            return sum(s.max_workers for s in self.streams)
        return sum(p.max_workers for p in self.pods)

    def get_stream(self, name: str) -> StreamConfig | None:
        """Get stream config by name."""
        for s in self.streams:
            if s.name == name:
                return s
        return None

    def get_role_mix(self) -> dict[str, float]:
        """Get role mix as percentages."""
        total = self.total_target_workers()
        if total == 0:
            return {}
        return {s.name: s.target_workers / total * 100 for s in self.streams}

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "topology": self.topology.value,
            "streams": [s.to_dict() for s in self.streams],
            "pods": [p.to_dict() for p in self.pods],
            "caps": self.caps.to_dict(),
            "risk_merge_policy": self.risk_merge_policy.to_dict(),
            "task_policy": self.task_policy.to_dict(),
            "retry_policy": self.retry_policy.to_dict(),
            "budget_policy": self.budget_policy.to_dict(),
            "reorg_policy": self.reorg_policy.to_dict(),
            "reset_triggers": self.reset_triggers.to_dict(),
            "observability": self.observability.to_dict(),
            "prd_hash": self.prd_hash,
            "spec_hash": self.spec_hash,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "complexity_score": self.complexity_score,
            "coupling_score": self.coupling_score,
            "risk_score": self.risk_score
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def save(self, path: str):
        """Save TEAMPLAN to file."""
        with open(path, 'w') as f:
            f.write(self.to_json())

    @classmethod
    def from_dict(cls, data: dict) -> "TeamPlan":
        return cls(
            version=data.get("version", "0.1"),
            topology=Topology(data.get("topology", "streams")),
            streams=[StreamConfig.from_dict(s) for s in data.get("streams", [])],
            pods=[PodConfig.from_dict(p) for p in data.get("pods", [])],
            caps=CapsConfig.from_dict(data.get("caps", {})),
            risk_merge_policy=RiskMergePolicy.from_dict(data.get("risk_merge_policy", {})),
            task_policy=TaskPolicy.from_dict(data.get("task_policy", {})),
            retry_policy=RetryPolicy.from_dict(data.get("retry_policy", {})),
            budget_policy=BudgetPolicy.from_dict(data.get("budget_policy", {})),
            reorg_policy=ReorgPolicy.from_dict(data.get("reorg_policy", {})),
            reset_triggers=ResetTriggers.from_dict(data.get("reset_triggers", {})),
            observability=ObservabilityConfig.from_dict(data.get("observability", {})),
            prd_hash=data.get("prd_hash"),
            spec_hash=data.get("spec_hash"),
            created_at=data.get("created_at"),
            created_by=data.get("created_by", "org_builder"),
            complexity_score=data.get("complexity_score", 0.0),
            coupling_score=data.get("coupling_score", 0.0),
            risk_score=data.get("risk_score", 0.0)
        )

    @classmethod
    def load(cls, path: str) -> "TeamPlan":
        """Load TEAMPLAN from file."""
        with open(path) as f:
            data = json.load(f)
        return cls.from_dict(data)

    @classmethod
    def default(cls) -> "TeamPlan":
        """Create a sensible default TEAMPLAN."""
        return cls(
            streams=[
                StreamConfig("feature", "feature_engineer", 50, 70, ["git", "tests", "ci"]),
                StreamConfig("qa", "qa_engineer", 25, 35, ["tests", "ci"]),
                StreamConfig("ops", "ops_engineer", 10, 15, ["ci", "deploy", "observability"]),
                StreamConfig("security", "security_engineer", 5, 10, ["ci", "scan"]),
                StreamConfig("docs", "docs_engineer", 5, 10, ["git"])
            ]
        )


# Role prompt templates for each stream
ROLE_PROMPTS = {
    "feature_engineer": """You are a feature engineer focused on implementing new functionality.
Your priorities:
1. Write clean, testable code that implements the spec
2. Include unit tests for all new code
3. Keep changes focused - one feature per PR
4. Document public APIs and complex logic
5. Consider edge cases and error handling""",

    "qa_engineer": """You are a QA engineer focused on quality assurance.
Your priorities:
1. Write comprehensive integration and e2e tests
2. Identify edge cases and failure modes
3. Verify acceptance criteria are met
4. Check for security and performance issues
5. Ensure test coverage meets standards""",

    "ops_engineer": """You are an ops engineer focused on deployment and reliability.
Your priorities:
1. Set up CI/CD pipelines and automation
2. Configure monitoring and alerting
3. Manage infrastructure as code
4. Ensure staging/production parity
5. Document runbooks and incident procedures""",

    "security_engineer": """You are a security engineer focused on secure coding.
Your priorities:
1. Review code for security vulnerabilities (OWASP top 10)
2. Implement authentication and authorization
3. Secure data handling and encryption
4. Set up security scanning in CI
5. Document security considerations""",

    "docs_engineer": """You are a documentation engineer.
Your priorities:
1. Write clear API documentation
2. Create user guides and tutorials
3. Document architecture decisions
4. Maintain README and setup guides
5. Keep docs in sync with code changes"""
}
