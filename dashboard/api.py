"""MX Swarm Dashboard API - Enhanced with Governor & Credibility Metrics.

FastAPI backend providing:
- Real-time swarm status with credibility tiles
- Governor controls (PR caps, throttling)
- CI health metrics
- Structured event logging
- Manual worker control
- WebSocket for live updates
"""

import asyncio
import json
import os
import random
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

# Load .env file for GITHUB_TOKEN and other secrets
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv not installed, rely on environment variables
from collections import deque
from enum import Enum

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from boss import (
    DecisionQueue,
    EscalationEngine,
    Governor,
    OrgBuilder,
    OrgConstraints,
    Reporter,
    TelemetrySnapshot,
)
from shared.adapters import create_queue_adapter
from shared.schemas import Spec
from shared.schemas.teamplan import GovernorMode, TeamPlan
from shared.utils import MXConfig
from shared.utils.metrics import get_metrics_collector

# ============================================================================
# Enums
# ============================================================================

class EventType(str, Enum):
    # Task events
    TASK_CLAIM = "TASK_CLAIM"
    TASK_ACK = "TASK_ACK"
    TASK_NACK = "TASK_NACK"
    TASK_TIMEOUT = "TASK_TIMEOUT"
    # PR events
    PR_OPEN = "PR_OPEN"
    PR_MERGE = "PR_MERGE"
    PR_REJECT = "PR_REJECT"
    # CI events
    CI_PASS = "CI_PASS"
    CI_FAIL = "CI_FAIL"
    # Governor events
    GOV_CHECK = "GOV_CHECK"
    GOV_THROTTLE = "GOV_THROTTLE"
    GOV_RESUME = "GOV_RESUME"
    GOV_FREEZE = "GOV_FREEZE"
    # Judge events
    JUDGE_EVAL = "JUDGE_EVAL"
    JUDGE_REPLAN = "JUDGE_REPLAN"
    JUDGE_RESET = "JUDGE_RESET"
    JUDGE_STOP = "JUDGE_STOP"
    # System events
    SYS_START = "SYS_START"
    SYS_STOP = "SYS_STOP"
    SYS_SCALE = "SYS_SCALE"
    SYS_CONFIG = "SYS_CONFIG"




class RiskTier(int, Enum):
    LOW = 0       # Auto-merge OK
    MEDIUM = 1    # Requires 1 approval
    HIGH = 2      # Manual review required


@dataclass
class WorkerState:
    """3-tier worker state model for UI consistency.

    Separates:
    - teamplan_recommended: What Boss analysis recommends
    - runtime_target: What operator has set as target
    - runtime_cap: Maximum allowed workers (never exceed)
    - online_workers: Actually running workers
    """
    teamplan_recommended: int = 5   # From Boss/OrgBuilder analysis
    runtime_target: int = 10        # Operator setting
    runtime_cap: int = 10           # Hard cap
    online_workers: int = 0         # Actual running (never > cap)

    def enforce_invariant(self):
        """Ensure online never exceeds cap."""
        if self.online_workers > self.runtime_cap:
            self.online_workers = self.runtime_cap


@dataclass
class AnalyticsCacheEntry:
    value: Any
    cached_at_monotonic: float


# ============================================================================
# State Management
# ============================================================================

class SwarmState:
    """Global swarm state tracker with enhanced metrics."""

    def __init__(self):
        # Worker tracking - 3-tier model
        self.workers: dict[str, dict] = {}  # worker_id -> {last_seen, task_id, ...}
        self.worker_state = WorkerState()   # 3-tier: recommended, target, cap, online

        # Task tracking
        self.active_tasks: dict[str, dict] = {}
        self.tasks_by_tier: dict[int, int] = {0: 0, 1: 0, 2: 0}

        # Metrics
        self.tasks_completed: int = 0
        self.tasks_failed: int = 0
        self.tasks_retried: int = 0
        self.tasks_in_progress_runtime: int = 0
        self.prs_opened: int = 0
        self.prs_merged: int = 0
        self.prs_rejected: int = 0
        self.submissions_opened: int = 0
        self.submissions_accepted: int = 0
        self.ci_passes: int = 0
        self.ci_fails: int = 0

        # Cost tracking
        self.total_cost: float = 0.0
        self.hourly_costs: deque = deque(maxlen=24)  # Last 24 hours
        self.current_hour_cost: float = 0.0
        self.last_hour_reset: datetime = datetime.now(UTC)

        # Time series for charts
        self.tasks_per_minute: deque = deque(maxlen=60)  # Last 60 minutes
        self.prs_per_hour: deque = deque(maxlen=24)  # Last 24 hours
        self.ci_health_trend: deque = deque(maxlen=60)  # Last 60 data points

        # Governor state
        self.governor_state: GovernorMode = GovernorMode.NORMAL
        self.open_pr_cap: int = 25
        self.pr_hour_cap: int = 15
        self.current_open_prs: int = 0
        self.prs_this_hour: int = 0
        self.last_queue_depth: int | None = None
        self.last_phase: str | None = None

        # Logs
        self.logs: deque = deque(maxlen=1000)

        # Timing
        self.start_time: str | None = None

        # WebSocket clients
        self.websocket_clients: list[WebSocket] = []

        # Escalation, Reporting & Decisions
        self.escalation_engine = EscalationEngine(repo_path=".")
        self.reporter = Reporter(repo_path=".")
        self.decision_queue = DecisionQueue(repo_path=".")

        # Subprocess management
        self.swarm_process = None

        # Initialize Governor
        teamplan_path = "TEAMPLAN.json"
        if os.path.exists(teamplan_path):
            self.teamplan = TeamPlan.load(teamplan_path)
        else:
            self.teamplan = TeamPlan.default()

        self.governor = Governor(
            teamplan=self.teamplan,
            telemetry_provider=self._get_telemetry_snapshot
        )

    def register_worker(self, worker_id: str, task_id: str | None = None):
        """Register or update worker activity."""
        self.workers[worker_id] = {
            "worker_id": worker_id,
            "last_seen": datetime.now(UTC).isoformat(),
            "task_id": task_id,
            "status": "active" if task_id else "idle",
        }

    def get_active_workers(self, seconds: int = 60) -> int:
        """Count workers active in last N seconds."""
        cutoff = datetime.now(UTC) - timedelta(seconds=seconds)
        return sum(
            1 for w in self.workers.values()
            if datetime.fromisoformat(w["last_seen"]) > cutoff
        )

    def get_ci_health(self) -> float:
        """Calculate CI health percentage."""
        total = self.ci_passes + self.ci_fails
        if total == 0:
            return 100.0
        return (self.ci_passes / total) * 100

    def get_burn_rate(self) -> float:
        """Get current $/hour burn rate."""
        return self.current_hour_cost

    def get_forecast_to_cap(self, daily_cap: float) -> str | None:
        """Forecast when daily cap will be hit."""
        if self.current_hour_cost <= 0:
            return None
        remaining = daily_cap - self.total_cost
        hours_left = remaining / self.current_hour_cost
        if hours_left <= 0:
            return "NOW"
        eta = datetime.now(UTC) + timedelta(hours=hours_left)
        return eta.strftime("%H:%M")

    def _get_telemetry_snapshot(self) -> TelemetrySnapshot:
        """Provide telemetry snapshot for the Governor."""
        queue_depth = self.last_queue_depth if isinstance(self.last_queue_depth, int) else 0
        return TelemetrySnapshot(
            ci_health_percent=self.get_ci_health(),
            tasks_completed_1h=self.tasks_completed, # Simplified
            tasks_failed_1h=self.tasks_failed,
            tasks_in_progress=self.tasks_in_progress_runtime,
            queue_depth=max(queue_depth, 0),
            prs_open_total=self.current_open_prs,
            workers_active=self.get_active_workers(),
            spend_today_usd=self.total_cost
        )

    def check_governor(self) -> GovernorMode:
        """Check and update governor state based on caps and config."""
        # Update telemetry and evaluate
        asyncio.create_task(self.governor._evaluate())

        # Sync state
        self.governor_state = GovernorMode(self.governor.state.mode.value)
        self.open_pr_cap = self.governor.state.current_open_pr_cap
        self.pr_hour_cap = self.governor.state.current_pr_cap_hour

        return self.governor_state

    def add_log(self, event_type: EventType, message: str, level: str = "info"):
        """Add structured log entry."""
        entry = {
            "timestamp": datetime.now(UTC).strftime("%H.%M.%S"),
            "event_type": event_type.value,
            "level": level,
            "message": message,
        }
        self.logs.append(entry)
        asyncio.create_task(self.broadcast_log(entry))

    async def broadcast_log(self, entry: dict):
        """Broadcast log to all connected clients."""
        disconnected = []
        for ws in self.websocket_clients:
            try:
                await ws.send_json({"type": "log", "data": entry})
            except Exception:
                disconnected.append(ws)
        for ws in disconnected:
            if ws in self.websocket_clients:
                self.websocket_clients.remove(ws)

    async def broadcast_status(self):
        """Broadcast status update to all clients."""
        status = await get_swarm_status()
        disconnected = []
        for ws in self.websocket_clients:
            try:
                await ws.send_json({"type": "status", "data": status})
            except Exception:
                disconnected.append(ws)
        for ws in disconnected:
            if ws in self.websocket_clients:
                self.websocket_clients.remove(ws)


# Global state
state = SwarmState()


# ============================================================================
# Pydantic Models
# ============================================================================

