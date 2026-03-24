"""
Reporter - Automated Reporting Engine (Loop C)

Implements the three reporting cadences from GPT_INSTRUCTIONS:
1. 15-minute Heartbeat (automated, no human needed)
2. 60-minute Ops Summary (automated, human optional)
3. Daily Executive Standup (human reviews)

Key feature: Narrative layer - "what changed since last check"
"""

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum

logger = logging.getLogger(__name__)


class ReportPeriod(str, Enum):
    HEARTBEAT = "heartbeat"  # 15-minute
    HOURLY = "hourly"        # 60-minute
    DAILY = "daily"          # 24-hour


@dataclass
class MetricsDelta:
    """Tracks changes between reporting periods."""
    prs_merged: int = 0
    prs_opened: int = 0
    tasks_completed: int = 0
    tasks_failed: int = 0
    ci_passes: int = 0
    ci_fails: int = 0
    cost_usd: float = 0.0

    # Previous values for delta calculation
    prev_prs_merged: int = 0
    prev_tasks_completed: int = 0
    prev_ci_passes: int = 0
    prev_ci_fails: int = 0
    prev_cost_usd: float = 0.0


@dataclass
class OpsReport:
    timestamp: str
    period: ReportPeriod
    content: str
    metrics: dict
    narrative: str = ""  # What changed since last check
    decisions_needed: list[dict] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)


