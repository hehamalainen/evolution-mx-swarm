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
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional
from collections import deque
from enum import Enum

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.utils import MXConfig
from shared.adapters import create_queue_adapter
from shared.schemas import Spec
from shared.schemas.teamplan import TeamPlan
from shared.utils.metrics import get_metrics_collector, CoordinationMetrics
from boss import (
    EscalationEngine, Escalation, EscalationType, EscalationSeverity,
    Governor, GovernorState, TelemetrySnapshot, Adjustment, AdjustmentType, TriggerType,
    Reporter
)


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


class GovernorState(str, Enum):
    NORMAL = "NORMAL"
    THROTTLED = "THROTTLED"
    FROZEN = "FROZEN"


class RiskTier(int, Enum):
    LOW = 0       # Auto-merge OK
    MEDIUM = 1    # Requires 1 approval
    HIGH = 2      # Manual review required


# ============================================================================
# State Management
# ============================================================================

class SwarmState:
    """Global swarm state tracker with enhanced metrics."""

    def __init__(self):
        # Worker tracking
        self.workers: dict[str, dict] = {}  # worker_id -> {last_seen, task_id, ...}

        # Task tracking
        self.active_tasks: dict[str, dict] = {}
        self.tasks_by_tier: dict[int, int] = {0: 0, 1: 0, 2: 0}

        # Metrics
        self.tasks_completed: int = 0
        self.tasks_failed: int = 0
        self.tasks_retried: int = 0
        self.prs_opened: int = 0
        self.prs_merged: int = 0
        self.prs_rejected: int = 0
        self.ci_passes: int = 0
        self.ci_fails: int = 0

        # Cost tracking
        self.total_cost: float = 0.0
        self.hourly_costs: deque = deque(maxlen=24)  # Last 24 hours
        self.current_hour_cost: float = 0.0
        self.last_hour_reset: datetime = datetime.utcnow()

        # Time series for charts
        self.tasks_per_minute: deque = deque(maxlen=60)  # Last 60 minutes
        self.prs_per_hour: deque = deque(maxlen=24)  # Last 24 hours
        self.ci_health_trend: deque = deque(maxlen=60)  # Last 60 data points

        # Governor state
        self.governor_state: GovernorState = GovernorState.NORMAL
        self.open_pr_cap: int = 25
        self.pr_hour_cap: int = 15
        self.current_open_prs: int = 0
        self.prs_this_hour: int = 0

        # Logs
        self.logs: deque = deque(maxlen=1000)

        # Timing
        self.start_time: Optional[str] = None

        # WebSocket clients
        self.websocket_clients: list[WebSocket] = []

        # Escalation & Reporting
        self.escalation_engine = EscalationEngine(repo_path=".")
        self.reporter = Reporter(repo_path=".")
        
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

    def register_worker(self, worker_id: str, task_id: Optional[str] = None):
        """Register or update worker activity."""
        self.workers[worker_id] = {
            "worker_id": worker_id,
            "last_seen": datetime.utcnow().isoformat(),
            "task_id": task_id,
            "status": "active" if task_id else "idle",
        }

    def get_active_workers(self, seconds: int = 60) -> int:
        """Count workers active in last N seconds."""
        cutoff = datetime.utcnow() - timedelta(seconds=seconds)
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

    def get_forecast_to_cap(self, daily_cap: float) -> Optional[str]:
        """Forecast when daily cap will be hit."""
        if self.current_hour_cost <= 0:
            return None
        remaining = daily_cap - self.total_cost
        hours_left = remaining / self.current_hour_cost
        if hours_left <= 0:
            return "NOW"
        eta = datetime.utcnow() + timedelta(hours=hours_left)
        return eta.strftime("%H:%M")

    def _get_telemetry_snapshot(self) -> TelemetrySnapshot:
        """Provide telemetry snapshot for the Governor."""
        return TelemetrySnapshot(
            ci_health_percent=self.get_ci_health(),
            tasks_completed_1h=self.tasks_completed, # Simplified
            prs_open_total=self.current_open_prs,
            workers_active=self.get_active_workers(),
            spend_today_usd=self.total_cost
        )

    def check_governor(self) -> GovernorState:
        """Check and update governor state based on caps and config."""
        # Update telemetry and evaluate
        asyncio.create_task(self.governor._evaluate())
        
        # Sync state
        self.governor_state = GovernorState(self.governor.state.mode.value)
        self.open_pr_cap = self.governor.state.current_open_pr_cap
        self.pr_hour_cap = self.governor.state.current_pr_cap_hour
            
        return self.governor_state

    def add_log(self, event_type: EventType, message: str, level: str = "info"):
        """Add structured log entry."""
        entry = {
            "timestamp": datetime.utcnow().strftime("%H.%M.%S"),
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
    open_pr_cap: Optional[int] = None
    pr_hour_cap: Optional[int] = None
    ci_health_threshold: Optional[float] = None

class WorkerAction(BaseModel):
    worker_id: str
    action: str  # "stop", "restart"

class IgniteRequest(BaseModel):
    max_workers: Optional[int] = None
    budget_usd: Optional[float] = None
    auto_mode: bool = True
    prd_content: Optional[str] = None

class ConfigUpdate(BaseModel):
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    timebox_minutes: Optional[int] = None
    retry_max: Optional[int] = None
    judge_interval_minutes: Optional[int] = None
    auto_merge_low_risk: Optional[bool] = None

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

def load_spec() -> Optional[Spec]:
    if os.path.exists("SPEC.json"):
        return Spec.load("SPEC.json")
    return None


def load_teamplan() -> Optional[TeamPlan]:
    """Load TEAMPLAN.json if it exists."""
    if os.path.exists("TEAMPLAN.json"):
        return TeamPlan.load("TEAMPLAN.json")
    return None


def get_coordination_metrics() -> dict:
    """Get coordination metrics for dashboard."""
    collector = get_metrics_collector()
    return collector.get_dashboard_data()

async def get_swarm_status() -> dict:
    """Get complete swarm status with all credibility metrics."""
    config = load_config()
    spec = load_spec()

    # Queue depth
    queue_depth = 0
    try:
        queue = create_queue_adapter(config.queue.to_dict())
        await queue.connect()
        queue_depth = await queue.get_queue_depth()
        await queue.disconnect()
    except Exception:
        queue_depth = -1

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

    # Task contracts with risk tiers
    task_contracts = []
    if spec:
        for req in spec.requirements:
            for i, task_id in enumerate(req.assigned_tasks[:10]):
                risk = RiskTier.LOW if i % 3 == 0 else (RiskTier.MEDIUM if i % 3 == 1 else RiskTier.HIGH)
                task_contracts.append({
                    "id": task_id,
                    "goal": req.title[:50],
                    "worker": state.workers.get(task_id, {}).get("worker_id", "-"),
                    "status": req.status.value.upper(),
                    "progress": 100 if req.status.value == "completed" else 50,
                    "spec_ref": req.id,
                    "risk_tier": risk.value,
                })

    # Check governor
    state.check_governor()

    return {
        # Agents Online tile
        "agents": {
            "total": len(state.workers),
            "max": config.worker.max_workers,
            "active_60s": state.get_active_workers(60),
            "idle": len([w for w in state.workers.values() if w["status"] == "idle"]),
        },

        # CI Health tile
        "ci_health": {
            "percent": round(state.get_ci_health(), 1),
            "passes": state.ci_passes,
            "fails": state.ci_fails,
            "avg_duration_sec": 400,  # Would come from actual CI
            "queue_time_sec": 70,
        },

        # PR Throughput tile
        "pr_throughput": {
            "open": state.current_open_prs,
            "merged_last_hour": state.prs_this_hour,
            "merged_total": state.prs_merged,
            "rejected": state.prs_rejected,
            "per_hour_rate": state.prs_this_hour,
        },

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
            "max_workers": config.worker.max_workers,
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
            "tier_2_in_progress": len([t for t in task_contracts if t.get("risk_tier") == 2 and t.get("status") == "IN_PROGRESS"]),
        },

        # Charts data
        "charts": {
            "tasks_per_minute": list(state.tasks_per_minute),
            "prs_per_hour": list(state.prs_per_hour),
            "ci_health_trend": list(state.ci_health_trend),
        },

        # Requirements list
        "requirements": requirements,

        # Task contracts table
        "task_contracts": task_contracts[:20],

        # Config
        "config": {
            "llm_provider": config.llm.provider,
            "llm_model": config.llm.model,
            "timebox_minutes": config.worker.timebox_minutes,
            "retry_max": config.worker.retry_max,
            "judge_interval": config.judge.interval_minutes,
            "auto_merge": config.github.auto_merge_low_risk,
        },

        # TEAMPLAN (org-as-code)
        "teamplan": get_teamplan_summary(),

        # Coordination metrics
        "coordination": get_coordination_metrics(),
    }