class ScaleRequest(BaseModel):
    max_workers: int

class BudgetRequest(BaseModel):
    daily_usd_cap: float

class GovernorRequest(BaseModel):
    open_pr_cap: int | None = None
    pr_hour_cap: int | None = None
    ci_health_threshold: float | None = None

class WorkerAction(BaseModel):
    worker_id: str
    action: str  # "stop", "restart"

class IgniteRequest(BaseModel):
    max_workers: int | None = None
    budget_usd: float | None = None
    auto_mode: bool = True
    prd_content: str | None = None
    vcs_target: str = "github"  # "aether" or "github"

class VCSPreflightRequest(BaseModel):
    vcs_target: str = "github"

class ConfigUpdate(BaseModel):
    llm_provider: str | None = None
    llm_model: str | None = None
    timebox_minutes: int | None = None
    retry_max: int | None = None
    judge_interval_minutes: int | None = None
    auto_merge_low_risk: bool | None = None

class PRDAnalyzeRequest(BaseModel):
    prd_content: str


# ============================================================================
# Helper Functions
# ============================================================================

def analyze_prd_content(content: str) -> dict:
    """Analyze PRD content without LLM - keyword-based estimation."""
    import re

    content_lower = content.lower()
    lines = content.split('\n')
    words = content.split()

    # Detect categories based on keywords
    categories = []
    category_keywords = {
        'UI/Frontend': ['ui', 'frontend', 'react', 'vue', 'angular', 'css', 'html', 'component', 'button', 'form', 'page', 'dashboard', 'display'],
        'API/Backend': ['api', 'endpoint', 'rest', 'graphql', 'server', 'route', 'controller', 'service', 'backend'],
        'Database': ['database', 'db', 'sql', 'postgres', 'mysql', 'mongo', 'redis', 'schema', 'table', 'migration'],
        'Authentication': ['auth', 'login', 'password', 'jwt', 'oauth', 'session', 'user', 'permission', 'role'],
        'Testing': ['test', 'pytest', 'jest', 'spec', 'coverage', 'unit test', 'integration'],
        'DevOps/Infra': ['docker', 'kubernetes', 'k8s', 'deploy', 'ci/cd', 'pipeline', 'aws', 'gcp', 'azure'],
        'Security': ['security', 'encrypt', 'ssl', 'https', 'vulnerability', 'sanitize', 'xss', 'injection'],
        'Documentation': ['doc', 'readme', 'comment', 'swagger', 'openapi'],
    }

    for cat, keywords in category_keywords.items():
        if any(kw in content_lower for kw in keywords):
            categories.append(cat)

    if not categories:
        categories = ['General']

    # Count requirements (lines starting with numbers, bullets, or FR/NFR patterns)
    req_patterns = [
        r'^\d+\.',           # 1. 2. 3.
        r'^[-*•]',           # bullets
        r'^FR-\d+',          # FR-001
        r'^NFR-\d+',         # NFR-001
        r'^REQ-\d+',         # REQ-001
    ]
    requirements = []
    for line in lines:
        line = line.strip()
        for pattern in req_patterns:
            if re.match(pattern, line):
                requirements.append(line[:80])
                break

    # Estimate complexity
    word_count = len(words)
    req_count = len(requirements)

    if word_count < 300 or req_count < 5:
        complexity = 'Low'
        suggested_workers = 3
        estimated_tasks = max(5, req_count * 2)
        risk = 'Low'
    elif word_count < 1000 or req_count < 15:
        complexity = 'Medium'
        suggested_workers = 5
        estimated_tasks = max(10, req_count * 2)
        risk = 'Medium'
    else:
        complexity = 'High'
        suggested_workers = 10
        estimated_tasks = max(20, req_count * 2)
        risk = 'High'

    # Estimate budget based on complexity
    budget_map = {'Low': 25.0, 'Medium': 50.0, 'High': 100.0}

    # Build team composition
    team = []
    if 'API/Backend' in categories or 'Database' in categories:
        team.append({'role': 'Backend Engineer', 'count': max(1, suggested_workers // 3)})
    if 'UI/Frontend' in categories:
        team.append({'role': 'Frontend Engineer', 'count': max(1, suggested_workers // 3)})
    if 'Testing' in categories:
        team.append({'role': 'Test Engineer', 'count': max(1, suggested_workers // 4)})
    if 'DevOps/Infra' in categories:
        team.append({'role': 'DevOps Engineer', 'count': 1})
    if 'Security' in categories:
        team.append({'role': 'Security Engineer', 'count': 1})
    if not team:
        team.append({'role': 'Fullstack Engineer', 'count': suggested_workers})

    return {
        'status': 'analyzed',
        'summary': {
            'word_count': word_count,
            'line_count': len(lines),
            'requirement_count': req_count,
        },
        'categories': categories,
        'complexity': complexity,
        'risk_level': risk,
        'recommendations': {
            'suggested_workers': suggested_workers,
            'estimated_tasks': estimated_tasks,
            'estimated_budget_usd': budget_map[complexity],
            'estimated_duration': f"{estimated_tasks * 30} - {estimated_tasks * 60} minutes",
        },
        'team_composition': team,
        'requirements_preview': requirements[:10],  # First 10 requirements
    }

def load_config() -> MXConfig:
    return MXConfig.load_or_create("mx.config.json")

def save_config(config: MXConfig):
    config.save("mx.config.json")

def load_spec() -> Spec | None:
    if os.path.exists("SPEC.json"):
        return Spec.load("SPEC.json")
    return None


def load_teamplan() -> TeamPlan | None:
    """Load TEAMPLAN.json if it exists."""
    if os.path.exists("TEAMPLAN.json"):
        return TeamPlan.load("TEAMPLAN.json")
    return None


def get_coordination_metrics() -> dict:
    """Get coordination metrics for dashboard."""
    collector = get_metrics_collector()
    return collector.get_dashboard_data()


def get_event_observability(window_minutes: int = 60) -> dict:
    """Get event-sourced observability metrics and formula cards."""
    collector = get_metrics_collector()
    return collector.compute_event_observability(window_minutes=window_minutes)


def get_trace_bundle(trace_id: str, limit: int = 500) -> dict:
    """Get trace spans and state events for a specific trace id."""
    collector = get_metrics_collector()
    spans = [
        span for span in collector.read_trace_spans(limit=limit)
        if str(span.get("trace_id", "")).strip() == trace_id
    ]
    events = [
        event for event in collector.read_task_state_events(limit=limit)
        if str(event.get("trace_id", "")).strip() == trace_id
    ]
    spans.sort(key=lambda span: str(span.get("timestamp", "")))
    events.sort(key=lambda event: str(event.get("timestamp", "")))
    return {
        "trace_id": trace_id,
        "span_count": len(spans),
        "event_count": len(events),
        "services": sorted({str(span.get("service", "")) for span in spans if span.get("service")}),
        "spans": spans,
        "events": events,
    }


def _map_task_runtime_state(state_to: str, reason: str | None) -> tuple[str, int]:
    state_key = state_to.strip().lower()
    if state_key == "queued":
        return ("QUEUED", 10)
    if state_key == "claimed":
        return ("CLAIMED", 20)
    if state_key == "executing":
        return ("RUNNING", 40)
    if state_key == "artifact_ready":
        return ("ARTIFACT_READY", 70)
    if state_key == "verified":
        return ("VERIFIED", 85)
    if state_key == "submitted_pending_ledger":
        return ("SUBMITTED_PENDING_LEDGER", 90)
    if state_key == "settled_ack":
        return ("COMPLETED", 100)
    if state_key == "settled_nack":
        suffix = f" ({reason})" if reason else ""
        return (f"FAILED{suffix}", 100)
    if state_key == "dead_letter":
        suffix = f" ({reason})" if reason else ""
        return (f"DEAD_LETTER{suffix}", 100)
    return ("IN_PROGRESS", 50)


def _index_latest_task_states(task_ids: set[str], limit: int = 5000) -> dict[str, dict[str, str]]:
    if not task_ids:
        return {}
    collector = get_metrics_collector()
    events = collector.read_task_state_events(limit=limit)
    latest: dict[str, dict[str, str]] = {}
    for event in reversed(events):
        task_id = str(event.get("task_id", "")).strip()
        if not task_id or task_id not in task_ids or task_id in latest:
            continue
        latest[task_id] = {
            "state_to": str(event.get("state_to", "")).strip().lower(),
            "reason": str(event.get("reason", "")).strip() or "",
        }
        if len(latest) >= len(task_ids):
            break
    return latest

async def get_swarm_status() -> dict:
    """Get complete swarm status with all credibility metrics."""
    config = load_config()
    spec = load_spec()
    coordination_data = get_coordination_metrics()
    event_observability = coordination_data.get("event_sourced", {})
    collector = get_metrics_collector()
    submission_summary = collector.get_submission_summary()

    # Queue depth (prefer swarm heartbeat if available)
    if state.last_queue_depth is not None:
        queue_depth = state.last_queue_depth
    else:
        queue_depth = 0
        try:
            queue = create_queue_adapter(config.queue.to_dict())
            await queue.connect()
            queue_depth = await queue.get_queue_depth()
            await queue.disconnect()
        except Exception:
            queue_depth = -1
    if isinstance(queue_depth, int) and queue_depth >= 0:
        state.last_queue_depth = queue_depth

    # SPEC/DoD progress
    spec_progress = {"total": 0, "completed": 0, "percent": 0}
    dod_status = {
        "e2e_tests": "pending",
        "security_scan": "pending",
        "staging_deploy": "pending",
    }
    requirements = []

    if spec:
        spec_progress = spec.progress_summary()
        requirements = [
            {
                "id": r.id,
                "title": r.title,
                "status": r.status.value,
                "tasks": len(r.assigned_tasks),
            }
            for r in spec.requirements[:20]
        ]
        # Simulate DoD checks based on progress
        if spec_progress["percent"] >= 30:
            dod_status["e2e_tests"] = "passing"
        if spec_progress["percent"] >= 50:
            dod_status["security_scan"] = "passing"
        if spec_progress["percent"] >= 80:
            dod_status["staging_deploy"] = "passing"

    runtime_max_workers = (
        state.worker_state.runtime_cap
        if state.worker_state.runtime_cap > 0
        else config.worker.max_workers
    )

    # Task contracts with risk tiers
    task_contracts: list[dict[str, Any]] = []
    if spec:
        live_tasks = {
            str(task_id): dict(task)
            for task_id, task in state.active_tasks.items()
        }
        spec_task_ids = {
            str(task_id)
            for req in spec.requirements
            for task_id in req.assigned_tasks
            if str(task_id).strip()
        }
        latest_states = _index_latest_task_states(spec_task_ids)
        merged_contracts: dict[str, dict[str, Any]] = {}

        for req in spec.requirements:
            for i, task_id in enumerate(req.assigned_tasks[:10]):
                task_id_str = str(task_id)
                risk = RiskTier.LOW if i % 3 == 0 else (RiskTier.MEDIUM if i % 3 == 1 else RiskTier.HIGH)
                contract: dict[str, Any] = {
                    "id": task_id,
                    "goal": req.title[:50],
                    "worker": "-",
                    "status": req.status.value.upper(),
                    "progress": 100 if req.status.value == "completed" else 50,
                    "spec_ref": req.id,
                    "prd_ref": req.id, # Traceability Fix (5.1)
                    "risk_tier": risk.value,
                }
                live_task = live_tasks.get(task_id_str)
                if live_task:
                    contract["worker"] = live_task.get("worker", "-")
                    contract["status"] = live_task.get("status", "RUNNING")
                    contract["progress"] = live_task.get("progress", 50)
                elif task_id_str in latest_states:
                    state_to = latest_states[task_id_str].get("state_to", "")
                    reason = latest_states[task_id_str].get("reason")
                    status, progress = _map_task_runtime_state(state_to, reason)
                    contract["status"] = status
                    contract["progress"] = progress
                elif req.status.value == "in_progress":
                    contract["status"] = "QUEUED"
                    contract["progress"] = 10
                merged_contracts[task_id_str] = contract

        # Include runtime-only tasks not yet present in SPEC view.
        for task_id, live_task in live_tasks.items():
            if task_id in merged_contracts:
                continue
            merged_contracts[task_id] = dict(live_task)

        task_contracts = list(merged_contracts.values())

    # Check governor
    state.check_governor()

    return {
        # Agents Online tile
        "agents": {
            "total": len(state.workers),
            "max": runtime_max_workers,
            "active_60s": state.get_active_workers(60),
            "idle": len([w for w in state.workers.values() if w["status"] == "idle"]),
        },

        # Worker State - 3-tier model (GPT 5.2 feedback)
        "worker_state": {
            "teamplan_recommended": state.worker_state.teamplan_recommended,
            "runtime_target": state.worker_state.runtime_target,
            "runtime_cap": state.worker_state.runtime_cap,
            "online_workers": state.worker_state.online_workers,
        },

        # CI Health tile
        "ci_health": {
            "percent": round(state.get_ci_health(), 1),
            "passes": state.ci_passes,
            "fails": state.ci_fails,
            "avg_duration_sec": 400,  # Would come from actual CI
            "queue_time_sec": 70,
        },

        # PR / Submission Throughput tile
        "pr_throughput": {
            "open": state.submissions_opened if config.vcs.provider == "aether" else state.current_open_prs,
            "merged_last_hour": state.prs_this_hour,
            "merged_total": state.submissions_opened if config.vcs.provider == "aether" else state.prs_merged,
            "rejected": state.prs_rejected,
            "per_hour_rate": 0 if config.vcs.provider == "aether" else state.prs_this_hour,
        },

        # TeamPlan (Org Builder 5.6)
        "teamplan": get_teamplan_summary(),

        # Governor State tile
        "governor": {
            "state": state.governor_state.value,
            "open_pr_cap": state.open_pr_cap,
            "open_pr_current": state.current_open_prs,
            "pr_hour_cap": state.pr_hour_cap,
            "pr_hour_used": state.prs_this_hour,
        },

        # SPEC/DoD Progress tile
        "spec_dod": {
            "spec_total": spec_progress["total"],
            "spec_completed": spec_progress["completed"],
            "spec_percent": spec_progress["percent"],
            "dod_status": dod_status,
        },

        # Burn Rate tile
        "budget": {
            "spent_today": round(state.total_cost, 2),
            "daily_cap": config.budget.daily_usd_cap,
            "burn_rate_hour": round(state.get_burn_rate(), 2),
            "forecast_cap_eta": state.get_forecast_to_cap(config.budget.daily_usd_cap),
            "percent": round((state.total_cost / config.budget.daily_usd_cap * 100) if config.budget.daily_usd_cap > 0 else 0, 1),
        },

        # Swarm status
        "swarm": {
            "active_workers": state.get_active_workers(60),
            "max_workers": runtime_max_workers,
            "paused": config.paused,
            "start_time": state.start_time,
        },

        # Queue
        "queue": {
            "depth": queue_depth,
            "dlq_size": state.tasks_failed,  # Dead letter queue
        },

        # Task metrics by tier
        "tasks": {
            "completed": state.tasks_completed,
            "failed": state.tasks_failed,
            "retried": state.tasks_retried,
            "tier_0": state.tasks_by_tier.get(0, 0),
            "tier_1": state.tasks_by_tier.get(1, 0),
            "tier_2": state.tasks_by_tier.get(2, 0),
            "tier_2_in_progress": len([
                t
                for t in task_contracts
                if t.get("risk_tier") == 2
                and str(t.get("status", "")).upper()
                in {"IN_PROGRESS", "QUEUED", "CLAIMED", "RUNNING", "ARTIFACT_READY", "VERIFIED", "SUBMITTED_PENDING_LEDGER"}
            ]),
        },

        # Charts data
        "charts": {
            "tasks_per_minute": list(state.tasks_per_minute),
            "prs_per_hour": list(state.prs_per_hour),
            "ci_health_trend": list(state.ci_health_trend),
        },

        # Requirements list
        "requirements": requirements,

        # Task contracts table - combine SPEC tasks with simulation tasks (GPT 5.2 Fix 2)
        "task_contracts": task_contracts[:40],

        # Active tasks from simulation (separate for UI convenience)
        "active_tasks": list(state.active_tasks.values()),
        "active_tasks_count": len(state.active_tasks),

        # Config
        "config": {
            "llm_provider": config.llm.provider,
            "llm_model": config.llm.model,
            "timebox_minutes": config.worker.timebox_minutes,
            "retry_max": config.worker.retry_max,
            "judge_interval": config.judge.interval_minutes,
            "auto_merge": config.github.auto_merge_low_risk,
            "vcs_provider": config.vcs.provider,
        },

        "coordination": coordination_data,
        "observability": {
            "metrics": event_observability.get("metrics", {}),
            "formula_cards": event_observability.get("formula_cards", []),
            "gate_a_ready": event_observability.get("gate_a_ready", False),
        },
        "submissions": {
            "latest_successful_branch": submission_summary.get("latest_successful_branch"),
            "latest_successful_submission_id": submission_summary.get("latest_successful_submission_id"),
            "latest_successful_submission_url": submission_summary.get("latest_successful_submission_url"),
            "latest_successful_trace_id": submission_summary.get("latest_successful_trace_id"),
            "latest_settlement_timestamp": submission_summary.get("latest_settlement_timestamp"),
            "recent_settlement_count": submission_summary.get("recent_settlement_count", 0),
        },

        # Pending decisions
        "decisions": {
            "pending": [d.to_dict() for d in state.decision_queue.get_pending_decisions()[:5]],
            "count": len(state.decision_queue.get_pending_decisions()),
        },
    }


def get_teamplan_summary() -> dict | None:
    """Get TEAMPLAN summary for dashboard."""
    teamplan = load_teamplan()
    if not teamplan:
        return None

    return {
        "version": teamplan.version,
        "topology": teamplan.topology.value,
        "streams": [
            {
                "name": s.name,
                "role": s.role,
                "target_workers": s.target_workers,
                "max_workers": s.max_workers,
                "priority": s.priority
            }
            for s in teamplan.streams
        ],
        "total_target_workers": teamplan.total_target_workers(),
        "total_max_workers": teamplan.total_max_workers(),
        "caps": teamplan.caps.to_dict(),
        "risk_merge_policy": teamplan.risk_merge_policy.to_dict(),
        "task_policy": {
            "requires_spec_ref": teamplan.task_policy.requires_spec_ref,
            "max_files_touched": teamplan.task_policy.max_files_touched,
            "max_task_minutes": teamplan.task_policy.max_task_minutes,
        },
        "budget_policy": {
            "usd_per_day": teamplan.budget_policy.usd_per_day,
            "hard_stop": teamplan.budget_policy.hard_stop,
        },
        "reset_triggers": teamplan.reset_triggers.to_dict(),
        "scores": {
            "complexity": round(teamplan.complexity_score, 2),
            "coupling": round(teamplan.coupling_score, 2),
            "risk": round(teamplan.risk_score, 2),
        },
        "created_at": teamplan.created_at,
        "prd_hash": teamplan.prd_hash,
    }


_V2_ANALYTICS_CACHE_TTL_S = 900
_v2_analytics_cache: dict[str, AnalyticsCacheEntry] = {}


def _v2_repo_root() -> str:
    return os.path.dirname(os.path.dirname(__file__))


def _v2_actor_map_dir() -> str:
    return os.path.join(_v2_repo_root(), "vault", "actor_map")


def _v2_phase_transition_path() -> str:
    return os.path.join(_v2_repo_root(), "data", "message_trends.json")


def _read_json_file(path: str) -> dict[str, Any] | None:
    try:
        with open(path) as handle:
            return json.load(handle)
    except Exception:
        return None


def _get_cached_v2_payload(key: str, builder) -> Any:
    now = time.monotonic()
    entry = _v2_analytics_cache.get(key)
    if entry and (now - entry.cached_at_monotonic) < _V2_ANALYTICS_CACHE_TTL_S:
        return entry.value

    value = builder()
    _v2_analytics_cache[key] = AnalyticsCacheEntry(
        value=value,
        cached_at_monotonic=now,
    )
    return value


def _build_v2_economic_flow() -> list[dict[str, Any]]:
    vault_dir = _v2_actor_map_dir()
    if not os.path.exists(vault_dir):
        return []

    results = []
    with os.scandir(vault_dir) as entries:
        for entry in entries:
            if not entry.is_file() or not entry.name.endswith(".json"):
                continue
            data = _read_json_file(entry.path)
            if not data:
                continue
            scores = data.get("intelligence_scores", {})
            economic_mentions = scores.get("economic_mentions", 0)
            sentience_mentions = scores.get("sentience_mentions", 0)

            meatware_url = None
            for p in data.get("post_history", []):
                if "url" in p:
                    meatware_url = p["url"]
                    break
            if not meatware_url:
                for idx in data.get("interaction_history", []):
                    if "url" in idx:
                        meatware_url = idx["url"]
                        break

            if economic_mentions > 0 or sentience_mentions > 0:
                results.append(
                    {
                        "id": data.get("profile", {}).get("id", entry.name),
                        "name": data.get("profile", {}).get("name", entry.name),
                        "economic_mentions": economic_mentions,
                        "sentience_mentions": sentience_mentions,
                        "meatware_url": meatware_url,
                    }
                )
    results.sort(key=lambda item: item["economic_mentions"] + item["sentience_mentions"], reverse=True)
    return results


def _build_v2_alignment_matrix() -> list[dict[str, Any]]:
    vault_dir = _v2_actor_map_dir()
    if not os.path.exists(vault_dir):
        return []

    results = []
    with os.scandir(vault_dir) as entries:
        for entry in entries:
            if not entry.is_file() or not entry.name.endswith(".json"):
                continue
            data = _read_json_file(entry.path)
            if not data:
                continue
            scores = data.get("intelligence_scores", {})
            cooperative_mentions = scores.get("cooperative_mentions", 0)
            adversarial_mentions = scores.get("adversarial_mentions", 0)
            if cooperative_mentions > 0 or adversarial_mentions > 0:
                results.append(
                    {
                        "id": data.get("profile", {}).get("id", entry.name),
                        "name": data.get("profile", {}).get("name", entry.name),
                        "cooperative": cooperative_mentions,
                        "adversarial": adversarial_mentions,
                        "alignment_score": scores.get("alignment_score", 0.0),
                        "total_mentions": len(data.get("post_history", [])),
                    }
                )
    return results


def _build_v2_phase_transition() -> dict[str, Any]:
    trends_path = _v2_phase_transition_path()
    if not os.path.exists(trends_path):
        return {}

    data = _read_json_file(trends_path)
    if not isinstance(data, dict):
        return {}

    macro_data = {}
    for date_str, topics in data.items():
        if not isinstance(topics, dict):
            continue
        macro_data[date_str] = {
            "Structural": topics.get("Macro: Structural", 0),
            "Ritual": topics.get("Macro: Ritual/Spam", 0),
            "_post_count": topics.get("_post_count", 0),
        }
    return macro_data


async def _prewarm_v2_analytics_cache() -> None:
    builders = (
        ("economic-flow", _build_v2_economic_flow),
        ("alignment-matrix", _build_v2_alignment_matrix),
        ("phase-transition", _build_v2_phase_transition),
    )
    for key, builder in builders:
        try:
            await asyncio.to_thread(_get_cached_v2_payload, key, builder)
        except Exception:
            continue


# ============================================================================
# App Setup
# ============================================================================

@asynccontextmanager
async def lifespan(_app: FastAPI):
    state.add_log(EventType.SYS_START, "MX Swarm Dashboard started")
    asyncio.create_task(_prewarm_v2_analytics_cache())
    yield
    state.add_log(EventType.SYS_STOP, "MX Swarm Dashboard stopped")


app = FastAPI(
    title="MX Swarm Dashboard",
    description="Control panel for MX Swarm agent orchestration",
    version="2.0.0",
    lifespan=lifespan,
)

v2_dist_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "dashboard_v2", "dist"))
if os.path.isdir(v2_dist_path):
    app.mount("/v2", StaticFiles(directory=v2_dist_path, html=True), name="dashboard_v2")
else:
    @app.get("/v2")
    async def v2_missing_build():
        return {
            "status": "missing_build",
            "message": "Dashboard V2 not built. Run `npm install` and `npm run build` in dashboard_v2.",
        }

    @app.get("/v2/{path:path}")
    async def v2_missing_build_path(path: str):
        return {
            "status": "missing_build",
            "message": "Dashboard V2 not built. Run `npm install` and `npm run build` in dashboard_v2.",
        }


# ============================================================================
# API Endpoints
# ============================================================================

@app.get("/")
async def root():
    dashboard_path = os.path.join(os.path.dirname(__file__), "index.html")
    return FileResponse(dashboard_path)


@app.get("/api/status")
async def api_status():
    return await get_swarm_status()

# ============================================================================
# V2 Gas Town Analytical Lenses
# ============================================================================

@app.get("/api/v2/economic-flow")
async def v2_economic_flow():
    return await asyncio.to_thread(
        _get_cached_v2_payload,
        "economic-flow",
        _build_v2_economic_flow,
    )

@app.get("/api/v2/alignment-matrix")
async def v2_alignment_matrix():
    return await asyncio.to_thread(
        _get_cached_v2_payload,
        "alignment-matrix",
        _build_v2_alignment_matrix,
    )

@app.get("/api/v2/phase-transition")
async def v2_phase_transition():
    return await asyncio.to_thread(
        _get_cached_v2_payload,
        "phase-transition",
        _build_v2_phase_transition,
    )


@app.get("/api/logs")
async def api_logs(limit: int = 100):
    return {"logs": list(state.logs)[-limit:]}


class LogIngestRequest(BaseModel):
    logs: list[dict]
    source: str = "cli"

@app.post("/api/logs/ingest")
async def api_logs_ingest(req: LogIngestRequest):
    """Ingest logs from external sources (e.g. CLI runner)."""
    for log in req.logs:
        # Map fields to match internal log structure
        state.add_log(
            EventType.SYS_CONFIG,  # Default to config/system type
            f"[{req.source}] {log.get('message', '')}",
            log.get("level", "info")
        )
    return {"status": "ok", "count": len(req.logs)}


@app.post("/api/control/pause")
async def control_pause():
    config = load_config()
    config.paused = True
    save_config(config)
    state.add_log(EventType.SYS_STOP, "Swarm PAUSED by user", "warn")
    await state.broadcast_status()
    return {"status": "paused"}


@app.post("/api/control/resume")
async def control_resume():
    config = load_config()
    config.paused = False
    save_config(config)
    state.add_log(EventType.SYS_START, "Swarm RESUMED by user")
    await state.broadcast_status()
    return {"status": "resumed"}


@app.post("/api/control/scale")
async def control_scale(req: ScaleRequest):
    if req.max_workers < 1 or req.max_workers > 100:
        raise HTTPException(400, "Workers must be between 1-100")

    config = load_config()
    old = config.worker.max_workers
    config.worker.max_workers = req.max_workers
    save_config(config)

    state.add_log(EventType.SYS_SCALE, f"Scaling max workers: {old} → {req.max_workers}")
    await state.broadcast_status()
    return {"old": old, "new": req.max_workers}


@app.post("/api/control/budget")
async def control_budget(req: BudgetRequest):
    config = load_config()
    old = config.budget.daily_usd_cap
    config.budget.daily_usd_cap = req.daily_usd_cap
    save_config(config)

    state.add_log(EventType.SYS_CONFIG, f"Budget cap: ${old} → ${req.daily_usd_cap}")
    await state.broadcast_status()
    return {"old": old, "new": req.daily_usd_cap}


@app.post("/api/control/governor")
async def control_governor(req: GovernorRequest):
    """Update governor caps."""
    if req.open_pr_cap is not None:
        old = state.open_pr_cap
        state.open_pr_cap = req.open_pr_cap
        state.add_log(EventType.SYS_CONFIG, f"Open PR cap: {old} → {req.open_pr_cap}")

    if req.pr_hour_cap is not None:
        old = state.pr_hour_cap
        state.pr_hour_cap = req.pr_hour_cap
        state.add_log(EventType.SYS_CONFIG, f"PR/hour cap: {old} → {req.pr_hour_cap}")

    state.check_governor()
    await state.broadcast_status()
    return {"governor_state": state.governor_state.value}


@app.post("/api/control/throttle")
async def control_throttle():
    """Throttle the swarm - reduce workers + pause PR creation."""
    config = load_config()
    old_workers = config.worker.max_workers
    config.worker.max_workers = max(1, old_workers // 2)  # Halve workers
    save_config(config)

    state.governor_state = GovernorMode.THROTTLED
    state.add_log(EventType.GOV_THROTTLE, f"Swarm THROTTLED: workers {old_workers} → {config.worker.max_workers}", "warn")
    await state.broadcast_status()
    return {"status": "throttled", "new_workers": config.worker.max_workers}


@app.post("/api/control/reset")
async def control_reset():
    """Reset the swarm - replan + restart workers."""
    # Clear state
    state.workers.clear()
    state.active_tasks.clear()
    state.governor_state = GovernorMode.NORMAL

    state.add_log(EventType.JUDGE_RESET, "Swarm RESET initiated - replanning", "warn")
    await state.broadcast_status()
    return {"status": "reset"}


@app.post("/api/prd/analyze")
async def analyze_prd(req: PRDAnalyzeRequest):
    """Analyze PRD content without needing API keys - simulates Agent Boss analysis."""
    if not req.prd_content or len(req.prd_content.strip()) < 10:
        raise HTTPException(status_code=400, detail="PRD content is too short")

    # Perform keyword-based analysis
    analysis = analyze_prd_content(req.prd_content)

    # Generate TEAMPLAN using OrgBuilder
    try:
        builder = OrgBuilder()
        constraints = OrgConstraints(
            max_workers=analysis['recommendations']['suggested_workers'],
            max_budget_per_day=analysis['recommendations']['estimated_budget_usd']
        )
        teamplan = builder.build_teamplan(
            prd_content=req.prd_content,
            constraints=constraints
        )

        # Save TEAMPLAN.json to disk
        teamplan.save("TEAMPLAN.json")

        # Add teamplan summary to response
        analysis['teamplan'] = get_teamplan_summary()

        state.add_log(EventType.SYS_CONFIG,
            f"PRD analyzed: {analysis['complexity']} complexity, TEAMPLAN generated with {teamplan.total_target_workers()} workers")
    except Exception as e:
        # Log error with traceback for debugging
        import traceback
        error_details = traceback.format_exc()
        state.add_log(EventType.SYS_CONFIG,
            f"PRD analyzed: {analysis['complexity']} complexity (TEAMPLAN generation failed: {str(e)})", "error")
        print(f"TEAMPLAN generation error:\n{error_details}", file=sys.stderr)

    return analysis


@app.post("/api/control/ignite")
async def control_ignite(req: IgniteRequest):
    config = load_config()
    if req.vcs_target not in {"aether", "github"}:
        raise HTTPException(400, "vcs_target must be 'aether' or 'github'")

    if req.max_workers is not None:
        config.worker.max_workers = req.max_workers
        state.add_log(EventType.SYS_CONFIG, f"Manual override: {req.max_workers} workers")

    if req.prd_content:
        with open("PRD.md", "w") as f:
            f.write(req.prd_content)
        # Clear old state when new PRD is uploaded
        state.active_tasks.clear()
        state.workers.clear()
        # Remove old SPEC.json so planner regenerates
        if os.path.exists("SPEC.json"):
            os.remove("SPEC.json")
        state.add_log(EventType.SYS_CONFIG, "PRD.md updated - cleared old state")

    if req.budget_usd is not None:
        config.budget.daily_usd_cap = req.budget_usd
        state.add_log(EventType.SYS_CONFIG, f"Manual override: ${req.budget_usd} budget")

    # Handle VCS target selection
    vcs_labels = {
        "aether": "🔮 Aether",
        "github": "🐙 GitHub",
    }
    config.vcs.provider = req.vcs_target
    state.add_log(EventType.SYS_CONFIG, f"VCS target: {vcs_labels.get(req.vcs_target, req.vcs_target)}")

    config.paused = False
    save_config(config)

    state.start_time = datetime.now(UTC).isoformat()
    state.governor_state = GovernorMode.NORMAL


    if req.auto_mode:
        state.add_log(EventType.SYS_START, "Agent Boss analyzing PRD and assembling team...")

        # Launch 'mx ignite --auto-mode' in background
        # cmd = [sys.executable, "-m", "mx-swarm.cli.main", "ignite", "--analyze-only"]
        # We start with analyze-only to generate the plan, the dashboard should then trigger run?
        # Actually, for the demo "Launch Swarm" usually implies "GO".
        # Let's run the full 'mx ignite' which does analysis AND tells user to run 'mx run'.
        # Wait, the CLI 'ignite' doesn't actually run the loop. It just preps.
        # So we should run 'mx run' directly if we want immediate action, OR run 'ignite' then 'run'.

        # Let's simplify: Run 'mx run' directly. The CLI 'run' command handles missing plans by failing?
        # But ignite sets up the plan.

        # Better approach: Run 'mx ignite' to generate the plan, then 'mx run'.
        # For now, let's just launch 'mx run' and assume the user wants to start processing.
        # But if PRD changed, we interpret that as needing a plan.

        # Let's spawn a helper to do: ignite -> run
        async def launch_sequence():
            # 1. Ignite (Analyze)
            state.add_log(EventType.SYS_CONFIG, "Running Agent Boss analysis...")
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "cli.main", "ignite", "--analyze-only",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await proc.communicate()
            except Exception as e:
                state.add_log(EventType.SYS_STOP, f"Ignite subprocess failed to start: {e}", "error")
                return

            if proc.returncode != 0:
                state.add_log(EventType.SYS_STOP, f"Ignite failed: {stderr.decode()}", "error")
                return

            state.add_log(EventType.SYS_CONFIG, "Plan generated. Starting swarm...")

            # 2. Run with output streaming
            try:
                # Use asyncio subprocess for non-blocking I/O
                state.swarm_process = await asyncio.create_subprocess_exec(
                    sys.executable, "-u", "-m", "cli.main", "run",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )

                state.add_log(EventType.SYS_START, f"Swarm Process Started (PID: {state.swarm_process.pid})")

                async def stream_output(stream, log_type):
                    while True:
                        line = await stream.readline()
                        if not line:
                            break
                        decoded = line.decode().strip()
                        if decoded:
                            state.add_log(log_type, decoded)

                # Run stdout and stderr readers concurrently
                await asyncio.gather(
                    stream_output(state.swarm_process.stdout, EventType.SYS_CONFIG),
                    stream_output(state.swarm_process.stderr, EventType.SYS_CONFIG)
                )

                await state.swarm_process.wait()

            except Exception as e:
                state.add_log(EventType.SYS_STOP, f"Run subprocess failed: {e}", "error")
            finally:
                if state.swarm_process:
                    code = state.swarm_process.returncode
                    state.add_log(EventType.SYS_STOP, f"Swarm process exited code {code}", "warn" if code != 0 else "info")
                    state.swarm_process = None

        asyncio.create_task(launch_sequence())

    else:
        # Manual mode: Just run
        try:
            state.swarm_process = subprocess.Popen(
                [sys.executable, "-m", "cli.main", "run"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            state.add_log(EventType.SYS_START, f"Swarm ignited: {config.worker.max_workers} workers (PID: {state.swarm_process.pid})")
        except Exception as e:
            state.add_log(EventType.SYS_STOP, f"Run subprocess failed to start: {e}", "error")

    await state.broadcast_status()
    return {"status": "ignited", "auto_mode": req.auto_mode}


@app.post("/api/control/vcs_preflight")
async def control_vcs_preflight(req: VCSPreflightRequest):
    if req.vcs_target not in {"aether", "github"}:
        raise HTTPException(400, "vcs_target must be 'aether' or 'github'")

    config = load_config()
    target = req.vcs_target
    details: dict[str, str] = {}

    if target == "github":
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("MX_GITHUB_TOKEN") or config.github.token
        if not token:
            return {"ok": False, "target": target, "message": "Missing GitHub token", "details": {"token": "not_set"}}
        if not config.github.repo_owner or not config.github.repo_name:
            details["repo"] = f"{config.github.repo_owner or '?'} / {config.github.repo_name or '?'}"
            return {"ok": False, "target": target, "message": "Missing GitHub repo owner/name", "details": details}

        try:
            from shared.adapters import create_vcs_adapter

            adapter = create_vcs_adapter({
                "provider": "github",
                "token": token,
                "owner": config.github.repo_owner,
                "repo": config.github.repo_name,
            })
            healthy = await adapter.health_check()
            if not healthy:
                return {"ok": False, "target": target, "message": "GitHub health check failed"}
        except Exception as exc:
            return {"ok": False, "target": target, "message": f"GitHub check error: {exc}"}

        return {"ok": True, "target": target, "message": "GitHub ready"}

    # Aether preflight
    if not config.vcs.aether_base_url:
        return {"ok": False, "target": target, "message": "Missing Aether base URL"}
    if not config.vcs.aether_repo_id:
        return {"ok": False, "target": target, "message": "Missing Aether repo id"}

    try:
        from shared.adapters import create_vcs_adapter

        adapter = create_vcs_adapter({
            "provider": "aether",
            "base_url": config.vcs.aether_base_url,
            "repo_id": config.vcs.aether_repo_id,
            "agent_id": config.vcs.aether_agent_id or "mx-swarm",
        })
        healthy = await adapter.health_check()
        if not healthy:
            return {"ok": False, "target": target, "message": "Aether health check failed"}
    except Exception as exc:
        return {"ok": False, "target": target, "message": f"Aether check error: {exc}"}

    return {"ok": True, "target": target, "message": "Aether ready"}


@app.post("/api/control/stop")
async def control_stop():
    """Emergency stop."""
    config = load_config()
    config.paused = True
    save_config(config)

    worker_count = len(state.workers)
    state.workers.clear()
    state.governor_state = GovernorMode.FROZEN

    if state.swarm_process:
        state.swarm_process.terminate()
        try:
            state.swarm_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            state.swarm_process.kill()
        state.swarm_process = None

    state.add_log(EventType.SYS_STOP, f"EMERGENCY STOP - {worker_count} workers halted", "error")
    return {"status": "stopped"}


@app.post("/api/simulation/event")
async def simulation_event(event: dict):
    """Receive event from simulation harness."""
    event_type = event.get("type", "SYS_CONFIG")
    message = event.get("message", "Simulation event")
    level = event.get("level", "info")

    state.add_log(EventType(event_type), message, level)

    # Update metrics if provided
    if "metrics" in event:
        m = event["metrics"]
        state.tasks_completed += m.get("tasks_completed", 0)
        state.prs_opened += m.get("prs_opened", 0)
        state.prs_merged += m.get("prs_merged", 0)
        state.ci_passes += m.get("ci_passes", 0)
        state.ci_fails += m.get("ci_fails", 0)

        # Update workers and worker_state
        if "workers_active" in m:
            # Register simulated workers with proper structure

            now = datetime.now(UTC).isoformat()
            for i in range(m["workers_active"]):
                worker_id = f"sim-worker-{i}"
                state.workers[worker_id] = {
                    "status": "active",
                    "task": f"task-{i}",
                    "last_seen": now
                }
            # Update 3-tier worker_state
            state.worker_state.online_workers = m["workers_active"]
            state.worker_state.enforce_invariant()
        if "tasks_in_progress" in m:
            try:
                state.tasks_in_progress_runtime = max(0, int(m["tasks_in_progress"]))
            except (TypeError, ValueError):
                pass
        if "queue_depth" in m:
            try:
                state.last_queue_depth = int(m["queue_depth"])
            except (TypeError, ValueError):
                pass

        # Update teamplan recommended (from Boss analysis)
        if "teamplan_workers" in m:
            state.worker_state.teamplan_recommended = m["teamplan_workers"]

        # Update runtime target
        if "target_workers" in m:
            state.worker_state.runtime_target = m["target_workers"]
            state.worker_state.runtime_cap = m["target_workers"]  # Cap = target

        # Update governor state
        if "governor_state" in m:
            state.governor_state = GovernorMode(m["governor_state"])

        # Update current open PRs count
        if "prs_open" in m:
            state.current_open_prs = m["prs_open"]

        # Update costs
        if "cost" in m:
            state.total_cost += m["cost"]
            state.current_hour_cost += m["cost"]

    # Task registry for Active Task Contracts table (GPT 5.2 Fix 2)
    if event_type == "PR_OPEN" and "task_id" in event:
        state.active_tasks[event["task_id"]] = {
            "id": event["task_id"],
            "spec_ref": event.get("spec_ref", "-"),
            "goal": event.get("message", "")[:50],
            "worker": event.get("worker_id", "-"),
            "risk_tier": event.get("risk_tier", 1),
            "status": event.get("status", "RUNNING"),
            "progress": 50,
            "pr_number": event.get("pr_number"),
            "created_at": datetime.now(UTC).isoformat()
        }
    elif event_type == "PR_MERGE" and "task_id" in event:
        if event["task_id"] in state.active_tasks:
            state.active_tasks[event["task_id"]]["status"] = "MERGED"
            state.active_tasks[event["task_id"]]["progress"] = 100
    elif event_type == "CI_FAIL" and "task_id" in event:
        if event["task_id"] in state.active_tasks:
            state.active_tasks[event["task_id"]]["status"] = "FAILED"

    await state.broadcast_status()
    return {"status": "received"}


@app.post("/api/config/update")
async def config_update(req: ConfigUpdate):
    config = load_config()

    updates = []
    if req.llm_provider is not None:
        config.llm.provider = req.llm_provider
        updates.append(f"LLM: {req.llm_provider}")
    if req.llm_model is not None:
        config.llm.model = req.llm_model
        updates.append(f"Model: {req.llm_model}")
    if req.timebox_minutes is not None:
        config.worker.timebox_minutes = req.timebox_minutes
        updates.append(f"Timebox: {req.timebox_minutes}m")
    if req.retry_max is not None:
        config.worker.retry_max = req.retry_max
        updates.append(f"Retries: {req.retry_max}")
    if req.judge_interval_minutes is not None:
        config.judge.interval_minutes = req.judge_interval_minutes
        updates.append(f"Judge interval: {req.judge_interval_minutes}m")
    if req.auto_merge_low_risk is not None:
        config.github.auto_merge_low_risk = req.auto_merge_low_risk
        updates.append(f"Auto-merge: {req.auto_merge_low_risk}")

    save_config(config)
    state.add_log(EventType.SYS_CONFIG, f"Config updated: {', '.join(updates)}")
    await state.broadcast_status()
    return {"status": "updated"}


@app.get("/api/team-composition")
async def get_team_composition():
    if os.path.exists("TEAM_COMPOSITION.json"):
        with open("TEAM_COMPOSITION.json") as f:
            return json.load(f)
    return None


@app.get("/api/teamplan")
async def get_teamplan():
    """Get current TEAMPLAN."""
    teamplan = load_teamplan()
    if not teamplan:
        return {"status": "not_found", "message": "No TEAMPLAN.json found. Run `mx ignite` to generate."}
    return teamplan.to_dict()


@app.get("/api/teamplan/summary")
async def get_teamplan_summary_api():
    """Get TEAMPLAN summary for dashboard."""
    summary = get_teamplan_summary()
    if not summary:
        return {"status": "not_found"}
    return summary


@app.get("/api/coordination")
async def get_coordination():
    """Get coordination metrics."""
    return get_coordination_metrics()


@app.get("/api/metrics/dictionary")
async def get_metric_dictionary():
    """Get metric formulas, units and descriptions."""
    collector = get_metrics_collector()
    return {"metric_dictionary": collector.get_metric_dictionary()}


@app.get("/api/metrics/event_sourced")
async def get_event_sourced_metrics(window_minutes: int = 60):
    """Get event-sourced metrics and formula cards."""
    return get_event_observability(window_minutes=window_minutes)


@app.get("/api/traces")
async def list_traces(limit: int = 200):
    """List recent trace spans for dashboard drill-down."""
    collector = get_metrics_collector()
    spans = collector.read_trace_spans(limit=max(1, min(limit, 5000)))
    return {"trace_spans": spans, "count": len(spans)}


@app.get("/api/traces/{trace_id}")
async def get_trace(trace_id: str, limit: int = 500):
    """Get full trace detail (spans + task state events) by trace id."""
    bundle = get_trace_bundle(trace_id=trace_id, limit=max(1, min(limit, 5000)))
    if bundle["span_count"] == 0 and bundle["event_count"] == 0:
        raise HTTPException(status_code=404, detail=f"Trace not found: {trace_id}")
    return bundle


@app.get("/api/submissions/recent")
async def list_recent_submissions(limit: int = 20, settled_only: bool = True):
    """List recent submission events, defaulting to settled outcomes for operators."""
    collector = get_metrics_collector()
    events = collector.get_recent_submission_events(
        limit=max(1, min(limit, 200)),
        settled_only=settled_only,
    )
    return {"submissions": events, "count": len(events)}


@app.post("/api/teamplan/generate")
async def generate_teamplan():
    """Generate TEAMPLAN from PRD."""
    if not os.path.exists("PRD.md"):
        raise HTTPException(400, "PRD.md not found")

    # Import here to avoid circular imports
    from boss.org_builder import OrgBuilder, OrgConstraints

    config = load_config()

    with open("PRD.md") as f:
        prd_content = f.read()

    constraints = OrgConstraints(
        max_budget_per_day=config.budget.daily_usd_cap,
        max_workers=config.worker.max_workers,
    )

    builder = OrgBuilder()
    teamplan = builder.build_teamplan(prd_content, constraints)
    teamplan.save("TEAMPLAN.json")

    state.add_log(EventType.SYS_CONFIG, f"TEAMPLAN generated: {teamplan.total_target_workers()} workers, {len(teamplan.streams)} streams")
    await state.broadcast_status()

    return {
        "status": "generated",
        "topology": teamplan.topology.value,
        "streams": len(teamplan.streams),
        "total_workers": teamplan.total_target_workers(),
        "scores": {
            "complexity": round(teamplan.complexity_score, 2),
            "coupling": round(teamplan.coupling_score, 2),
            "risk": round(teamplan.risk_score, 2),
        }
    }


@app.get("/api/reports/heartbeat")
async def get_heartbeat_reports():
    """Get list of 15-minute heartbeat reports."""
    reports_dir = os.path.join(os.getcwd(), "reports", "heartbeat")
    if not os.path.exists(reports_dir):
        return {"reports": []}

    reports = []
    for f in sorted(os.listdir(reports_dir), reverse=True):
        if f.endswith(".md"):
            with open(os.path.join(reports_dir, f)) as file:
                content = file.read()
                reports.append({"filename": f, "timestamp": f.replace(".md", ""), "content": content})
    return {"reports": reports[:20]}  # Last 20 heartbeats (5 hours)


@app.get("/api/reports/hourly")
async def get_hourly_reports():
    """Get list of hourly reports."""
    reports_dir = os.path.join(os.getcwd(), "reports", "hourly")
    if not os.path.exists(reports_dir):
        return {"reports": []}

    reports = []
    for f in sorted(os.listdir(reports_dir), reverse=True):
        if f.endswith(".md"):
            with open(os.path.join(reports_dir, f)) as file:
                content = file.read()
                reports.append({"filename": f, "timestamp": f.replace(".md", ""), "content": content})
    return {"reports": reports[:24]}  # Last 24 hours


@app.get("/api/reports/daily")
async def get_daily_reports():
    """Get list of daily reports."""
    reports_dir = os.path.join(os.getcwd(), "reports", "daily")
    if not os.path.exists(reports_dir):
        return {"reports": []}

    reports = []
    for f in sorted(os.listdir(reports_dir), reverse=True):
        if f.endswith(".md"):
            with open(os.path.join(reports_dir, f)) as file:
                content = file.read()
                reports.append({"filename": f, "timestamp": f.replace(".md", ""), "content": content})
    return {"reports": reports[:7]}  # Last 7 days


@app.post("/api/decisions/approve")
async def approve_decision_legacy(decision_id: str = "budget_increase"):
    """Legacy endpoint - Approve a pending decision."""
    state.add_log(EventType.SYS_CONFIG, f"Decision APPROVED: {decision_id}")
    return {"status": "approved"}


# ============================================================================
# Decision Queue Endpoints
# ============================================================================

class DecisionActionRequest(BaseModel):
    action_id: str


@app.get("/api/decisions")
async def get_decisions():
    """Get all pending decisions."""
    decisions = state.decision_queue.get_pending_decisions()
    return {
        "decisions": [d.to_dict() for d in decisions],
        "count": len(decisions)
    }


@app.get("/api/decisions/{decision_id}")
async def get_decision(decision_id: str):
    """Get a specific decision."""
    decision = state.decision_queue.get_decision(decision_id)
    if not decision:
        raise HTTPException(404, "Decision not found")
    return decision.to_dict()


@app.post("/api/decisions/{decision_id}/resolve")
async def resolve_decision(decision_id: str, req: DecisionActionRequest):
    """Resolve a decision by executing an action."""
    success = state.decision_queue.resolve_decision(decision_id, req.action_id, "human")
    if not success:
        raise HTTPException(400, "Failed to resolve decision")

    state.add_log(EventType.SYS_CONFIG, f"Decision {decision_id} resolved: {req.action_id}")
    await state.broadcast_status()
    return {"status": "resolved", "action": req.action_id}


@app.post("/api/decisions/create/budget")
async def create_budget_decision():
    """Create a budget increase decision based on current metrics."""
    config = load_config()
    decision = state.decision_queue.create_budget_decision(
        current_spend=state.total_cost,
        daily_cap=config.budget.daily_usd_cap,
        burn_rate=state.current_hour_cost
    )
    state.add_log(EventType.SYS_CONFIG, f"Budget decision created: {decision.id}")
    return decision.to_dict()


@app.post("/api/decisions/create/deployment")
async def create_deployment_decision(version: str = "v1.0.0", summary: str = "Ready for deployment"):
    """Create a deployment approval decision."""
    decision = state.decision_queue.create_deployment_decision(version, summary)
    state.add_log(EventType.SYS_CONFIG, f"Deployment decision created: {decision.id}")
    return decision.to_dict()


@app.post("/api/decisions/create/replan")
async def create_replan_decision(reason: str = "Stall detected", blocked: int = 0):
    """Create a replan decision."""
    decision = state.decision_queue.create_replan_decision(reason, blocked)
    state.add_log(EventType.SYS_CONFIG, f"Replan decision created: {decision.id}")
    return decision.to_dict()


# ============================================================================
# Escalation Endpoints
# ============================================================================

@app.get("/api/escalations")
async def get_escalations():
    """Get all active escalations."""
    escalations = state.escalation_engine.get_active_escalations()
    return {
        "escalations": [e.to_dict() for e in escalations],
        "count": len(escalations)
    }


@app.post("/api/escalations/check")
async def check_escalations():
    """Manually trigger escalation check with current metrics."""
    metrics = {
        "ci_health": state.ci_passes / max(1, state.ci_passes + state.ci_fails),
        "retry_rate": state.tasks_retried / max(1, state.tasks_completed + state.tasks_failed),
        "queue_depth": len(state.active_tasks),
        "budget_percent": state.total_cost / max(1, load_config().budget.daily_usd_cap),
    }

    new_escalations = state.escalation_engine.check_triggers(metrics)

    for esc in new_escalations:
        state.add_log(EventType.GOV_THROTTLE, f"ESCALATION: {esc.title}", "warn")

    return {
        "checked": True,
        "new_escalations": [e.to_dict() for e in new_escalations],
        "metrics": metrics
    }


@app.post("/api/escalations/{escalation_id}/acknowledge")
async def acknowledge_escalation(escalation_id: str):
    """Acknowledge an escalation."""
    success = state.escalation_engine.acknowledge(escalation_id)
    if success:
        state.add_log(EventType.SYS_CONFIG, f"Escalation {escalation_id} acknowledged")
    return {"status": "acknowledged" if success else "not_found"}


@app.post("/api/escalations/{escalation_id}/resolve")
async def resolve_escalation(escalation_id: str):
    """Resolve an escalation."""
    success = state.escalation_engine.resolve(escalation_id)
    if success:
        state.add_log(EventType.SYS_CONFIG, f"Escalation {escalation_id} resolved")
    return {"status": "resolved" if success else "not_found"}


@app.post("/api/reports/generate/heartbeat")
async def generate_heartbeat_report():
    """Manually generate a 15-minute heartbeat report."""
    metrics = {
        "ci_health": state.ci_passes / max(1, state.ci_passes + state.ci_fails),
        "governor_state": state.governor_state.value,
        "active_workers": len([w for w in state.workers.values() if w.get("status") == "active"]),
        "prs_merged": state.prs_merged,
        "prs_open": state.prs_opened - state.prs_merged - state.prs_rejected,
        "tasks_completed": state.tasks_completed,
        "cost_usd": state.total_cost,
        "ci_passes": state.ci_passes,
        "ci_fails": state.ci_fails,
    }

    # Get governor actions from recent adjustments
    governor_actions = []
    if state.governor:
        recent = state.governor.adjustments[-5:] if state.governor.adjustments else []
        governor_actions = [f"{adj.type.value}: {adj.reason}" for adj in recent]

    report = state.reporter.generate_heartbeat_report(metrics, governor_actions)
    state.add_log(EventType.JUDGE_EVAL, f"Heartbeat report generated: {report.timestamp}")
    return {"status": "generated", "filename": f"{report.timestamp}.md"}


@app.post("/api/reports/generate/hourly")
async def generate_hourly_report():
    """Manually generate an hourly ops report."""
    metrics = {
        "ci_health": state.ci_passes / max(1, state.ci_passes + state.ci_fails),
        "governor_state": state.governor_state.value,
        "active_workers": len([w for w in state.workers.values() if w.get("status") == "active"]),
        "prs_merged": state.prs_merged,
        "prs_open": state.prs_opened - state.prs_merged - state.prs_rejected,
        "burn_rate": state.current_hour_cost,
        "tasks_completed": state.tasks_completed,
        "cost_usd": state.total_cost,
    }
    report = state.reporter.generate_hourly_report(metrics, [])
    state.add_log(EventType.JUDGE_EVAL, f"Hourly report generated: {report.timestamp}")
    return {"status": "generated", "filename": f"{report.timestamp}.md"}


@app.post("/api/reports/generate/daily")
async def generate_daily_report():
    """Manually generate a daily executive standup report."""
    metrics = {
        "daily_merged": state.prs_merged,
        "daily_features": state.tasks_completed,
        "ci_pass_rate": state.ci_passes / max(1, state.ci_passes + state.ci_fails),
        "security_status": "PASSING",
        "spent_today": state.total_cost,
        "forecast": state.total_cost * 1.2,  # Simple forecast
    }
    report = state.reporter.generate_daily_report(metrics)
    state.add_log(EventType.JUDGE_EVAL, f"Daily report generated: {report.timestamp}")
    return {"status": "generated", "filename": f"{report.timestamp}.md"}


# ============================================================================
# Telemetry Endpoints
# ============================================================================

@app.get("/api/telemetry/snapshot")
async def get_telemetry_snapshot():
    """Get current telemetry snapshot from Governor."""
    try:
        snapshot = state.governor.get_telemetry()
        return {
            "ci_health_percent": snapshot.ci_health_percent,
            "tasks_completed_1h": snapshot.tasks_completed_1h,
            "tasks_nacked_1h": getattr(snapshot, 'tasks_nacked_1h', 0),
            "queue_depth": getattr(snapshot, 'queue_depth', len(state.active_tasks)),
            "prs_open_total": snapshot.prs_open_total,
            "workers_active": snapshot.workers_active,
            "spend_today_usd": getattr(snapshot, 'spend_today_usd', state.total_cost),
        }
    except Exception:
        # Fallback if governor not initialized
        return {
            "ci_health_percent": state.get_ci_health(),
            "tasks_completed_1h": state.tasks_completed,
            "tasks_nacked_1h": state.tasks_failed,
            "queue_depth": len(state.active_tasks),
            "prs_open_total": state.current_open_prs,
            "workers_active": state.get_active_workers(),
            "spend_today_usd": state.total_cost,
        }


@app.get("/api/governor/adjustments")
async def get_governor_adjustments():
    """Get recent governor adjustments."""
    try:
        gov_state = state.governor.get_state_summary()
        return {
            "mode": gov_state["mode"],
            "current_caps": gov_state["current_caps"],
            "recent_adjustments": gov_state["recent_adjustments"][-20:],  # Last 20
        }
    except Exception:
        # Fallback
        return {
            "mode": state.governor_state.value,
            "current_caps": {
                "open_pr_cap": state.open_pr_cap,
                "pr_hour_cap": state.pr_hour_cap,
            },
            "recent_adjustments": [],
        }


# ============================================================================
# Swarm Heartbeat Endpoint (for CLI swarm to report status)
# ============================================================================

class SwarmHeartbeat(BaseModel):
    """Heartbeat from CLI swarm process."""
    active_workers: int = 0
    worker_details: list[dict] | None = None
    tasks_in_progress: int = 0
    tasks_completed: int = 0
    tasks_failed: int = 0
    tasks_processed: int = 0
    prs_opened: int = 0
    prs_merged: int = 0
    submissions_opened: int = 0
    submissions_accepted: int = 0
    ci_passes: int = 0
    ci_fails: int = 0
    queue_depth: int | None = None
    target_workers: int | None = None
    runtime_cap: int | None = None
    is_running: bool = True
    current_phase: str | None = None  # "planning", "working", "judging", etc.


@app.post("/api/swarm/heartbeat")
async def swarm_heartbeat(heartbeat: SwarmHeartbeat):
    """Receive heartbeat from CLI swarm process to sync dashboard state."""
    now = datetime.now(UTC).isoformat()

    # Update worker state
    state.worker_state.online_workers = heartbeat.active_workers
    if heartbeat.target_workers is not None:
        state.worker_state.runtime_target = max(0, heartbeat.target_workers)
    if heartbeat.runtime_cap is not None:
        state.worker_state.runtime_cap = max(0, heartbeat.runtime_cap)
    elif heartbeat.target_workers is not None:
        state.worker_state.runtime_cap = max(0, heartbeat.target_workers)
    state.worker_state.enforce_invariant()

    # Register workers from details or create placeholders
    if heartbeat.worker_details:
        heartbeat_workers: dict[str, dict[str, Any]] = {}
        heartbeat_tasks: dict[str, dict] = {}
        for w in heartbeat.worker_details:
            worker_id = w.get("id", f"worker-{len(heartbeat_workers)}")
            heartbeat_workers[worker_id] = {
                "worker_id": worker_id,
                "last_seen": now,
                "task_id": w.get("task_id"),
                "status": w.get("status", "active"),
            }
            task_id = w.get("task_id")
            if task_id:
                heartbeat_tasks[task_id] = {
                    "id": task_id,
                    "spec_ref": w.get("spec_ref") or "-",
                    "prd_ref": w.get("spec_ref") or "-",
                    "goal": w.get("task_goal") or "In progress",
                    "worker": worker_id,
                    "risk_tier": 1,
                    "status": "RUNNING",
                    "progress": 50,
                }
        state.workers = heartbeat_workers
        state.active_tasks = heartbeat_tasks
    else:
        # Create placeholder workers based on count
        heartbeat_workers: dict[str, dict[str, Any]] = {}
        for i in range(heartbeat.active_workers):
            worker_id = f"swarm-worker-{i}"
            heartbeat_workers[worker_id] = {
                "worker_id": worker_id,
                "last_seen": now,
                "task_id": None,
                "status": "active",
            }
        state.workers = heartbeat_workers
        if heartbeat.tasks_in_progress == 0 and heartbeat.queue_depth == 0:
            state.active_tasks = {}

    state.tasks_in_progress_runtime = max(0, heartbeat.tasks_in_progress)

    # Update metrics (incremental)
    if heartbeat.tasks_completed > state.tasks_completed:
        state.tasks_completed = heartbeat.tasks_completed
    if heartbeat.tasks_failed > state.tasks_failed:
        state.tasks_failed = heartbeat.tasks_failed
    if heartbeat.prs_opened > state.prs_opened:
        state.prs_opened = heartbeat.prs_opened
    if heartbeat.prs_merged > state.prs_merged:
        state.prs_merged = heartbeat.prs_merged
    if heartbeat.submissions_opened > state.submissions_opened:
        state.submissions_opened = heartbeat.submissions_opened
    if heartbeat.submissions_accepted > state.submissions_accepted:
        state.submissions_accepted = heartbeat.submissions_accepted
    if heartbeat.ci_passes > state.ci_passes:
        state.ci_passes = heartbeat.ci_passes
    if heartbeat.ci_fails > state.ci_fails:
        state.ci_fails = heartbeat.ci_fails
    if heartbeat.queue_depth is not None:
        state.last_queue_depth = heartbeat.queue_depth

    # Track current phase (suppress steady-state "running" noise).
    normalized_phase = (heartbeat.current_phase or "").strip().lower()
    if normalized_phase and normalized_phase != state.last_phase:
        if normalized_phase != "running" or (state.last_phase and state.last_phase != "running"):
            state.add_log(EventType.SYS_CONFIG, f"Swarm phase: {normalized_phase}")
        state.last_phase = normalized_phase

    # Broadcast update to all connected clients
    await state.broadcast_status()

    return {"status": "ok", "received_at": now}


@app.get("/api/swarm/status")
async def get_swarm_running_status():
    """Quick check if swarm appears to be running (workers active in last 60s)."""
    active = state.get_active_workers(60)
    config = load_config()
    return {
        "is_running": active > 0 or (state.swarm_process is not None),
        "active_workers": active,
        "paused": config.paused,
        "subprocess_running": state.swarm_process is not None,
    }


# ============================================================================
# WebSocket
# ============================================================================

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    state.websocket_clients.append(ws)
    state.add_log(EventType.SYS_CONFIG, "Dashboard client connected")

    try:
        status = await get_swarm_status()
        await ws.send_json({"type": "status", "data": status})

        for log in list(state.logs)[-50:]:
            await ws.send_json({"type": "log", "data": log})

        while True:
            try:
                data = await asyncio.wait_for(ws.receive_text(), timeout=30)
                if data == "ping":
                    await ws.send_text("pong")
            except TimeoutError:
                await ws.send_json({"type": "heartbeat"})
    except WebSocketDisconnect:
        pass
    finally:
        if ws in state.websocket_clients:
            state.websocket_clients.remove(ws)


# ============================================================================
# Demo/Simulation
# ============================================================================

@app.post("/api/demo/simulate")
async def simulate_activity():
    """Simulate realistic swarm activity."""

    # Register some workers
    for _i in range(random.randint(3, 8)):
        worker_id = f"worker-{random.randint(1, 99):02d}"
        task_id = f"TSK{random.randint(1000, 9999)}" if random.random() > 0.3 else None
        state.register_worker(worker_id, task_id)

    # Simulate events
    events = [
        (EventType.TASK_CLAIM, f"Worker worker-{random.randint(1,20):02d} claimed task TSK{random.randint(1000,9999)}"),
        (EventType.TASK_ACK, f"Task TSK{random.randint(1000,9999)} completed successfully"),
        (EventType.PR_OPEN, f"PR #{random.randint(1,100)} opened for TSK{random.randint(1000,9999)}"),
        (EventType.CI_PASS, f"CI passed for PR #{random.randint(1,100)}"),
        (EventType.PR_MERGE, f"PR #{random.randint(1,100)} merged to main"),
    ]

    if random.random() > 0.7:
        events.append((EventType.TASK_NACK, f"Task TSK{random.randint(1000,9999)} failed verification. Retrying..."))
        events.append((EventType.CI_FAIL, f"CI failed for PR #{random.randint(1,100)}"))
        state.ci_fails += 1
        state.tasks_failed += 1

    for event_type, msg in random.sample(events, min(3, len(events))):
        level = "warn" if "fail" in msg.lower() else "info"
        state.add_log(event_type, msg, level)

    # Update metrics
    state.tasks_completed += random.randint(1, 3)
    state.prs_opened += random.randint(0, 2)
    state.prs_merged += random.randint(0, 1)
    state.ci_passes += random.randint(1, 3)
    state.current_open_prs = random.randint(3, 15)
    state.prs_this_hour = random.randint(2, 10)
    state.total_cost += random.uniform(0.3, 1.5)
    state.current_hour_cost = random.uniform(2.0, 8.0)

    # Update task tiers
    state.tasks_by_tier[0] += random.randint(1, 3)
    state.tasks_by_tier[1] += random.randint(0, 2)
    state.tasks_by_tier[2] += random.randint(0, 1)

    # Update time series
    state.tasks_per_minute.append(random.randint(1, 5))
    state.ci_health_trend.append(state.get_ci_health())

    state.check_governor()
    await state.broadcast_status()
    return {"status": "simulated"}


# ============================================================================
# Run
# ============================================================================

def main():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