class Reporter:
    """
    Automated Reporting Engine with narrative layer.

    Generates reports at three cadences:
    - Heartbeat (15 min): System state snapshot, deltas, violations
    - Hourly (60 min): Ops summary for glancing during the day
    - Daily: Executive standup - approve decisions, not manage work
    """

    def __init__(self, repo_path: str = "."):
        self.repo_path = repo_path
        self.reports_dir = os.path.join(repo_path, "reports")

        # Create report directories
        for subdir in ["heartbeat", "hourly", "daily"]:
            os.makedirs(os.path.join(self.reports_dir, subdir), exist_ok=True)

        # State tracking for narrative generation
        self._last_metrics: dict = {}
        self._last_heartbeat: datetime | None = None
        self._last_hourly: datetime | None = None
        self._last_daily: datetime | None = None

        # Running state
        self._running = False
        self._tasks: list[asyncio.Task] = []

        # Callbacks
        self._on_report: Callable[[OpsReport], None] | None = None
        self._get_metrics: Callable[[], dict] | None = None
        self._get_decisions: Callable[[], list[dict]] | None = None

    def set_callbacks(
        self,
        get_metrics: Callable[[], dict],
        get_decisions: Callable[[], list[dict]] | None = None,
        on_report: Callable[[OpsReport], None] | None = None
    ):
        """Set callback functions for dynamic data retrieval."""
        self._get_metrics = get_metrics
        self._get_decisions = get_decisions
        self._on_report = on_report

    async def start(self):
        """Start automated report generation loops."""
        if self._running:
            return
        self._running = True

        # Start the three reporting loops
        self._tasks = [
            asyncio.create_task(self._heartbeat_loop()),
            asyncio.create_task(self._hourly_loop()),
            asyncio.create_task(self._daily_loop()),
        ]
        logger.info("Reporter started - 3 reporting loops active")

    async def stop(self):
        """Stop automated report generation."""
        self._running = False
        for task in self._tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks = []
        logger.info("Reporter stopped")

    async def _heartbeat_loop(self):
        """15-minute heartbeat loop - system state + deltas."""
        while self._running:
            try:
                await asyncio.sleep(15 * 60)  # 15 minutes
                report = self.generate_heartbeat_report()
                if self._on_report:
                    self._on_report(report)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Heartbeat loop error: {e}")
                await asyncio.sleep(60)

    async def _hourly_loop(self):
        """60-minute ops summary loop."""
        while self._running:
            try:
                # Wait until next hour boundary
                now = datetime.now(UTC)
                next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
                wait_seconds = (next_hour - now).total_seconds()
                await asyncio.sleep(wait_seconds)

                report = self.generate_hourly_report()
                if self._on_report:
                    self._on_report(report)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Hourly loop error: {e}")
                await asyncio.sleep(60)

    async def _daily_loop(self):
        """Daily executive standup report."""
        while self._running:
            try:
                # Wait until midnight
                now = datetime.now(UTC)
                tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                wait_seconds = (tomorrow - now).total_seconds()
                await asyncio.sleep(wait_seconds)

                report = self.generate_daily_report()
                if self._on_report:
                    self._on_report(report)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Daily loop error: {e}")
                await asyncio.sleep(60)

    def _get_current_metrics(self) -> dict:
        """Get current metrics from callback or empty dict."""
        if self._get_metrics:
            return self._get_metrics()
        return {}

    def _get_pending_decisions(self) -> list[dict]:
        """Get pending decisions from callback or empty list."""
        if self._get_decisions:
            return self._get_decisions()
        return []

    def _calculate_deltas(self, current: dict, previous: dict) -> dict:
        """Calculate metric deltas between periods."""
        deltas = {}
        for key in ['prs_merged', 'prs_opened', 'tasks_completed', 'tasks_failed',
                    'ci_passes', 'ci_fails', 'cost_usd', 'spec_completed']:
            curr_val = current.get(key, 0)
            prev_val = previous.get(key, 0)
            deltas[f"{key}_delta"] = curr_val - prev_val
            deltas[key] = curr_val
        return deltas

    def _generate_narrative(self, deltas: dict, period: str) -> str:
        """Generate narrative layer - what changed since last check."""
        changes = []

        # PR activity
        pr_delta = deltas.get('prs_merged_delta', 0)
        if pr_delta > 0:
            changes.append(f"+{pr_delta} PRs merged")
        elif pr_delta < 0:
            changes.append("PR merge rate decreased")

        # Task completion
        task_delta = deltas.get('tasks_completed_delta', 0)
        if task_delta > 0:
            changes.append(f"+{task_delta} tasks completed")

        # CI health changes
        ci_pass_delta = deltas.get('ci_passes_delta', 0)
        ci_fail_delta = deltas.get('ci_fails_delta', 0)
        if ci_fail_delta > 0:
            changes.append(f"+{ci_fail_delta} CI failures (attention needed)")
        elif ci_pass_delta > 0 and ci_fail_delta == 0:
            changes.append("CI health stable")

        # Cost
        cost_delta = deltas.get('cost_usd_delta', 0)
        if cost_delta > 5:
            changes.append(f"+${cost_delta:.2f} spend")

        if not changes:
            return f"No significant changes since last {period}."

        return "Since last check: " + ", ".join(changes) + "."

    def _detect_risks(self, metrics: dict) -> list[str]:
        """Detect risks from current metrics."""
        risks = []

        # CI health risk
        ci_health = metrics.get('ci_health', 1.0)
        if isinstance(ci_health, (int, float)):
            if ci_health < 0.85:
                risks.append(f"CI health at {ci_health:.1%} - below 85% threshold")

        # Retry rate risk
        retry_rate = metrics.get('retry_rate', 0)
        if retry_rate > 0.25:
            risks.append(f"Retry rate at {retry_rate:.1%} - above 25% threshold")

        # Budget risk
        budget_percent = metrics.get('budget_percent', 0)
        if budget_percent > 0.8:
            risks.append(f"Budget at {budget_percent:.1%} of daily cap")

        # Stall detection
        if metrics.get('stall_detected', False):
            risks.append("Delivery stall detected - no merges while queue non-empty")

        return risks

    def generate_heartbeat_report(self, metrics: dict | None = None,
                                   governor_actions: list[str] | None = None) -> OpsReport:
        """
        Generate 15-minute heartbeat report.

        No human needed. System writes:
        - Current governor state
        - Key deltas since last 15 min
        - Any violations and what it did about them
        """
        now = datetime.now(UTC)
        timestamp = now.strftime("%Y-%m-%d-%H-%M")

        if metrics is None:
            metrics = self._get_current_metrics()
        if governor_actions is None:
            governor_actions = []

        # Calculate deltas
        deltas = self._calculate_deltas(metrics, self._last_metrics)
        narrative = self._generate_narrative(deltas, "15 minutes")
        risks = self._detect_risks(metrics)

        # Format governor actions
        actions_text = "\n".join(f"- {a}" for a in governor_actions) if governor_actions else "- No actions taken"

        # Format risks
        risks_text = "\n".join(f"- {r}" for r in risks) if risks else "- None detected"

        content = f"""# Heartbeat: {now.strftime('%Y-%m-%d %H:%M')}

## Narrative
{narrative}

## System State
- **Governor**: {metrics.get('governor_state', 'NORMAL')}
- **CI Health**: {metrics.get('ci_health', 1.0):.1%}
- **Active Workers**: {metrics.get('active_workers', 0)}
- **Open PRs**: {metrics.get('prs_open', 0)}

## Deltas (Last 15m)
- PRs Merged: {deltas.get('prs_merged_delta', 0):+d}
- Tasks Completed: {deltas.get('tasks_completed_delta', 0):+d}
- CI Passes/Fails: {deltas.get('ci_passes_delta', 0):+d}/{deltas.get('ci_fails_delta', 0):+d}
- Cost: ${deltas.get('cost_usd_delta', 0):+.2f}

## Governor Actions
{actions_text}

## Risks & Violations
{risks_text}
"""

        # Save report
        filename = os.path.join(self.reports_dir, "heartbeat", f"{timestamp}.md")
        with open(filename, "w") as f:
            f.write(content)

        # Update state
        self._last_metrics = metrics.copy()
        self._last_heartbeat = now

        report = OpsReport(
            timestamp=timestamp,
            period=ReportPeriod.HEARTBEAT,
            content=content,
            metrics=metrics,
            narrative=narrative,
            risks=risks
        )

        logger.info(f"Heartbeat report generated: {timestamp}")
        return report

    def generate_hourly_report(self, metrics: dict | None = None,
                                governor_actions: list[str] | None = None) -> OpsReport:
        """
        Generate 60-minute ops summary.

        Human optional. The one you glance at during the day.
        """
        now = datetime.now(UTC)
        timestamp = now.strftime("%Y-%m-%d-%H")

        if metrics is None:
            metrics = self._get_current_metrics()
        if governor_actions is None:
            governor_actions = []

        # Calculate deltas
        deltas = self._calculate_deltas(metrics, self._last_metrics)
        narrative = self._generate_narrative(deltas, "hour")
        risks = self._detect_risks(metrics)
        decisions = self._get_pending_decisions()

        # Build content
        actions_text = "\n".join(f"- {a}" for a in governor_actions) if governor_actions else "- No actions taken"
        risks_text = "\n".join(f"- {r}" for r in risks) if risks else "- None detected"

        # Decisions section
        if decisions:
            decisions_text = "\n".join(f"- [ ] {d.get('title', 'Decision')}: {d.get('description', '')}"
                                       for d in decisions[:3])
        else:
            decisions_text = "- None pending"

        content = f"""# Ops Report: {now.strftime('%Y-%m-%d %H:00')}

## Narrative
{narrative}

## System Health
- **CI Health**: {metrics.get('ci_health', 1.0):.1%}
- **Governor State**: {metrics.get('governor_state', 'NORMAL')}
- **Active Workers**: {metrics.get('active_workers', 0)}

## Throughput (Last Hour)
| Metric | Value | Delta |
|--------|-------|-------|
| PRs Merged | {metrics.get('prs_merged', 0)} | {deltas.get('prs_merged_delta', 0):+d} |
| PRs Opened | {metrics.get('prs_open', 0)} | {deltas.get('prs_opened_delta', 0):+d} |
| Tasks Completed | {metrics.get('tasks_completed', 0)} | {deltas.get('tasks_completed_delta', 0):+d} |
| Burn Rate | ${metrics.get('burn_rate', 0):.2f}/hr | - |

## Governor Actions
{actions_text}

## Risks & Alerts
{risks_text}

## Decisions Needed
{decisions_text}
"""

        # Save report
        filename = os.path.join(self.reports_dir, "hourly", f"{timestamp}.md")
        with open(filename, "w") as f:
            f.write(content)

        # Update state
        self._last_metrics = metrics.copy()
        self._last_hourly = now

        report = OpsReport(
            timestamp=timestamp,
            period=ReportPeriod.HOURLY,
            content=content,
            metrics=metrics,
            narrative=narrative,
            decisions_needed=decisions,
            risks=risks
        )

        logger.info(f"Hourly report generated: {timestamp}")
        return report

    def generate_daily_report(self, metrics: dict | None = None) -> OpsReport:
        """
        Generate daily executive standup report.

        Human reviews this. Standup should be "approve decisions, not figure out what happened."

        Format (one screen):
        1. What shipped (merged + deployed)
        2. What is blocked (top 3 blockers)
        3. Quality status (CI/staging/security)
        4. Budget status (burn + forecast)
        5. Decisions required (max 3)
        6. Plan for next 24h (high-level)
        """
        now = datetime.now(UTC)
        date_str = now.strftime("%Y-%m-%d")

        if metrics is None:
            metrics = self._get_current_metrics()

        decisions = self._get_pending_decisions()
        risks = self._detect_risks(metrics)

        # Build blockers list
        blockers = []
        if metrics.get('ci_health', 1.0) < 0.85:
            blockers.append("CI health below threshold - investigate failing tests")
        if metrics.get('stall_detected', False):
            blockers.append("Delivery stall - no merges while queue has work")
        if metrics.get('budget_percent', 0) > 0.9:
            blockers.append("Budget cap imminent - approve increase or pause")

        blockers_text = "\n".join(f"{i+1}. {b}" for i, b in enumerate(blockers[:3])) if blockers else "1. None"

        # Decisions (max 3)
        if decisions:
            decisions_text = "\n".join(f"- [ ] **{d.get('title', 'Decision')}**: {d.get('description', '')}"
                                       for d in decisions[:3])
        else:
            decisions_text = "- None pending"

        # Quality status
        ci_status = "PASSING" if metrics.get('ci_health', 1.0) >= 0.85 else "FAILING"
        security_status = metrics.get('security_status', 'PASSING')
        staging_status = metrics.get('staging_status', 'HEALTHY')

        content = f"""# Executive Standup: {date_str}

## 1. What Shipped (Last 24h)
- **Merged PRs**: {metrics.get('daily_merged', metrics.get('prs_merged', 0))}
- **Features Completed**: {metrics.get('daily_features', metrics.get('spec_completed', 0))}
- **SPEC Progress**: {metrics.get('spec_percent', 0):.0f}%

## 2. Blockers (Top 3)
{blockers_text}

## 3. Quality Status
| Check | Status |
|-------|--------|
| CI Pass Rate | {metrics.get('ci_health', 1.0):.1%} ({ci_status}) |
| Security Scans | {security_status} |
| Staging Health | {staging_status} |

## 4. Budget Status
- **Spent Today**: ${metrics.get('spent_today', metrics.get('cost_usd', 0)):.2f}
- **Daily Cap**: ${metrics.get('daily_cap', 50):.2f}
- **Burn Rate**: ${metrics.get('burn_rate', 0):.2f}/hr
- **Forecast**: {metrics.get('forecast_eta', 'Within budget')}

## 5. Decisions Required (Max 3)
{decisions_text}

## 6. Plan for Next 24h
- Continue current SPEC execution
- Monitor CI health and address any failures
- Review and merge Tier 2 PRs as ready

---
*Human actions allowed: Approve Tier2 PRs, Adjust budget/risk tolerance, Pause/resume swarm, Replan from PRD, Ship now*
"""

        # Save report
        filename = os.path.join(self.reports_dir, "daily", f"{date_str}.md")
        with open(filename, "w") as f:
            f.write(content)

        # Update state
        self._last_daily = now

        report = OpsReport(
            timestamp=date_str,
            period=ReportPeriod.DAILY,
            content=content,
            metrics=metrics,
            decisions_needed=decisions,
            risks=risks
        )

        logger.info(f"Daily report generated: {date_str}")
        return report

    def get_latest_report(self, period: ReportPeriod) -> str | None:
        """Get the latest report content for a given period."""
        subdir = period.value
        reports_path = os.path.join(self.reports_dir, subdir)

        if not os.path.exists(reports_path):
            return None

        files = sorted(os.listdir(reports_path), reverse=True)
        if not files:
            return None

        with open(os.path.join(reports_path, files[0])) as f:
            return f.read()

    def list_reports(self, period: ReportPeriod, limit: int = 24) -> list[dict]:
        """List available reports for a given period."""
        subdir = period.value
        reports_path = os.path.join(self.reports_dir, subdir)

        if not os.path.exists(reports_path):
            return []

        reports = []
        for filename in sorted(os.listdir(reports_path), reverse=True)[:limit]:
            if filename.endswith(".md"):
                filepath = os.path.join(reports_path, filename)
                with open(filepath) as f:
                    content = f.read()
                reports.append({
                    "filename": filename,
                    "timestamp": filename.replace(".md", ""),
                    "content": content
                })

        return reports