def get_teamplan_summary() -> Optional[dict]:
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


# ============================================================================
# App Setup
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    state.add_log(EventType.SYS_START, "MX Swarm Dashboard started")
    yield
    state.add_log(EventType.SYS_STOP, "MX Swarm Dashboard stopped")


app = FastAPI(
    title="MX Swarm Dashboard",
    description="Control panel for MX Swarm agent orchestration",
    version="2.0.0",
    lifespan=lifespan,
)


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


@app.get("/api/logs")
async def api_logs(limit: int = 100):
    return {"logs": list(state.logs)[-limit:]}


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

    state.governor_state = GovernorState.THROTTLED
    state.add_log(EventType.GOV_THROTTLE, f"Swarm THROTTLED: workers {old_workers} → {config.worker.max_workers}", "warn")
    await state.broadcast_status()
    return {"status": "throttled", "new_workers": config.worker.max_workers}


@app.post("/api/control/reset")
async def control_reset():
    """Reset the swarm - replan + restart workers."""
    config = load_config()

    # Clear state
    state.workers.clear()
    state.active_tasks.clear()
    state.governor_state = GovernorState.NORMAL

    state.add_log(EventType.JUDGE_RESET, "Swarm RESET initiated - replanning", "warn")
    await state.broadcast_status()
    return {"status": "reset"}


