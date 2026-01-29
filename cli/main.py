#!/usr/bin/env python3
"""MX Swarm CLI - Command-line interface for the agent delivery harness.

Commands:
    mx run          Start the swarm (planner + workers + judge)
    mx status       Show swarm status and metrics
    mx scale        Adjust worker count
    mx pause        Pause all workers
    mx resume       Resume workers
    mx init         Initialize a new MX project
    mx plan         Run planner only
    mx judge        Run judge evaluation
    mx dashboard    Start the monitoring dashboard
"""

import argparse
import asyncio
import json
import os
import signal
import sys

# Load .env file for GITHUB_TOKEN and other secrets
# Use absolute path: go up from cli/ to project root
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_env_path = os.path.join(_project_root, ".env")
if os.path.exists(_env_path):
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_path)
    except ImportError:
        # Fallback: manually parse .env if dotenv not installed
        with open(_env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ.setdefault(key.strip(), value.strip())

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from boss import AgentBoss
from boss.org_builder import OrgBuilder, OrgConstraints
from judge import Judge
from planner import Planner
from shared.adapters import create_queue_adapter, create_vcs_adapter
from shared.adapters.aether_vcs import AetherVCS
from shared.schemas import Spec, RequirementStatus
from shared.schemas.teamplan import TeamPlan
from shared.utils import MXConfig
from worker import Worker


def print_banner():
    """Print MX Swarm banner."""
    print("""
╔═══════════════════════════════════════════════╗
║  MX SWARM - PRD-to-Shipping Agent Harness     ║
║  Autonomous software delivery at scale        ║
╚═══════════════════════════════════════════════╝
""")


def load_config(config_path: str = "mx.config.json") -> MXConfig:
    """Load or create configuration."""
    return MXConfig.load_or_create(config_path)


def load_spec(repo_path: str = ".") -> Spec | None:
    """Load SPEC.json if exists."""
    spec_path = os.path.join(repo_path, "SPEC.json")
    if os.path.exists(spec_path):
        return Spec.load(spec_path)
    return None


async def sync_spec_with_aether(spec: Spec, config: MXConfig) -> None:
    """Sync SPEC state with Aether proposals."""
    print("Syncing with Aether...")
    
    # Create temp adapter to fetch proposals
    vcs_config = config.vcs.to_dict()
    # Ensure provider is set correctly for factory
    if "provider" not in vcs_config:
        vcs_config["provider"] = config.vcs.provider
        
    try:
        vcs = create_vcs_adapter(vcs_config)
    except Exception as e:
        print(f"Warning: Could not create VCS adapter for sync: {e}")
        return

    if not isinstance(vcs, AetherVCS):
        return

    proposals = await vcs.get_agent_proposals(config.vcs.aether_agent_id or "mx-swarm")
    
    # Map task_id -> status
    # Note: Aether returns multiple proposals per task (history). 
    # We care if ANY proposal for a task is PROMOTED.
    task_status_map = {}
    for p in proposals:
        tid = p.get('task_id')
        status = p.get('status')
        if not tid:
            continue
        
        # Priority: PROMOTED > PENDING > SUPERSEDED
        current = task_status_map.get(tid)
        if status == "PROMOTED":
            task_status_map[tid] = "PROMOTED"
        elif status == "PENDING" and current != "PROMOTED":
            task_status_map[tid] = "PENDING"
        elif status == "SUPERSEDED" and current is None:
            task_status_map[tid] = "SUPERSEDED"
    
    updates = 0
    for req in spec.requirements:
        if req.status == RequirementStatus.COMPLETED:
            continue
            
        # Check assigned tasks
        if not req.assigned_tasks:
            continue
            
        # Check if ANY assigned task is PROMOTED
        # (Assuming 1 major task per requirement, or if one part is done we count it)
        promoted = any(task_status_map.get(tid) == "PROMOTED" for tid in req.assigned_tasks)
        
        if promoted:
            req.status = RequirementStatus.COMPLETED
            req.completion_evidence.append("Aether Proposal Promoted")
            updates += 1
            print(f"  > Marked {req.id} as COMPLETED (Aether promoted)")
        
        elif req.status == RequirementStatus.IN_PROGRESS:
            # If not promoted, and currently marked IN_PROGRESS from previous run,
            # we must reset it to PENDING so planner regenerates tasks.
            # Otherwise it's stuck in "limbo" (planner thinks it's busy, but queue is empty).
            
            # Exception: If we verify the task ID is currently PENDING in Aether, maybe we wait?
            # But we lost the queue item, so no worker is monitoring it.
            # Ideally we should "re-adopt" the pending proposal? 
            # Phase 1 Strategy: Aggressive retry. Reset to PENDING.
            # Aether will see a new proposal for the same task (actually new task ID gets generated).
            # This causes duplicate "Pending" proposals in Aether.
            # But the user logic handles duplicates (Superseded).
            
            req.status = RequirementStatus.PENDING
            # Keep assigned_tasks for history? No, planner appends. Clearing avoids confusion.
            # But we lose the link to the old proposal.
            # Ideally: Move old task IDs to 'history'?
            # For now: Clear responsible output.
            req.assigned_tasks = []
            updates += 1
            print(f"  > Reset {req.id} to PENDING (retry needed)")

    if updates:
        print(f"Synced {updates} requirements with Aether state.")
    else:
        print("Aether sync: No state changes needed.")


async def cmd_init(args):
    """Initialize a new MX project."""
    print("Initializing MX Swarm project...")

    # Create config
    config = MXConfig()
    config.github.repo_owner = args.owner or input("GitHub repo owner: ").strip()
    config.github.repo_name = args.repo or input("GitHub repo name: ").strip()
    config.save("mx.config.json")
    print("Created mx.config.json")

    # Create AGENTS.md template
    if not os.path.exists("AGENTS.md"):
        with open("AGENTS.md", "w") as f:
            f.write("""# Agent Guidelines

This file provides guidelines for MX Swarm workers.

## Code Style
- Follow existing patterns in the codebase
- Use meaningful variable and function names
- Add comments for complex logic

## Testing
- Add tests for new functionality
- Ensure existing tests pass

## Security
- Never commit secrets or credentials
- Validate all inputs
- Use parameterized queries for databases

## Constraints
- Keep PRs focused and small
- Don't modify core infrastructure without explicit task
- Prefer incremental changes over large rewrites
""")
        print("Created AGENTS.md")

    # Create PRD.md template if doesn't exist
    if not os.path.exists("PRD.md"):
        with open("PRD.md", "w") as f:
            f.write("""# Product Requirements Document

## Overview
[Describe your product/feature here]

## Goals
1. [Goal 1]
2. [Goal 2]

## User Stories
- As a [user], I want to [action] so that [benefit]

## Requirements

### Functional Requirements
1. [Requirement 1]
2. [Requirement 2]

### Non-Functional Requirements
1. [Performance requirement]
2. [Security requirement]

## Definition of Done
- [ ] All requirements implemented
- [ ] Tests passing
- [ ] Documentation updated
- [ ] Code reviewed
""")
        print("Created PRD.md template")

    print("\nInitialization complete!")
    print("Next steps:")
    print("  1. Edit PRD.md with your requirements")
    print("  2. Edit AGENTS.md with project-specific guidelines")
    print("  3. Set GITHUB_TOKEN and ANTHROPIC_API_KEY environment variables")
    print("  4. Run: mx run")


async def cmd_run(args):
    """Start the swarm."""
    print_banner()

    # Check for dashboard URL
    dashboard_url = os.environ.get("MX_DASHBOARD_URL", args.dashboard_url)
    remote_logger = None
    if dashboard_url:
        from shared.utils.remote_logger import RemoteLogStream
        remote_logger = RemoteLogStream(dashboard_url, source_id="cli-runner")
        remote_logger.start()
        print(f"Streaming logs to dashboard at {dashboard_url}")

    try:


        config = load_config(args.config)

        # Check prerequisites
        if not os.path.exists("PRD.md"):
            print("Error: PRD.md not found. Run 'mx init' first.")
            sys.exit(1)

        # 0. Sync with Aether (Status Sync Phase 1)
        if config.vcs.provider == "aether":
            spec = load_spec(".")
            if spec:
                await sync_spec_with_aether(spec, config)
                spec.save("SPEC.json")

        if not os.environ.get("GITHUB_TOKEN") and not config.github.token:
            print("Warning: GITHUB_TOKEN not set - Enabling Local Mode (Mock GitHub)")

        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("Warning: ANTHROPIC_API_KEY not set - using default LLM config")

        print("Starting MX Swarm...")
        print(f"  Repository: {config.github.repo_owner}/{config.github.repo_name}")
        print(f"  Max workers: {args.workers or config.worker.max_workers}")
        print(f"  LLM: {config.llm.provider}/{config.llm.model}")
        print()

        # Update config with CLI args
        if args.workers:
            config.worker.max_workers = args.workers
        config.paused = False
        config.save()

        # Run components

        # 1. Run planner first
        print("[1/3] Running planner...")
        planner = Planner(config, repo_path=".")
        
        # Split run() to inject Aether sync
        await planner.initialize()
        try:
            # Step 1: Generate Spec from PRD (or load existing)
            spec = await planner.generate_spec()
            
            # Step 2: Sync with Aether (Phase 1 Logic)
            if config.vcs.provider == "aether":
                await sync_spec_with_aether(spec, config)
                spec.save("SPEC.json")
                
            # Step 3: Generate and Publish Tasks (skips COMPLETED/PROMOTED requirements)
            tasks = await planner.generate_tasks(spec)
            published = await planner.publish_tasks(tasks)
            
            result = {
                "tasks_generated": len(tasks),
                "tasks_published": published,
                "spec_requirements": len(spec.requirements)
            }
        finally:
            await planner.shutdown()

        print(f"  Generated {result['tasks_generated']} tasks from {result['spec_requirements']} requirements")

        if result['tasks_published'] == 0:
            print("  No new tasks generated. Continuing to process existing tasks...")

        # 2. Start workers
        print(f"\n[2/3] Starting {config.worker.max_workers} workers...")
        workers = []
        for i in range(config.worker.max_workers):
            worker = Worker(config, worker_id=f"worker-{i}")
            workers.append(worker)

        # 3. Start judge
        print(f"\n[3/3] Starting judge (interval: {config.judge.interval_minutes}min)...")
        judge = Judge(config, repo_path=".")

        # Track metrics for dashboard heartbeat
        swarm_metrics = {
            "active_workers": config.worker.max_workers,
            "tasks_completed": 0,
            "prs_opened": 0,
            "prs_merged": 0,
            "ci_passes": 0,
            "ci_fails": 0,
            "current_phase": "running",
        }

        # Create async tasks
        async def run_worker(w):
            await w.initialize()
            try:
                await w.run_loop()
            finally:
                await w.shutdown()

        async def run_judge():
            await judge.initialize()
            try:
                await judge.run_loop()
            finally:
                await judge.shutdown()

        async def send_dashboard_heartbeat():
            """Send periodic heartbeats to the dashboard."""
            dashboard_url = os.environ.get("MX_DASHBOARD_URL", args.dashboard_url)
            heartbeat_interval = 5  # seconds

            # Try to use aiohttp, fall back to urllib if not available
            try:
                import aiohttp
                use_aiohttp = True
            except ImportError:
                import urllib.error
                import urllib.request
                use_aiohttp = False

            while True:
                try:
                    # Collect worker status
                    worker_details = []
                    active_count = 0
                    for w in workers:
                        is_active = hasattr(w, '_current_task') and w._current_task is not None
                        if is_active:
                            active_count += 1
                        current_task = getattr(w, "_current_task", None)
                        worker_details.append({
                            "id": w.worker_id,
                            "status": "active" if is_active else "idle",
                            "task_id": current_task.id if current_task else None,
                            "task_goal": current_task.goal if current_task else None,
                            "spec_ref": current_task.spec_ref if current_task else None,
                        })

                    tasks_completed = sum(w.metrics.tasks_succeeded for w in workers)
                    tasks_failed = sum(w.metrics.tasks_failed for w in workers)
                    tasks_processed = sum(w.metrics.tasks_processed for w in workers)

                    queue_depth = None
                    if workers:
                        try:
                            queue_depth = await workers[0].queue.get_queue_depth()
                        except Exception:
                            queue_depth = None

                    vcs_provider = config.vcs.provider
                    prs_opened = tasks_completed if vcs_provider == "github" else 0
                    submissions_opened = tasks_completed if vcs_provider == "aether" else 0

                    # Send heartbeat
                    payload = {
                        "active_workers": max(active_count, 1),  # At least 1 if running
                        "worker_details": worker_details,
                        "tasks_in_progress": active_count,
                        "tasks_completed": tasks_completed,
                        "tasks_failed": tasks_failed,
                        "tasks_processed": tasks_processed,
                        "prs_opened": prs_opened,
                        "prs_merged": 0,
                        "submissions_opened": submissions_opened,
                        "submissions_accepted": 0,
                        "ci_passes": 0,
                        "ci_fails": 0,
                        "queue_depth": queue_depth,
                        "is_running": True,
                        "current_phase": swarm_metrics["current_phase"],
                    }

                    if use_aiohttp:
                        async with aiohttp.ClientSession() as session:
                            async with session.post(
                                f"{dashboard_url}/api/swarm/heartbeat",
                                json=payload,
                                timeout=aiohttp.ClientTimeout(total=2)
                            ):
                                pass  # Just send, don't care about response
                    else:
                        # Fallback to synchronous urllib in thread pool
                        def sync_post(p):
                            req = urllib.request.Request(
                                f"{dashboard_url}/api/swarm/heartbeat",
                                data=json.dumps(p).encode('utf-8'),
                                headers={'Content-Type': 'application/json'},
                                method='POST'
                            )
                            try:
                                urllib.request.urlopen(req, timeout=2)
                            except (urllib.error.URLError, OSError):
                                pass
                        await asyncio.get_event_loop().run_in_executor(None, sync_post, payload)

                except Exception:
                    pass  # Ignore errors - dashboard might not be running

                await asyncio.sleep(heartbeat_interval)

        # Handle shutdown
        shutdown_event = asyncio.Event()

        def signal_handler():
            print("\n\nShutdown signal received...")
            shutdown_event.set()

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, signal_handler)
            except NotImplementedError:
                pass

        print("\nSwarm is running. Press Ctrl+C to stop.\n")

        # Run all components
        worker_tasks = [asyncio.create_task(run_worker(w)) for w in workers]
        judge_task = asyncio.create_task(run_judge())
        heartbeat_task = asyncio.create_task(send_dashboard_heartbeat())

        try:
            # Wait for shutdown or completion
            await shutdown_event.wait()
        finally:
            # Cancel all tasks
            for task in worker_tasks:
                task.cancel()
            judge_task.cancel()
            heartbeat_task.cancel()

            # Wait for cleanup
            await asyncio.gather(*worker_tasks, judge_task, heartbeat_task, return_exceptions=True)

    finally:
        if remote_logger:
            await remote_logger.stop()

    print("\nSwarm stopped.")


