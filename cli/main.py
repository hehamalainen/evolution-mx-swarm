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
from datetime import datetime
from typing import Optional

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.utils import MXConfig
from shared.adapters import create_queue_adapter
from shared.schemas import Spec
from shared.schemas.teamplan import TeamPlan
from planner import Planner
from worker import Worker
from judge import Judge
from boss import AgentBoss
from boss.org_builder import OrgBuilder, OrgConstraints


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


def load_spec(repo_path: str = ".") -> Optional[Spec]:
    """Load SPEC.json if exists."""
    spec_path = os.path.join(repo_path, "SPEC.json")
    if os.path.exists(spec_path):
        return Spec.load(spec_path)
    return None


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

    config = load_config(args.config)

    # Check prerequisites
    if not os.path.exists("PRD.md"):
        print("Error: PRD.md not found. Run 'mx init' first.")
        sys.exit(1)

    if not os.environ.get("GITHUB_TOKEN") and not config.github.token:
        print("Error: GITHUB_TOKEN not set")
        sys.exit(1)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Warning: ANTHROPIC_API_KEY not set - using default LLM config")

    print(f"Starting MX Swarm...")
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
    tasks = []

    # 1. Run planner first
    print("[1/3] Running planner...")
    planner = Planner(config, repo_path=".")
    result = await planner.run()
    print(f"  Generated {result['tasks_generated']} tasks from {result['spec_requirements']} requirements")

    if result['tasks_published'] == 0:
        print("  No tasks to execute. Check PRD.md and SPEC.json")
        return

    # 2. Start workers
    print(f"\n[2/3] Starting {config.worker.max_workers} workers...")
    workers = []
    for i in range(config.worker.max_workers):
        worker = Worker(config, worker_id=f"worker-{i}")
        workers.append(worker)

    # 3. Start judge
    print(f"\n[3/3] Starting judge (interval: {config.judge.interval_minutes}min)...")
    judge = Judge(config, repo_path=".")

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

    try:
        # Wait for shutdown or completion
        await shutdown_event.wait()
    finally:
        # Cancel all tasks
        for task in worker_tasks:
            task.cancel()
        judge_task.cancel()

        # Wait for cleanup
        await asyncio.gather(*worker_tasks, judge_task, return_exceptions=True)

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
        print(f"\nTEAMPLAN (Org-as-Code):")
        print(f"  Topology: {teamplan.topology.value}")
        print(f"  Streams:")
        for s in teamplan.streams:
            print(f"    - {s.name}: {s.target_workers}/{s.max_workers} workers")
        print(f"  Total: {teamplan.total_target_workers()} workers (max {teamplan.total_max_workers()})")
        print(f"  Scores: complexity={teamplan.complexity_score:.2f}, coupling={teamplan.coupling_score:.2f}, risk={teamplan.risk_score:.2f}")
        print(f"  Caps: {teamplan.caps.pr_cap_per_hour} PR/hr, {teamplan.caps.open_pr_cap} open PRs")
    else:
        print(f"\nNo TEAMPLAN.json - run 'mx ignite' to generate")

    # Config status
    print(f"\nConfiguration:")
    print(f"  Repository: {config.github.repo_owner}/{config.github.repo_name}")
    print(f"  Max workers: {config.worker.max_workers}")
    print(f"  Budget: ${config.budget.daily_usd_cap}/day")
    print(f"  Paused: {config.paused}")

    # Spec/progress status
    if spec:
        progress = spec.progress_summary()
        print(f"\nProgress:")
        print(f"  Requirements: {progress['completed']}/{progress['total']} ({progress['percent']}%)")
        print(f"  In Progress: {progress['in_progress']}")
        print(f"  Pending: {progress['pending']}")
        print(f"  Blocked: {progress.get('blocked', 0)}")
    else:
        print(f"\nNo SPEC.json found - run 'mx run' or 'mx plan' first")

    # Queue status
    try:
        queue = create_queue_adapter(config.queue.to_dict())
        await queue.connect()
        depth = await queue.get_queue_depth()
        await queue.disconnect()
        print(f"\nQueue:")
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

    print(f"\nPlanner Results:")
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

        print(f"\nJudge Report:")
        print(f"  Decision: {report.decision.value.upper()}")
        print(f"  Reason: {report.reason}")
        print(f"\nMetrics:")
        for key, value in report.metrics.items():
            print(f"    {key}: {value}")

        if report.actions_taken:
            print(f"\nActions:")
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

    print(f"\n  PRD Analysis Scores:")
    print(f"    Complexity: {teamplan.complexity_score:.2f}")
    print(f"    Coupling:   {teamplan.coupling_score:.2f}")
    print(f"    Risk:       {teamplan.risk_score:.2f}")

    print(f"\n  TEAMPLAN Generated:")
    print(f"    Topology: {teamplan.topology.value}")
    print(f"    Streams: {len(teamplan.streams)}")
    for s in teamplan.streams:
        print(f"      - {s.name}: {s.target_workers} workers (max {s.max_workers})")
    print(f"    Total Workers: {teamplan.total_target_workers()} (max {teamplan.total_max_workers()})")

    print(f"\n  Governance Caps:")
    print(f"    PR/hour: {teamplan.caps.pr_cap_per_hour}")
    print(f"    Open PRs: {teamplan.caps.open_pr_cap}")
    print(f"    Active tasks: {teamplan.caps.max_active_tasks}")

    print(f"\n  Risk Merge Policy:")
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

    print(f"\n  Planner Results:")
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
        default=8000,
        help="Port to bind to",
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
