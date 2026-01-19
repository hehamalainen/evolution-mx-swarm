"""
Simulation Harness - Test Rig for Operating Model

Mocks:
- LLM (returns synthetic plans/code)
- GitHub (simulates PRs, merges, CI outcomes)
- CI (simulates pass/fail with probability)

Runs a virtual swarm of N agents to stress-test the Governor.
"""

import asyncio
import random
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Dict, Optional

from shared.utils import MXConfig
from shared.utils.metrics import get_metrics_collector, TaskMetrics
from judge.service import Judge
from boss import Governor, GovernorState, TelemetrySnapshot, Reporter
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("simulation")


@dataclass
class MockPR:
    id: int
    title: str
    status: str = "open"  # open, merged, closed
    ci_status: str = "pending"  # pending, success, failure
    created_at: datetime = field(default_factory=datetime.utcnow)


class MockGitHub:
    def __init__(self, failure_rate: float = 0.1):
        self.prs: List[MockPR] = []
        self.failure_rate = failure_rate
        self._pr_counter = 0

    async def create_pr(self, title: str) -> MockPR:
        self._pr_counter += 1
        pr = MockPR(id=self._pr_counter, title=title)
        
        # Simulate CI
        if random.random() < self.failure_rate:
            pr.ci_status = "failure"
        else:
            pr.ci_status = "success"
            
        self.prs.append(pr)
        return pr

    async def merge_pr(self, pr_id: int):
        for pr in self.prs:
            if pr.id == pr_id:
                pr.status = "merged"
                return True
        return False

    async def list_prs(self, state: str = "open") -> List[dict]:
        return [
            {
                "number": pr.id,
                "title": pr.title,
                "state": pr.status,
                "head": {"sha": f"sha-{pr.id}"},
                "merged_at": datetime.utcnow().isoformat() if pr.status == "merged" else None
            }
            for pr in self.prs if pr.status == state
        ]

    async def get_check_runs(self, ref: str) -> dict:
        # Extract ID from ref "sha-123"
        try:
            pr_id = int(ref.split("-")[1])
            pr = next((p for p in self.prs if p.id == pr_id), None)
            if pr:
                return {"check_runs": [{"conclusion": pr.ci_status}]}
        except:
            pass
        return {"check_runs": []}