async def cmd_status(args):
    """Show swarm status."""
    config = load_config(args.config)
    spec = load_spec(".")

    print("MX Swarm Status")
    print("=" * 50)

    # TEAMPLAN status
    if os.path.exists("TEAMPLAN.json"):
        teamplan = TeamPlan.load("TEAMPLAN.json")
        print("\nTEAMPLAN (Org-as-Code):")
        print(f"  Topology: {teamplan.topology.value}")
        print("  Streams:")
        for s in teamplan.streams:
            print(f"    - {s.name}: {s.target_workers}/{s.max_workers} workers")
        print(f"  Total: {teamplan.total_target_workers()} workers (max {teamplan.total_max_workers()})")
        print(f"  Scores: complexity={teamplan.complexity_score:.2f}, coupling={teamplan.coupling_score:.2f}, risk={teamplan.risk_score:.2f}")
        print(f"  Caps: {teamplan.caps.pr_cap_per_hour} PR/hr, {teamplan.caps.open_pr_cap} open PRs")
    else:
        print("\nNo TEAMPLAN.json - run 'mx ignite' to generate")

    # Config status
    print("\nConfiguration:")
    print(f"  Repository: {config.github.repo_owner}/{config.github.repo_name}")
    print(f"  Max workers: {config.worker.max_workers}")
    print(f"  Budget: ${config.budget.daily_usd_cap}/day")
    print(f"  Paused: {config.paused}")

    # Spec/progress status
    if spec:
        progress = spec.progress_summary()
        print("\nProgress:")
        print(f"  Requirements: {progress['completed']}/{progress['total']} ({progress['percent']}%)")
        print(f"  In Progress: {progress['in_progress']}")
        print(f"  Pending: {progress['pending']}")
        print(f"  Blocked: {progress.get('blocked', 0)}")
    else:
        print("\nNo SPEC.json found - run 'mx run' or 'mx plan' first")

    # Queue status
    try:
        queue = create_queue_adapter(config.queue.to_dict())
        await queue.connect()
        depth = await queue.get_queue_depth()
        await queue.disconnect()
        print("\nQueue:")
        print(f"  Depth: {depth if depth >= 0 else 'unknown'}")
    except Exception as e:
        print(f"\nQueue: Unable to connect ({e})")

    # Judge report
    report_path = "JUDGE_REPORT.md"
    if os.path.exists(report_path):
        with open(report_path) as f:
            content = f.read()
            # Get last report
            reports = content.split("## Judge Report")
            if len(reports) > 1:
                last = reports[-1]
                # Extract decision
                for line in last.split("\n"):
                    if "**Decision:**" in line:
                        print(f"\nLast Judge Decision: {line.split('**Decision:**')[1].strip()}")
                        break