@app.post("/api/prd/analyze")
async def analyze_prd(req: PRDAnalyzeRequest):
    """Analyze PRD content without needing API keys - simulates Agent Boss analysis."""
    if not req.prd_content or len(req.prd_content.strip()) < 10:
        raise HTTPException(status_code=400, detail="PRD content is too short")

    analysis = analyze_prd_content(req.prd_content)
    state.add_log(EventType.SYS_CONFIG, f"PRD analyzed: {analysis['complexity']} complexity, {len(analysis['categories'])} categories detected")
    return analysis


@app.post("/api/control/ignite")
async def control_ignite(req: IgniteRequest):
    config = load_config()

    if req.max_workers is not None:
        config.worker.max_workers = req.max_workers
        state.add_log(EventType.SYS_CONFIG, f"Manual override: {req.max_workers} workers")

    if req.prd_content:
        with open("PRD.md", "w") as f:
            f.write(req.prd_content)
        state.add_log(EventType.SYS_CONFIG, "PRD.md updated from dashboard")

    if req.budget_usd is not None:
        config.budget.daily_usd_cap = req.budget_usd
        state.add_log(EventType.SYS_CONFIG, f"Manual override: ${req.budget_usd} budget")

    config.paused = False
    save_config(config)

    state.start_time = datetime.utcnow().isoformat()
    state.governor_state = GovernorState.NORMAL

    if req.auto_mode:
        state.add_log(EventType.SYS_START, "Agent Boss analyzing PRD and assembling team...")
    else:
        state.add_log(EventType.SYS_START, f"Swarm ignited: {config.worker.max_workers} workers, ${config.budget.daily_usd_cap} budget")

    await state.broadcast_status()
    return {"status": "ignited", "auto_mode": req.auto_mode}


@app.post("/api/control/stop")
async def control_stop():
    """Emergency stop."""
    config = load_config()
    config.paused = True
    save_config(config)

    worker_count = len(state.workers)
    state.workers.clear()
    state.governor_state = GovernorState.FROZEN

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
        
        # Update workers
        if "workers_active" in m:
            # Register simulated workers with proper structure
            from datetime import datetime
            now = datetime.utcnow().isoformat()
            for i in range(m["workers_active"]):
                worker_id = f"sim-worker-{i}"
                state.workers[worker_id] = {
                    "status": "active", 
                    "task": f"task-{i}",
                    "last_seen": now
                }
        
        # Update governor state
        if "governor_state" in m:
            state.governor_state = GovernorState(m["governor_state"])
            
        # Update current open PRs count
        if "prs_open" in m:
            state.current_open_prs = m["prs_open"]
            
        # Update costs
        if "cost" in m:
            state.total_cost += m["cost"]
            state.current_hour_cost += m["cost"]
        
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
                reports.append({"filename": f, "content": content})
    return {"reports": reports[:24]} # Last 24 hours


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
                reports.append({"filename": f, "content": content})
    return {"reports": reports[:7]} # Last 7 days


@app.post("/api/decisions/approve")
async def approve_decision(decision_id: str = "budget_increase"):
    """Approve a pending decision."""
    state.add_log(EventType.SYS_CONFIG, f"Decision APPROVED: {decision_id}")
    return {"status": "approved"}


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
            except asyncio.TimeoutError:
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
    import random

    # Register some workers
    for i in range(random.randint(3, 8)):
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