class SimulationRunner:
    def __init__(self, agents: int = 20, duration_minutes: int = 60):
        self.agents = agents
        self.duration_minutes = duration_minutes
        self.github = MockGitHub(failure_rate=0.15)  # 15% failure rate
        self.judge = Judge(MXConfig(), repo_path=".")
        self.reporter = Reporter(repo_path=".")
        self.metrics_collector = get_metrics_collector()
        
        # Initialize Governor
        self.governor = Governor(
            teamplan=self.judge.teamplan,
            telemetry_provider=self._get_telemetry_snapshot
        )
        
        # Monkey patch judge's github
        self.judge._github = self.github  # type: ignore

    async def run(self):
        logger.info(f"Starting simulation with {self.agents} agents for {self.duration_minutes}m")
        
        start_time = datetime.utcnow()
        end_time = start_time + timedelta(minutes=self.duration_minutes)
        
        # Simulation loop (1 tick = 1 minute)
        current_time = start_time
        while current_time < end_time:
            logger.info(f"Tick: {current_time.strftime('%H:%M')}")
            
            # 1. Simulate Agent Activity
            # Randomly open PRs based on agent count
            # Avg 1 PR per agent per hour = 1/60 PR per minute
            prob_pr = (1.0 / 60.0) * self.agents
            if random.random() < prob_pr:
                # Generate task with traceability fields
                task_id = f"task-{random.randint(100,999)}"
                spec_ref = f"SPEC-{random.randint(1, 20):03d}"
                worker_id = f"worker-{random.randint(1, self.agents)}"
                risk_tier = random.choice([0, 1, 2])
                
                pr = await self.github.create_pr(f"Task [{task_id}] implementation")
                logger.info("Agent opened PR")
                
                # Notify dashboard with full state + traceability (GPT 5.2 feedback)
                open_prs = await self.github.list_prs("open")
                try:
                    async with httpx.AsyncClient() as client:
                        await client.post("http://localhost:8000/api/simulation/event", json={
                            "type": "PR_OPEN",
                            "message": f"Agent opened PR: {pr.title}",
                            # Traceability fields (Fix 2)
                            "task_id": task_id,
                            "spec_ref": spec_ref,
                            "worker_id": worker_id,
                            "risk_tier": risk_tier,
                            "status": "RUNNING",
                            "pr_number": pr.id,
                            # Metrics
                            "metrics": {
                                "prs_opened": 1,
                                "prs_open": len(open_prs),
                                "workers_active": self.agents,
                                "ci_passes": 1 if pr.ci_status == "success" else 0,
                                "ci_fails": 1 if pr.ci_status == "failure" else 0,
                                "cost": round(random.uniform(0.02, 0.10), 3)
                            }
                        })
                except Exception:
                    pass
                
                # Record metric
                self.metrics_collector.record_task(TaskMetrics(
                    task_id=task_id,
                    stream="simulation",
                    tier=risk_tier,
                    spec_ref=spec_ref,
                    started_at=current_time,
                    llm_calls=random.randint(1, 5),
                    estimated_cost_usd=0.05,
                    pr_created=True
                ))

            # 2. Run Governor Check (every 15 mins)
            if (current_time - start_time).seconds % 900 == 0:
                logger.info("Running Governor Check...")
                report = await self.judge.run_governor_check()
                logger.info(f"Governor State: {report.state}")
                
                # Notify dashboard of governor state
                try:
                    async with httpx.AsyncClient() as client:
                        await client.post("http://localhost:8000/api/simulation/event", json={
                            "type": "GOV_CHECK",
                            "message": f"Governor Check: {report.state.value}",
                            "level": "warn" if report.state != GovernorState.NORMAL else "info",
                            "metrics": {
                                "governor_state": report.state.value,
                                "workers_active": self.agents
                            }
                        })
                except Exception:
                    pass
                
                # Generate Hourly Report if on hour
                if (current_time - start_time).seconds % 3600 == 0:
                    self.reporter.generate_hourly_report(report.metrics, report.actions)

            # 3. Simulate Merges (if CI passed)
            open_prs = await self.github.list_prs("open")
            for pr in open_prs:
                # 10% chance to merge if CI passed
                if random.random() < 0.1:
                    await self.github.merge_pr(pr["number"])
                    logger.info(f"Merged PR #{pr['number']}")
                    
                    # Notify dashboard of merge
                    try:
                        remaining_prs = await self.github.list_prs("open")
                        async with httpx.AsyncClient() as client:
                            await client.post("http://localhost:8000/api/simulation/event", json={
                                "type": "PR_MERGE",
                                "message": f"Merged PR #{pr['number']}",
                                "metrics": {
                                    "prs_merged": 1,
                                    "prs_open": len(remaining_prs),
                                    "ci_passes": 1,
                                    "tasks_completed": 1
                                }
                            })
                    except Exception:
                        pass

            # Advance time
            current_time += timedelta(minutes=1)
            await asyncio.sleep(2)  # 2 seconds per simulated minute - visible pace

        logger.info("Simulation complete")

    def _get_telemetry_snapshot(self) -> TelemetrySnapshot:
        """Provide telemetry snapshot for the Governor during simulation."""
        # In simulation, we can provide more accurate mock data
        return TelemetrySnapshot(
            ci_health_percent=85.0, # Mocked
            tasks_completed_1h=10, # Mocked
            prs_open_total=len(self.github.prs),
            workers_active=self.agents,
            spend_today_usd=0.0 # Mocked
        )

if __name__ == "__main__":
    runner = SimulationRunner(agents=20, duration_minutes=20)
    asyncio.run(runner.run())