async def cmd_scale(args):
    """Adjust worker count."""
    config = load_config(args.config)

    old_max = config.worker.max_workers
    config.worker.max_workers = args.max
    config.save()

    print(f"Worker limit updated: {old_max} -> {args.max}")
    print("Note: Running workers will continue until stopped/restarted")


async def cmd_pause(args):
    """Pause all workers."""
    config = load_config(args.config)
    config.paused = True
    config.save()
    print("Swarm paused. Workers will stop after current tasks complete.")


async def cmd_resume(args):
    """Resume workers."""
    config = load_config(args.config)
    config.paused = False
    config.save()
    print("Swarm resumed. Workers will continue processing tasks.")


async def cmd_plan(args):
    """Run planner only."""
    config = load_config(args.config)

    print("Running planner...")
    planner = Planner(config, repo_path=".")
    result = await planner.run(force_replan=args.force)

    print("\nPlanner Results:")
    print(f"  Requirements: {result['spec_requirements']}")
    print(f"  Tasks Generated: {result['tasks_generated']}")
    print(f"  Tasks Published: {result['tasks_published']}")
    print(f"  Progress: {result['progress']['percent']}%")


async def cmd_judge(args):
    """Run judge evaluation."""
    config = load_config(args.config)

    print("Running judge evaluation...")
    judge = Judge(config, repo_path=".")
    await judge.initialize()

    try:
        report = await judge.run_once()

        print("\nJudge Report:")
        print(f"  Decision: {report.decision.value.upper()}")
        print(f"  Reason: {report.reason}")
        print("\nMetrics:")
        for key, value in report.metrics.items():
            print(f"    {key}: {value}")

        if report.actions_taken:
            print("\nActions:")
            for action in report.actions_taken:
                print(f"    - {action}")

    finally:
        await judge.shutdown()


