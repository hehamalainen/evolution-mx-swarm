"""
Decision Queue - Human Decision Management System

Implements the "Decisions Needed" system from Phase 2:
- Explicit escalation triggers that create decisions
- Decision types with recommended actions
- Approval/dismissal workflow
- Integration with Governor for automatic execution

Human actions allowed:
- Approve Tier2 PRs
- Adjust budget cap / risk tolerance
- Pause/resume swarm
- Replan from PRD section X
- Ship now (promote to prod)
"""

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

logger = logging.getLogger(__name__)


class DecisionType(str, Enum):
    """Types of decisions that require human input."""
    BUDGET_INCREASE = "budget_increase"
    BUDGET_PAUSE = "budget_pause"
    APPROVE_TIER2_PR = "approve_tier2_pr"
    APPROVE_DEPLOYMENT = "approve_deployment"
    ADJUST_RISK_TOLERANCE = "adjust_risk_tolerance"
    PAUSE_SWARM = "pause_swarm"
    RESUME_SWARM = "resume_swarm"
    REPLAN_REQUIRED = "replan_required"
    SECURITY_REVIEW = "security_review"
    FREEZE_TIER = "freeze_tier"
    UNFREEZE_TIER = "unfreeze_tier"
    SHIP_NOW = "ship_now"


class DecisionPriority(str, Enum):
    """Priority levels for decisions."""
    LOW = "low"          # Can wait for daily standup
    MEDIUM = "medium"    # Should be addressed within hours
    HIGH = "high"        # Needs attention soon
    CRITICAL = "critical"  # Blocks swarm progress


class DecisionStatus(str, Enum):
    """Status of a decision."""
    PENDING = "pending"
    APPROVED = "approved"
    DISMISSED = "dismissed"
    EXPIRED = "expired"
    AUTO_RESOLVED = "auto_resolved"


@dataclass
class DecisionAction:
    """An action that can be taken on a decision."""
    id: str
    label: str
    description: str
    style: str = "primary"  # primary, success, warning, danger, secondary
    auto_execute: bool = False  # If true, system can auto-execute under conditions


@dataclass
class Decision:
    """A decision requiring human input."""
    id: str
    type: DecisionType
    priority: DecisionPriority
    title: str
    description: str
    context: dict  # Relevant data for making the decision
    actions: list[DecisionAction] = field(default_factory=list)
    recommended_action: str | None = None  # ID of recommended action
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None
    status: DecisionStatus = DecisionStatus.PENDING
    resolved_at: datetime | None = None
    resolved_by: str | None = None  # "human" or "auto"
    resolved_action: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type.value,
            "priority": self.priority.value,
            "title": self.title,
            "description": self.description,
            "context": self.context,
            "actions": [
                {"id": a.id, "label": a.label, "description": a.description, "style": a.style}
                for a in self.actions
            ],
            "recommended_action": self.recommended_action,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "status": self.status.value,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "resolved_by": self.resolved_by,
            "resolved_action": self.resolved_action,
        }


