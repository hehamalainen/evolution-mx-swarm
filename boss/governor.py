"""
Governor - Closed-Loop Control System

The Governor monitors swarm telemetry and adjusts operations
to maintain quality and efficiency. It enforces TEAMPLAN policies
and makes dynamic adjustments without thrashing.

Key responsibilities:
1. Monitor telemetry (CI health, PR backlog, task metrics)
2. Detect trigger conditions (quality dips, stalls, conflicts)
3. Adjust levers (worker counts, PR caps, freeze tiers)
4. Avoid thrash (hysteresis, magnitude limits)
"""

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional, Callable
import logging

from shared.schemas.teamplan import (
    TeamPlan, GovernorMode, RiskTier,
    ResetTriggers, ReorgPolicy, CapsConfig
)

logger = logging.getLogger(__name__)


class TriggerType(str, Enum):
    """Types of governor triggers."""
    CI_HEALTH_LOW = "ci_health_low"
    NACK_RATE_HIGH = "nack_rate_high"
    STALL_DETECTED = "stall_detected"
    CONFLICT_RATE_HIGH = "conflict_rate_high"
    RETRY_CASCADE = "retry_cascade"
    BUDGET_ALERT = "budget_alert"
    PR_CAP_REACHED = "pr_cap_reached"


class AdjustmentType(str, Enum):
    """Types of adjustments the governor can make."""
    SCALE_WORKERS = "scale_workers"
    ADJUST_PR_CAP = "adjust_pr_cap"
    FREEZE_TIER = "freeze_tier"
    UNFREEZE_TIER = "unfreeze_tier"
    THROTTLE = "throttle"
    UNTHROTTLE = "unthrottle"
    TRIGGER_REPLAN = "trigger_replan"
    SHIFT_WORKERS = "shift_workers"  # Move workers between streams


@dataclass
class TelemetrySnapshot:
    """Point-in-time telemetry data."""
    timestamp: datetime = field(default_factory=datetime.utcnow)

    # CI metrics
    ci_health_percent: float = 100.0
    ci_passes_1h: int = 0
    ci_fails_1h: int = 0
    ci_avg_duration_s: float = 0.0

    # Task metrics
    tasks_completed_1h: int = 0
    tasks_failed_1h: int = 0
    tasks_nacked_1h: int = 0
    tasks_in_progress: int = 0
    queue_depth: int = 0

    # PR metrics
    prs_opened_1h: int = 0
    prs_merged_1h: int = 0
    prs_open_total: int = 0
    merge_conflicts_1h: int = 0

    # Worker metrics
    workers_active: int = 0
    workers_idle: int = 0
    workers_by_stream: Dict[str, int] = field(default_factory=dict)

    # Budget metrics
    spend_today_usd: float = 0.0
    burn_rate_hour_usd: float = 0.0

    # Spec progress
    spec_items_completed: int = 0
    spec_items_total: int = 0

    def nack_rate(self) -> float:
        """Calculate task nack rate percentage."""
        total = self.tasks_completed_1h + self.tasks_failed_1h + self.tasks_nacked_1h
        if total == 0:
            return 0.0
        return (self.tasks_nacked_1h / total) * 100

    def conflict_rate(self) -> float:
        """Calculate merge conflict rate percentage."""
        if self.prs_opened_1h == 0:
            return 0.0
        return (self.merge_conflicts_1h / self.prs_opened_1h) * 100


@dataclass
class GovernorState:
    """Current state of the governor.

    Anti-thrash mechanism:
    - 20% max change per interval for caps/workers
    - Hysteresis windows to prevent rapid toggling
    - Cooldown periods after adjustments
    """
    mode: GovernorMode = GovernorMode.NORMAL
    frozen_tiers: List[int] = field(default_factory=list)  # Risk tiers frozen for new PRs

    # Current caps (may differ from TEAMPLAN if adjusted)
    current_pr_cap_hour: int = 15
    current_open_pr_cap: int = 25
    current_max_workers: int = 100

    # Baseline caps (for 20% change calculation)
    baseline_pr_cap_hour: int = 15
    baseline_open_pr_cap: int = 25
    baseline_max_workers: int = 100

    # Trigger history (for hysteresis)
    trigger_history: Dict[TriggerType, List[datetime]] = field(default_factory=dict)

    # Last adjustments (for rate limiting)
    last_adjustment_time: Optional[datetime] = None
    last_reorg_time: Optional[datetime] = None
    cooldown_until: Optional[datetime] = None

    # Consecutive issues (for cascade detection)
    consecutive_ci_fails: int = 0
    consecutive_retries: int = 0

    # Total adjustments in current interval (for 20% limit)
    adjustments_this_interval: int = 0
    interval_start: Optional[datetime] = None