async def cmd_budget(args):
    """Set budget limits."""
    config = load_config(args.config)

    if args.daily:
        config.budget.daily_usd_cap = args.daily
        print(f"Daily budget cap set to ${args.daily}")

    config.save()


async def cmd_ignite(args):
    """Agent Boss: Analyze PRD and ignite the swarm with dynamic org."""
    print_banner()
    print("=" * 60)
    print("  AGENT BOSS - Intelligent Team Orchestration")
    print("  + ORG BUILDER - Dynamic Organization Synthesis")
    print("=" * 60)

    config = load_config(args.config)

    # Check prerequisites
    if not os.path.exists("PRD.md"):
        print("\nError: PRD.md not found. Run 'mx init' first.")
        sys.exit(1)

    # Read PRD
    with open("PRD.md") as f:
        prd_content = f.read()

    # Read DoD if exists
    dod_content = None
    if os.path.exists("DoD.md"):
        with open("DoD.md") as f:
            dod_content = f.read()

    # Build constraints from config and args
    constraints = OrgConstraints(
        max_budget_per_day=args.budget or config.budget.daily_usd_cap,
        max_workers=args.workers or config.worker.max_workers,
    )

    # Step 1: Build TEAMPLAN
    print("\n[1/3] Org Builder analyzing PRD...")
    builder = OrgBuilder()
    teamplan = builder.build_teamplan(prd_content, constraints, dod_content)

    print("\n  PRD Analysis Scores:")
    print(f"    Complexity: {teamplan.complexity_score:.2f}")
    print(f"    Coupling:   {teamplan.coupling_score:.2f}")
    print(f"    Risk:       {teamplan.risk_score:.2f}")

    print("\n  TEAMPLAN Generated:")
    print(f"    Topology: {teamplan.topology.value}")
    print(f"    Streams: {len(teamplan.streams)}")
    for s in teamplan.streams:
        print(f"      - {s.name}: {s.target_workers} workers (max {s.max_workers})")
    print(f"    Total Workers: {teamplan.total_target_workers()} (max {teamplan.total_max_workers()})")

    print("\n  Governance Caps:")
    print(f"    PR/hour: {teamplan.caps.pr_cap_per_hour}")
    print(f"    Open PRs: {teamplan.caps.open_pr_cap}")
    print(f"    Active tasks: {teamplan.caps.max_active_tasks}")

    print("\n  Risk Merge Policy:")
    print(f"    Tier 0 (low):  {teamplan.risk_merge_policy.tier0.value}")
    print(f"    Tier 1 (med):  {teamplan.risk_merge_policy.tier1.value}")
    print(f"    Tier 2 (high): {teamplan.risk_merge_policy.tier2.value}")

    if args.dry_run:
        print("\n[DRY RUN] Would save TEAMPLAN.json and ignite swarm")
        return

    # Save TEAMPLAN
    teamplan.save("TEAMPLAN.json")
    print("\n  Saved TEAMPLAN.json")

    if args.analyze_only:
        print("\nAnalysis complete. Run 'mx ignite' without --analyze-only to continue.")
        return

    # Step 2: Run Agent Boss for team composition
    print("\n[2/3] Agent Boss assembling team...")
    if os.environ.get("ANTHROPIC_API_KEY"):
        boss = AgentBoss(config, repo_path=".")
        composition = await boss.analyze_prd()

        # Save team composition
        with open("TEAM_COMPOSITION.json", "w") as f:
            f.write(composition.to_json())
        print(composition.summary())
    else:
        print("  Skipping LLM-based team composition (ANTHROPIC_API_KEY not set)")
        print("  Using TEAMPLAN defaults")

    # Update config from TEAMPLAN
    config.worker.max_workers = teamplan.total_target_workers()
    config.budget.daily_usd_cap = teamplan.budget_policy.usd_per_day
    config.paused = False
    config.save()

    # Step 3: Run planner
    print("\n[3/3] Running planner to generate tasks...")
    planner = Planner(config, repo_path=".")
    result = await planner.run()

    print("\n  Planner Results:")
    print(f"    SPEC requirements: {result['spec_requirements']}")
    print(f"    Tasks generated:   {result['tasks_generated']}")
    print(f"    Tasks published:   {result['tasks_published']}")

    print("\n" + "=" * 60)
    print("  SWARM READY TO IGNITE")
    print("=" * 60)
    print(f"\n  Workers:      {teamplan.total_target_workers()}")
    print(f"  Tasks queued: {result['tasks_published']}")
    print(f"  Budget:       ${teamplan.budget_policy.usd_per_day}/day")
    print("\n  Run 'mx run' to start workers")
    print("  Or open dashboard with 'mx dashboard' for visual control")