class DecisionQueue:
    """
    Manages decisions requiring human input.

    Key features:
    - Creates decisions from escalation triggers
    - Tracks decision lifecycle (pending -> approved/dismissed)
    - Provides actions for dashboard
    - Can auto-resolve decisions under certain conditions
    """

    def __init__(self, repo_path: str = "."):
        self.repo_path = repo_path
        self.decisions_dir = os.path.join(repo_path, "decisions")
        os.makedirs(self.decisions_dir, exist_ok=True)

        self.decisions: list[Decision] = []
        self._decision_counter = 0

        # Callbacks for action execution
        self._action_handlers: dict[str, Callable[[Decision, str], bool]] = {}

        # Load existing decisions
        self._load_decisions()

    def _load_decisions(self):
        """Load pending decisions from disk."""
        if not os.path.exists(self.decisions_dir):
            return

        for filename in os.listdir(self.decisions_dir):
            if filename.endswith(".json"):
                try:
                    with open(os.path.join(self.decisions_dir, filename)) as f:
                        data = json.load(f)
                        # Only load pending decisions
                        if data.get("status") == "pending":
                            self._decision_counter = max(
                                self._decision_counter,
                                int(data["id"].split("-")[1])
                            )
                            # Reconstruct decision (simplified)
                            decision = Decision(
                                id=data["id"],
                                type=DecisionType(data["type"]),
                                priority=DecisionPriority(data["priority"]),
                                title=data["title"],
                                description=data["description"],
                                context=data.get("context", {}),
                                status=DecisionStatus(data["status"]),
                                created_at=datetime.fromisoformat(data["created_at"]),
                            )
                            self.decisions.append(decision)
                except Exception as e:
                    logger.warning(f"Failed to load decision {filename}: {e}")

    def register_action_handler(self, action_prefix: str, handler: Callable[[Decision, str], bool]):
        """Register a handler for executing decision actions."""
        self._action_handlers[action_prefix] = handler

    def create_decision(
        self,
        decision_type: DecisionType,
        title: str,
        description: str,
        context: dict,
        priority: DecisionPriority = DecisionPriority.MEDIUM,
        expires_hours: int | None = 24,
        actions: list[DecisionAction] | None = None,
        recommended_action: str | None = None
    ) -> Decision:
        """Create a new decision requiring human input."""
        # Check if we already have a pending decision of this type
        for d in self.decisions:
            if d.type == decision_type and d.status == DecisionStatus.PENDING:
                logger.debug(f"Decision of type {decision_type} already pending")
                return d

        self._decision_counter += 1
        decision_id = f"DEC-{self._decision_counter:04d}"

        # Default actions based on type
        if actions is None:
            actions = self._get_default_actions(decision_type)

        decision = Decision(
            id=decision_id,
            type=decision_type,
            priority=priority,
            title=title,
            description=description,
            context=context,
            actions=actions,
            recommended_action=recommended_action,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=expires_hours) if expires_hours else None
        )

        self.decisions.append(decision)
        self._save_decision(decision)

        logger.info(f"Decision created: {decision_id} - {title}")
        return decision

    def _get_default_actions(self, decision_type: DecisionType) -> list[DecisionAction]:
        """Get default actions for a decision type."""
        if decision_type == DecisionType.BUDGET_INCREASE:
            return [
                DecisionAction("approve_50", "Approve +$50", "Increase daily budget by $50", "success"),
                DecisionAction("approve_100", "Approve +$100", "Increase daily budget by $100", "success"),
                DecisionAction("pause", "Pause Swarm", "Stop new work until tomorrow", "warning"),
                DecisionAction("dismiss", "Dismiss", "Continue at current rate", "secondary"),
            ]

        elif decision_type == DecisionType.APPROVE_TIER2_PR:
            return [
                DecisionAction("approve", "Approve PR", "Approve for merge", "success"),
                DecisionAction("request_changes", "Request Changes", "Send back for revision", "warning"),
                DecisionAction("reject", "Reject", "Close PR without merge", "danger"),
            ]

        elif decision_type == DecisionType.APPROVE_DEPLOYMENT:
            return [
                DecisionAction("deploy_staging", "Deploy to Staging", "Deploy to staging environment", "primary"),
                DecisionAction("deploy_prod", "Deploy to Production", "Deploy to production", "success"),
                DecisionAction("delay", "Delay", "Postpone deployment", "secondary"),
            ]

        elif decision_type == DecisionType.REPLAN_REQUIRED:
            return [
                DecisionAction("replan_full", "Full Replan", "Regenerate all tasks from PRD", "primary"),
                DecisionAction("replan_partial", "Partial Replan", "Replan only blocked tasks", "secondary"),
                DecisionAction("manual_fix", "Manual Fix", "Fix blocking issues manually", "warning"),
            ]

        elif decision_type == DecisionType.SECURITY_REVIEW:
            return [
                DecisionAction("approve", "Approve", "Security issue addressed", "success"),
                DecisionAction("block", "Block Merge", "Block until resolved", "danger"),
                DecisionAction("escalate", "Escalate", "Escalate to security team", "warning"),
            ]

        elif decision_type in [DecisionType.PAUSE_SWARM, DecisionType.FREEZE_TIER]:
            return [
                DecisionAction("confirm", "Confirm", "Proceed with action", "warning"),
                DecisionAction("cancel", "Cancel", "Cancel action", "secondary"),
            ]

        elif decision_type == DecisionType.SHIP_NOW:
            return [
                DecisionAction("ship", "Ship Now", "Promote to production immediately", "success"),
                DecisionAction("delay", "Delay", "Wait for more testing", "secondary"),
                DecisionAction("cancel", "Cancel", "Do not ship", "danger"),
            ]

        # Default actions
        return [
            DecisionAction("approve", "Approve", "Approve this decision", "success"),
            DecisionAction("dismiss", "Dismiss", "Dismiss without action", "secondary"),
        ]

    def resolve_decision(
        self,
        decision_id: str,
        action_id: str,
        resolved_by: str = "human"
    ) -> bool:
        """Resolve a decision by executing an action."""
        decision = self.get_decision(decision_id)
        if not decision:
            logger.warning(f"Decision not found: {decision_id}")
            return False

        if decision.status != DecisionStatus.PENDING:
            logger.warning(f"Decision {decision_id} is not pending (status: {decision.status})")
            return False

        # Validate action
        valid_actions = [a.id for a in decision.actions]
        if action_id not in valid_actions:
            logger.warning(f"Invalid action {action_id} for decision {decision_id}")
            return False

        # Execute action handler if registered
        for prefix, handler in self._action_handlers.items():
            if action_id.startswith(prefix):
                try:
                    success = handler(decision, action_id)
                    if not success:
                        logger.warning(f"Action handler failed for {action_id}")
                        return False
                except Exception as e:
                    logger.error(f"Action handler error: {e}")
                    return False

        # Update decision status
        decision.status = DecisionStatus.APPROVED if action_id != "dismiss" else DecisionStatus.DISMISSED
        decision.resolved_at = datetime.now(timezone.utc)
        decision.resolved_by = resolved_by
        decision.resolved_action = action_id

        self._save_decision(decision)
        logger.info(f"Decision {decision_id} resolved with action: {action_id}")
        return True

    def get_decision(self, decision_id: str) -> Decision | None:
        """Get a decision by ID."""
        for d in self.decisions:
            if d.id == decision_id:
                return d
        return None

    def get_pending_decisions(self) -> list[Decision]:
        """Get all pending decisions, sorted by priority."""
        self._check_expirations()

        pending = [d for d in self.decisions if d.status == DecisionStatus.PENDING]

        # Sort by priority (critical first)
        priority_order = {
            DecisionPriority.CRITICAL: 0,
            DecisionPriority.HIGH: 1,
            DecisionPriority.MEDIUM: 2,
            DecisionPriority.LOW: 3,
        }
        return sorted(pending, key=lambda d: priority_order.get(d.priority, 99))

    def get_decisions_for_report(self, limit: int = 3) -> list[dict]:
        """Get decisions formatted for reports."""
        pending = self.get_pending_decisions()[:limit]
        return [d.to_dict() for d in pending]

    def _check_expirations(self):
        """Check and mark expired decisions."""
        now = datetime.now(timezone.utc)
        for decision in self.decisions:
            if decision.status == DecisionStatus.PENDING and decision.expires_at:
                if now > decision.expires_at:
                    decision.status = DecisionStatus.EXPIRED
                    decision.resolved_at = now
                    decision.resolved_by = "auto"
                    self._save_decision(decision)
                    logger.info(f"Decision {decision.id} expired")

    def _save_decision(self, decision: Decision):
        """Save decision to disk."""
        filename = os.path.join(self.decisions_dir, f"{decision.id}.json")
        with open(filename, "w") as f:
            json.dump(decision.to_dict(), f, indent=2)

    # =========================================================================
    # Factory Methods for Common Decisions
    # =========================================================================

    def create_budget_decision(self, current_spend: float, daily_cap: float, burn_rate: float) -> Decision:
        """Create a budget increase decision."""
        forecast_hours = (daily_cap - current_spend) / burn_rate if burn_rate > 0 else 999.0

        return self.create_decision(
            decision_type=DecisionType.BUDGET_INCREASE,
            title="Budget Cap Approaching",
            description=f"Burn rate ${burn_rate:.2f}/hr will hit ${daily_cap:.0f} cap in ~{forecast_hours:.1f}h",
            context={
                "current_spend": current_spend,
                "daily_cap": daily_cap,
                "burn_rate": burn_rate,
                "forecast_hours": forecast_hours,
            },
            priority=DecisionPriority.HIGH if forecast_hours < 2 else DecisionPriority.MEDIUM,
            recommended_action="approve_50"
        )

    def create_tier2_approval(self, pr_number: int, pr_title: str, risk_summary: str) -> Decision:
        """Create a Tier 2 PR approval decision."""
        return self.create_decision(
            decision_type=DecisionType.APPROVE_TIER2_PR,
            title=f"Approve Tier 2 PR #{pr_number}",
            description=f"{pr_title}\n\nRisk: {risk_summary}",
            context={
                "pr_number": pr_number,
                "pr_title": pr_title,
                "risk_summary": risk_summary,
            },
            priority=DecisionPriority.MEDIUM,
            recommended_action="approve"
        )

    def create_replan_decision(self, reason: str, blocked_tasks: int) -> Decision:
        """Create a replan decision."""
        return self.create_decision(
            decision_type=DecisionType.REPLAN_REQUIRED,
            title="Replan Required",
            description=f"{reason}\n\n{blocked_tasks} tasks are blocked.",
            context={
                "reason": reason,
                "blocked_tasks": blocked_tasks,
            },
            priority=DecisionPriority.HIGH,
            recommended_action="replan_partial"
        )

    def create_security_decision(self, pr_number: int, vulnerability: str, severity: str) -> Decision:
        """Create a security review decision."""
        return self.create_decision(
            decision_type=DecisionType.SECURITY_REVIEW,
            title=f"Security Issue in PR #{pr_number}",
            description=f"{vulnerability}\n\nSeverity: {severity}",
            context={
                "pr_number": pr_number,
                "vulnerability": vulnerability,
                "severity": severity,
            },
            priority=DecisionPriority.CRITICAL if severity == "high" else DecisionPriority.HIGH,
            recommended_action="block"
        )

    def create_deployment_decision(self, version: str, changes_summary: str) -> Decision:
        """Create a deployment approval decision."""
        return self.create_decision(
            decision_type=DecisionType.APPROVE_DEPLOYMENT,
            title=f"Deploy {version}",
            description=f"Ready for deployment.\n\n{changes_summary}",
            context={
                "version": version,
                "changes_summary": changes_summary,
            },
            priority=DecisionPriority.MEDIUM,
            recommended_action="deploy_staging"
        )
