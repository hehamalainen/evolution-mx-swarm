"""Reputation Manager - Trust-First Scaling for MX Swarm.

Tracks agent performance metrics and maintains a persistent reputation database.
Used by the Judge to evaluate agents and by the Boss/Governor for hiring/firing.
"""

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime


@dataclass
class AgentReputation:
    """Reputation metrics for a single agent."""
    id: str
    trust_score: int = 50  # 0-100, starts at 50
    tasks_completed: int = 0
    tasks_failed: int = 0
    ci_first_pass_rate: float = 0.0
    blacklist_status: bool = False
    last_active: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    processed_events: set[str] = field(default_factory=set)
    history: list[dict] = field(default_factory=list)

    def update_score(self, event_id: str, success: bool, ci_passes: int, total_checks: int):
        """Update reputation based on latest task outcome."""
        if event_id in self.processed_events:
            return
        self.processed_events.add(event_id)

        self.tasks_completed += 1 if success else 0
        self.tasks_failed += 0 if success else 1

        # Calculate new CI pass rate (moving average)
        current_pass_rate = ci_passes / total_checks if total_checks > 0 else 0.0
        self.ci_first_pass_rate = (self.ci_first_pass_rate * (self.tasks_completed + self.tasks_failed - 1) + current_pass_rate) / (self.tasks_completed + self.tasks_failed)

        # Scoring logic
        if success:
            self.trust_score = min(100, self.trust_score + 5)
        else:
            # Failure is more penalizing
            self.trust_score = max(0, self.trust_score - 15)

        self.last_active = datetime.now(UTC).isoformat()
        self.history.append({
            "timestamp": self.last_active,
            "success": success,
            "ci_pass_rate": current_pass_rate,
            "score_after": self.trust_score
        })

        # Auto-blacklist if score drops too low
        if self.trust_score < 20:
            self.blacklist_status = True


class ReputationManager:
    """Service to persist and manage agent reputations."""

    def __init__(self, db_path: str = ".runtime/reputation_db.json"):
        self.db_path = db_path
        self._reputations: dict[str, AgentReputation] = {}
        self._load()

    def _load(self):
        """Load reputation DB from disk."""
        if not os.path.exists(self.db_path):
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            self._save()
            return

        with open(self.db_path) as f:
            try:
                data = json.load(f)
                for agent_id, metrics in data.items():
                    processed_events = set(metrics.pop("processed_events", []))
                    self._reputations[agent_id] = AgentReputation(
                        processed_events=processed_events,
                        **metrics
                    )
            except (json.JSONDecodeError, KeyError):
                self._reputations = {}

    def _save(self):
        """Save reputation DB to disk."""
        data = {}
        for agent_id, rep in self._reputations.items():
            rep_dict = asdict(rep)
            rep_dict["processed_events"] = list(rep.processed_events)
            data[agent_id] = rep_dict

        with open(self.db_path, "w") as f:
            json.dump(data, f, indent=2)

    def get_reputation(self, agent_id: str) -> AgentReputation:
        """Get or create reputation for an agent."""
        if agent_id not in self._reputations:
            self._reputations[agent_id] = AgentReputation(id=agent_id)
        return self._reputations[agent_id]

    def record_outcome(self, agent_id: str, event_id: str, success: bool, ci_passes: int, total_checks: int):
        """Record a task outcome and persist."""
        rep = self.get_reputation(agent_id)
        rep.update_score(event_id, success, ci_passes, total_checks)
        self._save()

    def is_blacklisted(self, agent_id: str) -> bool:
        """Check if an agent is blacklisted."""
        return self.get_reputation(agent_id).blacklist_status

    def set_blacklist(self, agent_id: str, status: bool):
        """Manually set blacklist status."""
        rep = self.get_reputation(agent_id)
        rep.blacklist_status = status
        self._save()