async def cmd_dashboard(args):
    """Start the monitoring dashboard."""
    import uvicorn

    from dashboard.api import app

    print_banner()
    print(f"Starting MX Swarm Dashboard on http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.")

    # Run uvicorn
    config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
    )
    server = uvicorn.Server(config)
    await server.serve()


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="mx",
        description="MX Swarm - PRD-to-Shipping Agent Delivery Harness",
    )
    parser.add_argument(
        "--config",
        default="mx.config.json",
        help="Config file path",
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # init
    init_parser = subparsers.add_parser("init", help="Initialize MX project")
    init_parser.add_argument("--owner", help="GitHub repo owner")
    init_parser.add_argument("--repo", help="GitHub repo name")

    # run
    run_parser = subparsers.add_parser("run", help="Start the swarm")
    run_parser.add_argument(
        "--workers", "-w",
        type=int,
        help="Number of workers to run",
    )
    run_parser.add_argument(
        "--dashboard-url",
        default="http://localhost:8080",
        help="Dashboard URL for heartbeat sync (default: http://localhost:8080)",
    )

    # status
    subparsers.add_parser("status", help="Show swarm status")

    # scale
    scale_parser = subparsers.add_parser("scale", help="Adjust worker count")
    scale_parser.add_argument(
        "--max",
        type=int,
        required=True,
        help="Maximum number of workers",
    )

    # pause
    subparsers.add_parser("pause", help="Pause all workers")

    # resume
    subparsers.add_parser("resume", help="Resume workers")

    # plan
    plan_parser = subparsers.add_parser("plan", help="Run planner only")
    plan_parser.add_argument(
        "--force", "-f",
        action="store_true",
        help="Force regeneration",
    )

    # judge
    subparsers.add_parser("judge", help="Run judge evaluation")

    # budget
    budget_parser = subparsers.add_parser("budget", help="Set budget limits")
    budget_parser.add_argument(
        "--daily",
        type=float,
        help="Daily USD cap",
    )

    # ignite (Agent Boss + Org Builder)
    ignite_parser = subparsers.add_parser(
        "ignite",
        help="Agent Boss: Analyze PRD and ignite swarm with dynamic org"
    )
    ignite_parser.add_argument(
        "--analyze-only", "-a",
        action="store_true",
        help="Only analyze PRD and generate TEAMPLAN, don't ignite",
    )
    ignite_parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Show plan without executing",
    )
    ignite_parser.add_argument(
        "--workers", "-w",
        type=int,
        help="Override max workers (otherwise from TEAMPLAN)",
    )
    ignite_parser.add_argument(
        "--budget", "-b",
        type=float,
        help="Override daily budget cap (otherwise from TEAMPLAN)",
    )

    # dashboard
    dashboard_parser = subparsers.add_parser("dashboard", help="Start the monitoring dashboard")
    dashboard_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind to",
    )
    dashboard_parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port to bind to (default: 8080)",
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Map commands to functions
    commands = {
        "init": cmd_init,
        "run": cmd_run,
        "status": cmd_status,
        "scale": cmd_scale,
        "pause": cmd_pause,
        "resume": cmd_resume,
        "plan": cmd_plan,
        "judge": cmd_judge,
        "budget": cmd_budget,
        "ignite": cmd_ignite,
        "dashboard": cmd_dashboard,
    }

    # Run command
    try:
        asyncio.run(commands[args.command](args))
    except KeyboardInterrupt:
        print("\nInterrupted")
        sys.exit(130)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