@dataclass
class Adjustment:
    """A single adjustment made by the governor.
    
    Enhanced for explainability (GPT 5.2 Fix 3):
    - threshold: What threshold triggered (e.g., "CI < 85%")
    - current_value: What the actual value was (e.g., "84.6%")
    - before_settings: State before adjustment
    - after_settings: State after adjustment
    - cooldown_until: When cooldown period ends
    - next_eval_at: When next evaluation will happen
    """
    timestamp: datetime
    type: AdjustmentType
    reason: str
    details: Dict
    trigger: Optional[TriggerType] = None
    # Explainability fields
    threshold: Optional[str] = None
    current_value: Optional[str] = None
    before_settings: Optional[Dict] = None
    after_settings: Optional[Dict] = None
    cooldown_until: Optional[datetime] = None
    next_eval_at: Optional[datetime] = None



class Governor:
    """
    Closed-loop control system for swarm operations.

    The governor runs periodically and:
    1. Collects telemetry
    2. Checks trigger conditions
    3. Makes adjustments within limits (20% max change per interval)
    4. Records decisions for observability

    Anti-Thrash Mechanism:
    - 20% max change per interval: No single adjustment can change a cap by more than 20%
    - Hysteresis windows: Triggers must persist for N minutes before action
    - Cooldown periods: After an adjustment, wait before making another
    - Interval reset: The 20% limit resets each interval (default 15 minutes)
    """

    # Anti-thrash constants
    MAX_CHANGE_PERCENT = 0.20  # 20% max change per interval
    COOLDOWN_MINUTES = 5       # Cooldown after each adjustment
    INTERVAL_MINUTES = 15      # Interval for 20% limit reset

    def __init__(
        self,
        teamplan: TeamPlan,
        telemetry_provider: Callable[[], TelemetrySnapshot],
        on_adjustment: Optional[Callable[[Adjustment], None]] = None
    ):
        self.teamplan = teamplan
        self.get_telemetry = telemetry_provider
        self.on_adjustment = on_adjustment

        # Initialize state from teamplan with baselines
        self.state = GovernorState(
            current_pr_cap_hour=teamplan.caps.pr_cap_per_hour,
            current_open_pr_cap=teamplan.caps.open_pr_cap,
            current_max_workers=teamplan.total_max_workers(),
            # Baselines for 20% change calculation
            baseline_pr_cap_hour=teamplan.caps.pr_cap_per_hour,
            baseline_open_pr_cap=teamplan.caps.open_pr_cap,
            baseline_max_workers=teamplan.total_max_workers(),
            interval_start=datetime.utcnow()
        )

        # Adjustment history
        self.adjustments: List[Adjustment] = []

        # Running flag
        self._running = False
        self._task: Optional[asyncio.Task] = None

    # =========================================================================
    # Anti-Thrash Methods
    # =========================================================================

    def _is_in_cooldown(self) -> bool:
        """Check if we're in a cooldown period after a recent adjustment."""
        if self.state.cooldown_until is None:
            return False
        return datetime.utcnow() < self.state.cooldown_until

    def _check_interval_reset(self):
        """Reset the 20% limit on new interval."""
        now = datetime.utcnow()
        if self.state.interval_start is None:
            self.state.interval_start = now
            self.state.adjustments_this_interval = 0
            return

        # Check if interval has passed
        interval_elapsed = (now - self.state.interval_start).total_seconds() / 60
        if interval_elapsed >= self.INTERVAL_MINUTES:
            # Reset baselines to current values for next interval
            self.state.baseline_pr_cap_hour = self.state.current_pr_cap_hour
            self.state.baseline_open_pr_cap = self.state.current_open_pr_cap
            self.state.baseline_max_workers = self.state.current_max_workers
            self.state.interval_start = now
            self.state.adjustments_this_interval = 0
            logger.debug("Interval reset - new baselines set")

    def _clamp_to_max_change(self, current: int, target: int, baseline: int) -> int:
        """
        Clamp a change to the 20% max change limit.

        Args:
            current: Current value
            target: Desired target value
            baseline: Baseline value for 20% calculation

        Returns:
            Clamped value that respects the 20% limit
        """
        max_delta = max(1, int(baseline * self.MAX_CHANGE_PERCENT))

        if target > current:
            # Increasing - cap at +20%
            return min(target, current + max_delta)
        else:
            # Decreasing - cap at -20%
            return max(target, current - max_delta)

    def _set_cooldown(self):
        """Set cooldown period after an adjustment."""
        self.state.cooldown_until = datetime.utcnow() + timedelta(minutes=self.COOLDOWN_MINUTES)
        self.state.adjustments_this_interval += 1

    def _can_adjust(self) -> bool:
        """Check if an adjustment can be made (not in cooldown, interval ok)."""
        if self._is_in_cooldown():
            logger.debug("Skipping adjustment - in cooldown period")
            return False

        self._check_interval_reset()
        return True

    # =========================================================================
    # Main Loop
    # =========================================================================

    async def start(self):
        """Start the governor loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Governor started")

    async def stop(self):
        """Stop the governor loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Governor stopped")

    async def _run_loop(self):
        """Main governor loop."""
        interval = self.teamplan.reorg_policy.interval_minutes * 60
        while self._running:
            try:
                await self._evaluate()
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Governor error: {e}")
                await asyncio.sleep(30)  # Back off on error

    async def _evaluate(self):
        """Evaluate telemetry and make adjustments."""
        telemetry = self.get_telemetry()
        triggers = self._check_triggers(telemetry)

        if not triggers:
            # Check if we can recover from throttled/frozen state
            await self._check_recovery(telemetry)
            return

        # Process triggers
        for trigger in triggers:
            await self._handle_trigger(trigger, telemetry)

    def _check_triggers(self, telemetry: TelemetrySnapshot) -> List[TriggerType]:
        """Check which trigger conditions are met."""
        triggers = []
        thresholds = self.teamplan.reset_triggers

        # CI health check
        if telemetry.ci_health_percent < thresholds.ci_health_lt:
            if self._check_hysteresis(TriggerType.CI_HEALTH_LOW):
                triggers.append(TriggerType.CI_HEALTH_LOW)

        # Nack rate check
        if telemetry.nack_rate() > thresholds.nack_rate_gt:
            if self._check_hysteresis(TriggerType.NACK_RATE_HIGH):
                triggers.append(TriggerType.NACK_RATE_HIGH)

        # Stall detection (no progress)
        if self._detect_stall(telemetry):
            if self._check_hysteresis(TriggerType.STALL_DETECTED):
                triggers.append(TriggerType.STALL_DETECTED)

        # Conflict rate check
        if telemetry.conflict_rate() > thresholds.conflict_rate_gt:
            if self._check_hysteresis(TriggerType.CONFLICT_RATE_HIGH):
                triggers.append(TriggerType.CONFLICT_RATE_HIGH)

        # PR cap check
        if telemetry.prs_open_total >= self.state.current_open_pr_cap:
            triggers.append(TriggerType.PR_CAP_REACHED)

        # Budget check
        budget = self.teamplan.budget_policy
        if telemetry.spend_today_usd >= budget.usd_per_day * 0.9:
            triggers.append(TriggerType.BUDGET_ALERT)

        return triggers

    def _check_hysteresis(self, trigger_type: TriggerType) -> bool:
        """
        Check if trigger has persisted long enough (hysteresis).

        This prevents oscillation by requiring conditions to persist.
        """
        now = datetime.utcnow()
        hysteresis = timedelta(minutes=self.teamplan.reorg_policy.hysteresis_minutes)

        # Record this trigger
        if trigger_type not in self.state.trigger_history:
            self.state.trigger_history[trigger_type] = []
        self.state.trigger_history[trigger_type].append(now)

        # Clean old entries
        cutoff = now - hysteresis
        self.state.trigger_history[trigger_type] = [
            t for t in self.state.trigger_history[trigger_type] if t > cutoff
        ]

        # Need multiple triggers within hysteresis window
        return len(self.state.trigger_history[trigger_type]) >= 2

    def _detect_stall(self, telemetry: TelemetrySnapshot) -> bool:
        """Detect if the swarm has stalled."""
        threshold = self.teamplan.reset_triggers.stall_minutes_gt

        # Stall = workers active but no completions
        if telemetry.workers_active > 0 and telemetry.tasks_completed_1h == 0:
            return True

        # Stall = queue has items but nothing in progress
        if telemetry.queue_depth > 0 and telemetry.tasks_in_progress == 0:
            return True

        return False

    async def _handle_trigger(self, trigger: TriggerType, telemetry: TelemetrySnapshot):
        """Handle a trigger by making appropriate adjustments."""
        logger.warning(f"Governor trigger: {trigger.value}")

        # Prefer caps before reorg
        if self.teamplan.reorg_policy.prefer_caps_before_reorg:
            if await self._try_caps_adjustment(trigger, telemetry):
                return

        # Make structural adjustment
        await self._make_structural_adjustment(trigger, telemetry)

    async def _try_caps_adjustment(
        self,
        trigger: TriggerType,
        telemetry: TelemetrySnapshot
    ) -> bool:
        """Try to resolve trigger by adjusting caps. Returns True if sufficient.

        Respects the 20% max change per interval anti-thrash limit.
        """
        # Check if we can make an adjustment
        if not self._can_adjust():
            return False

        if trigger == TriggerType.CI_HEALTH_LOW:
            # Reduce PR rate to let CI catch up
            # Target: reduce by 3, but clamp to 20% max change
            target_cap = max(5, self.state.current_pr_cap_hour - 3)
            new_cap = self._clamp_to_max_change(
                self.state.current_pr_cap_hour,
                target_cap,
                self.state.baseline_pr_cap_hour
            )
            new_cap = max(5, new_cap)  # Ensure minimum

            if new_cap != self.state.current_pr_cap_hour:
                old_cap = self.state.current_pr_cap_hour
                self._make_adjustment(
                    AdjustmentType.ADJUST_PR_CAP,
                    f"CI health low ({telemetry.ci_health_percent:.1f}%)",
                    {"old_cap": old_cap, "new_cap": new_cap},
                    trigger,
                    threshold=f"CI < {self.teamplan.reset_triggers.ci_health_lt}%",
                    current_value=f"{telemetry.ci_health_percent:.1f}%",
                    before_settings={"pr_cap_hour": old_cap},
                    after_settings={"pr_cap_hour": new_cap}
                )
                self.state.current_pr_cap_hour = new_cap
                self._set_cooldown()
                return True

        elif trigger == TriggerType.CONFLICT_RATE_HIGH:
            # Reduce open PR cap to reduce parallel changes
            # Target: reduce by 5, but clamp to 20% max change
            target_cap = max(5, self.state.current_open_pr_cap - 5)
            new_cap = self._clamp_to_max_change(
                self.state.current_open_pr_cap,
                target_cap,
                self.state.baseline_open_pr_cap
            )
            new_cap = max(5, new_cap)  # Ensure minimum

            if new_cap != self.state.current_open_pr_cap:
                old_cap = self.state.current_open_pr_cap
                self._make_adjustment(
                    AdjustmentType.ADJUST_PR_CAP,
                    f"Conflict rate high ({telemetry.conflict_rate():.1f}%)",
                    {"old_cap": old_cap, "new_cap": new_cap},
                    trigger,
                    threshold=f"Conflicts > {self.teamplan.reset_triggers.conflict_rate_gt}%",
                    current_value=f"{telemetry.conflict_rate():.1f}%",
                    before_settings={"open_pr_cap": old_cap},
                    after_settings={"open_pr_cap": new_cap}
                )
                self.state.current_open_pr_cap = new_cap
                self._set_cooldown()
                return True

        elif trigger == TriggerType.NACK_RATE_HIGH:
            # Freeze high-risk tiers first
            if 2 not in self.state.frozen_tiers:
                self._make_adjustment(
                    AdjustmentType.FREEZE_TIER,
                    f"Nack rate high ({telemetry.nack_rate():.1f}%)",
                    {"tier": 2},
                    trigger,
                    threshold=f"Nacks > {self.teamplan.reset_triggers.nack_rate_gt}%",
                    current_value=f"{telemetry.nack_rate():.1f}%",
                    before_settings={"frozen_tiers": list(self.state.frozen_tiers)},
                    after_settings={"frozen_tiers": self.state.frozen_tiers + [2]}
                )
                self.state.frozen_tiers.append(2)
                self._set_cooldown()
                return True

        return False

    async def _make_structural_adjustment(
        self,
        trigger: TriggerType,
        telemetry: TelemetrySnapshot
    ):
        """Make a structural adjustment (mode change, worker scaling).

        Respects the 20% max change per interval anti-thrash limit for worker scaling.
        """
        # Check if we can make adjustment (cooldown + rate limit)
        if not self._can_adjust():
            return

        now = datetime.utcnow()
        if self.state.last_adjustment_time:
            min_interval = timedelta(minutes=10)  # At least 10 min between major changes
            if now - self.state.last_adjustment_time < min_interval:
                return

        if trigger == TriggerType.CI_HEALTH_LOW:
            # Throttle mode: reduce activity
            if self.state.mode == GovernorMode.NORMAL:
                old_workers = self.state.current_max_workers
                # Target: halve workers, but clamp to 20% max change
                target_workers = max(10, old_workers // 2)
                new_workers = self._clamp_to_max_change(
                    old_workers,
                    target_workers,
                    self.state.baseline_max_workers
                )
                new_workers = max(10, new_workers)  # Ensure minimum

                self._make_adjustment(
                    AdjustmentType.THROTTLE,
                    f"CI health critical ({telemetry.ci_health_percent:.1f}%)",
                    {"old_mode": self.state.mode.value, "new_mode": GovernorMode.THROTTLED.value},
                    trigger,
                    threshold=f"CI < {self.teamplan.reset_triggers.ci_health_lt}%",
                    current_value=f"{telemetry.ci_health_percent:.1f}%",
                    before_settings={"mode": self.state.mode.value, "max_workers": old_workers},
                    after_settings={"mode": GovernorMode.THROTTLED.value, "max_workers": new_workers}
                )
                self.state.mode = GovernorMode.THROTTLED
                self.state.current_max_workers = new_workers
                self._set_cooldown()

        elif trigger == TriggerType.STALL_DETECTED:
            # Trigger replan
            self._make_adjustment(
                AdjustmentType.TRIGGER_REPLAN,
                "Stall detected - no progress",
                {"workers_active": telemetry.workers_active, "completed_1h": telemetry.tasks_completed_1h},
                trigger,
                threshold=f"Stall > {self.teamplan.reset_triggers.stall_minutes_gt} min",
                current_value="No completions while workers active"
            )
            self._set_cooldown()

        elif trigger == TriggerType.BUDGET_ALERT:
            # Freeze mode: stop new work
            if self.state.mode != GovernorMode.FROZEN:
                self._make_adjustment(
                    AdjustmentType.THROTTLE,
                    f"Budget critical (${telemetry.spend_today_usd:.2f})",
                    {"old_mode": self.state.mode.value, "new_mode": GovernorMode.FROZEN.value},
                    trigger,
                    threshold=f"Budget > 90% of ${self.teamplan.budget_policy.usd_per_day}",
                    current_value=f"${telemetry.spend_today_usd:.2f}",
                    before_settings={"mode": self.state.mode.value},
                    after_settings={"mode": GovernorMode.FROZEN.value}
                )
                self.state.mode = GovernorMode.FROZEN
                self._set_cooldown()

        self.state.last_adjustment_time = now

    async def _check_recovery(self, telemetry: TelemetrySnapshot):
        """Check if we can recover from throttled/frozen state."""

        if self.state.mode == GovernorMode.NORMAL:
            # Check if we can unfreeze tiers
            if self.state.frozen_tiers:
                if telemetry.ci_health_percent > 90 and telemetry.nack_rate() < 10:
                    tier = self.state.frozen_tiers.pop()
                    self._make_adjustment(
                        AdjustmentType.UNFREEZE_TIER,
                        "Quality recovered",
                        {"tier": tier, "ci_health": telemetry.ci_health_percent}
                    )
            return

        # Check recovery conditions
        thresholds = self.teamplan.reset_triggers
        can_recover = (
            telemetry.ci_health_percent >= thresholds.ci_health_lt + 5 and
            telemetry.nack_rate() <= thresholds.nack_rate_gt - 5 and
            telemetry.conflict_rate() <= thresholds.conflict_rate_gt - 5
        )

        if can_recover and self._check_hysteresis_recovery():
            self._make_adjustment(
                AdjustmentType.UNTHROTTLE,
                "Quality recovered - returning to normal",
                {
                    "old_mode": self.state.mode.value,
                    "ci_health": telemetry.ci_health_percent,
                    "nack_rate": telemetry.nack_rate()
                }
            )
            self.state.mode = GovernorMode.NORMAL
            # Restore caps from teamplan
            self.state.current_pr_cap_hour = self.teamplan.caps.pr_cap_per_hour
            self.state.current_open_pr_cap = self.teamplan.caps.open_pr_cap
            self.state.current_max_workers = self.teamplan.total_max_workers()

    def _check_hysteresis_recovery(self) -> bool:
        """Check if recovery conditions have persisted."""
        # Simplified: just check we've been stable for a while
        if self.state.last_adjustment_time:
            recovery_window = timedelta(minutes=self.teamplan.reorg_policy.hysteresis_minutes)
            if datetime.utcnow() - self.state.last_adjustment_time < recovery_window:
                return False
        return True

    def _make_adjustment(
        self,
        adj_type: AdjustmentType,
        reason: str,
        details: Dict,
        trigger: Optional[TriggerType] = None,
        # Explainability parameters (GPT 5.2 Fix 3)
        threshold: Optional[str] = None,
        current_value: Optional[str] = None,
        before_settings: Optional[Dict] = None,
        after_settings: Optional[Dict] = None
    ):
        """Record and notify about an adjustment."""
        # Calculate cooldown
        cooldown_minutes = self.teamplan.reorg_policy.hysteresis_minutes
        cooldown_until = datetime.utcnow() + timedelta(minutes=cooldown_minutes)
        next_eval_at = datetime.utcnow() + timedelta(minutes=5)  # Next eval in 5 min
        
        adjustment = Adjustment(
            timestamp=datetime.utcnow(),
            type=adj_type,
            reason=reason,
            details=details,
            trigger=trigger,
            # Explainability fields
            threshold=threshold,
            current_value=current_value,
            before_settings=before_settings,
            after_settings=after_settings,
            cooldown_until=cooldown_until,
            next_eval_at=next_eval_at
        )
        self.adjustments.append(adjustment)

        # Keep only last 100 adjustments
        if len(self.adjustments) > 100:
            self.adjustments = self.adjustments[-100:]

        logger.info(f"Governor adjustment: {adj_type.value} - {reason}")

        # Notify callback
        if self.on_adjustment:
            self.on_adjustment(adjustment)

    def get_state_summary(self) -> Dict:
        """Get current governor state for dashboard."""
        now = datetime.utcnow()

        # Calculate time until cooldown ends
        cooldown_remaining = None
        if self.state.cooldown_until and self.state.cooldown_until > now:
            cooldown_remaining = int((self.state.cooldown_until - now).total_seconds())

        # Calculate time until interval reset
        interval_remaining = None
        if self.state.interval_start:
            interval_elapsed = (now - self.state.interval_start).total_seconds() / 60
            interval_remaining = int((self.INTERVAL_MINUTES - interval_elapsed) * 60)

        return {
            "mode": self.state.mode.value,
            "frozen_tiers": self.state.frozen_tiers,
            "current_caps": {
                "pr_cap_hour": self.state.current_pr_cap_hour,
                "open_pr_cap": self.state.current_open_pr_cap,
                "max_workers": self.state.current_max_workers
            },
            "teamplan_caps": {
                "pr_cap_hour": self.teamplan.caps.pr_cap_per_hour,
                "open_pr_cap": self.teamplan.caps.open_pr_cap,
                "max_workers": self.teamplan.total_max_workers()
            },
            # Anti-thrash state
            "anti_thrash": {
                "in_cooldown": self._is_in_cooldown(),
                "cooldown_remaining_seconds": cooldown_remaining,
                "interval_remaining_seconds": interval_remaining,
                "adjustments_this_interval": self.state.adjustments_this_interval,
                "max_change_percent": int(self.MAX_CHANGE_PERCENT * 100),
                "baseline_caps": {
                    "pr_cap_hour": self.state.baseline_pr_cap_hour,
                    "open_pr_cap": self.state.baseline_open_pr_cap,
                    "max_workers": self.state.baseline_max_workers
                }
            },
            # Enhanced adjustment serialization (GPT 5.2 Fix 3)
            "recent_adjustments": [
                {
                    "timestamp": adj.timestamp.isoformat(),
                    "type": adj.type.value,
                    "reason": adj.reason,
                    "trigger": adj.trigger.value if adj.trigger else None,
                    # Explainability fields
                    "threshold": adj.threshold,
                    "current_value": adj.current_value,
                    "before_settings": adj.before_settings,
                    "after_settings": adj.after_settings,
                    "cooldown_until": adj.cooldown_until.isoformat() if adj.cooldown_until else None,
                    "next_eval_at": adj.next_eval_at.isoformat() if adj.next_eval_at else None,
                }
                for adj in self.adjustments[-10:]  # Last 10 adjustments
            ]
        }

    def is_tier_frozen(self, tier: int) -> bool:
        """Check if a risk tier is frozen for new PRs."""
        return tier in self.state.frozen_tiers

    def can_create_pr(self, tier: int = 0) -> bool:
        """Check if a PR can be created given current state."""
        if self.state.mode == GovernorMode.FROZEN:
            return False
        if self.is_tier_frozen(tier):
            return False
        # Would need telemetry to check PR caps
        return True
