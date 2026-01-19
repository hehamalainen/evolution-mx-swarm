"""
Escalation Engine - Event-Driven Human Alerts

Triggers escalation when thresholds are breached:
- CI health < 85% for 30 min
- Retries/nacks > 25% for 30 min
- No merges for 60-90 min while queue is non-empty
- Budget burn exceeds forecast
- Security gate failures on Tier2 work
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import List, Dict, Optional
import os
import json


class EscalationSeverity(str, Enum):
    LOW = "low"          # Informational
    MEDIUM = "medium"    # Needs attention soon
    HIGH = "high"        # Needs immediate attention
    CRITICAL = "critical"  # System at risk


class EscalationType(str, Enum):
    CI_HEALTH = "ci_health"
    RETRY_RATE = "retry_rate"
    STALL = "stall"
    BUDGET = "budget"
    SECURITY = "security"
    STAGING = "staging"


@dataclass
class Escalation:
    id: str
    type: EscalationType
    severity: EscalationSeverity
    title: str
    description: str
    recommended_action: str
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    acknowledged: bool = False
    resolved: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type.value,
            "severity": self.severity.value,
            "title": self.title,
            "description": self.description,
            "recommended_action": self.recommended_action,
            "created_at": self.created_at,
            "acknowledged": self.acknowledged,
            "resolved": self.resolved,
        }


@dataclass
class EscalationThresholds:
    ci_health_min: float = 0.85  # 85%
    ci_health_duration_min: int = 30
    retry_rate_max: float = 0.25  # 25%
    retry_rate_duration_min: int = 30
    stall_duration_min: int = 60
    budget_warning_percent: float = 0.80  # 80% of cap


class EscalationEngine:
    def __init__(self, repo_path: str = "."):
        self.repo_path = repo_path
        self.thresholds = EscalationThresholds()
        self.active_escalations: List[Escalation] = []
        self._escalation_counter = 0

        # Track breach durations
        self._ci_breach_start: Optional[datetime] = None
        self._retry_breach_start: Optional[datetime] = None
        self._stall_start: Optional[datetime] = None
        self._last_merge_time: Optional[datetime] = None

    def check_triggers(self, metrics: Dict) -> List[Escalation]:
        """Check all escalation triggers and return new escalations."""
        new_escalations = []
        now = datetime.utcnow()

        # 1. CI Health Check
        ci_health = metrics.get("ci_health", 1.0)
        if ci_health < self.thresholds.ci_health_min:
            if self._ci_breach_start is None:
                self._ci_breach_start = now
            elif (now - self._ci_breach_start).seconds >= self.thresholds.ci_health_duration_min * 60:
                esc = self._create_escalation(
                    EscalationType.CI_HEALTH,
                    EscalationSeverity.HIGH,
                    "CI Health Critical",
                    f"CI pass rate at {ci_health:.1%} for {self.thresholds.ci_health_duration_min}+ minutes",
                    "Pause new PR creation, investigate failing tests"
                )
                if esc:
                    new_escalations.append(esc)
        else:
            self._ci_breach_start = None

        # 2. Retry Rate Check
        retry_rate = metrics.get("retry_rate", 0.0)
        if retry_rate > self.thresholds.retry_rate_max:
            if self._retry_breach_start is None:
                self._retry_breach_start = now
            elif (now - self._retry_breach_start).seconds >= self.thresholds.retry_rate_duration_min * 60:
                esc = self._create_escalation(
                    EscalationType.RETRY_RATE,
                    EscalationSeverity.MEDIUM,
                    "High Retry Rate",
                    f"Task retry rate at {retry_rate:.1%} for {self.thresholds.retry_rate_duration_min}+ minutes",
                    "Review task complexity, consider replanning"
                )
                if esc:
                    new_escalations.append(esc)
        else:
            self._retry_breach_start = None

        # 3. Stall Detection
        queue_depth = metrics.get("queue_depth", 0)
        last_merge = metrics.get("last_merge_time")

        if queue_depth > 0:
            if last_merge:
                self._last_merge_time = datetime.fromisoformat(last_merge) if isinstance(last_merge, str) else last_merge

            if self._last_merge_time:
                stall_minutes = (now - self._last_merge_time).seconds / 60
                if stall_minutes >= self.thresholds.stall_duration_min:
                    esc = self._create_escalation(
                        EscalationType.STALL,
                        EscalationSeverity.HIGH,
                        "Delivery Stall Detected",
                        f"No merges for {int(stall_minutes)} minutes with {queue_depth} tasks in queue",
                        "Check for blockers, consider emergency replan"
                    )
                    if esc:
                        new_escalations.append(esc)

        # 4. Budget Warning
        budget_percent = metrics.get("budget_percent", 0.0)
        if budget_percent >= self.thresholds.budget_warning_percent:
            esc = self._create_escalation(
                EscalationType.BUDGET,
                EscalationSeverity.MEDIUM if budget_percent < 0.95 else EscalationSeverity.HIGH,
                "Budget Cap Approaching",
                f"Budget at {budget_percent:.1%} of daily cap",
                "Approve budget increase or pause swarm"
            )
            if esc:
                new_escalations.append(esc)

        # 5. Security Gate Failures
        security_fails = metrics.get("security_failures", 0)
        if security_fails > 0:
            esc = self._create_escalation(
                EscalationType.SECURITY,
                EscalationSeverity.CRITICAL,
                "Security Gate Failed",
                f"{security_fails} PR(s) failed security scans",
                "Review security issues immediately, block merge"
            )
            if esc:
                new_escalations.append(esc)

        return new_escalations

    def _create_escalation(self, esc_type: EscalationType, severity: EscalationSeverity,
                          title: str, description: str, action: str) -> Optional[Escalation]:
        """Create escalation if not already active for this type."""
        # Check if we already have an active escalation of this type
        for esc in self.active_escalations:
            if esc.type == esc_type and not esc.resolved:
                return None  # Already escalated

        self._escalation_counter += 1
        escalation = Escalation(
            id=f"ESC-{self._escalation_counter:04d}",
            type=esc_type,
            severity=severity,
            title=title,
            description=description,
            recommended_action=action
        )
        self.active_escalations.append(escalation)
        self._save_escalation(escalation)
        return escalation

    def acknowledge(self, escalation_id: str) -> bool:
        """Mark escalation as acknowledged."""
        for esc in self.active_escalations:
            if esc.id == escalation_id:
                esc.acknowledged = True
                return True
        return False

    def resolve(self, escalation_id: str) -> bool:
        """Mark escalation as resolved."""
        for esc in self.active_escalations:
            if esc.id == escalation_id:
                esc.resolved = True
                return True
        return False

    def get_active_escalations(self) -> List[Escalation]:
        """Get all unresolved escalations."""
        return [e for e in self.active_escalations if not e.resolved]

    def _save_escalation(self, escalation: Escalation):
        """Save escalation to file."""
        esc_dir = os.path.join(self.repo_path, "escalations")
        os.makedirs(esc_dir, exist_ok=True)

        filename = os.path.join(esc_dir, f"{escalation.id}.json")
        with open(filename, "w") as f:
            json.dump(escalation.to_dict(), f, indent=2)
