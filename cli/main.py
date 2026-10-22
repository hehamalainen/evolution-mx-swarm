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
from shared.schemas import RequirementStatus, Spec
from shared.schemas.teamplan import TeamPlan
from shared.utils import MXConfig
from shared.utils.control_plane import build_control_plane_snapshot, render_mxtop
from shared.utils.metrics import get_budget_snapshot
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


async def llm_preflight(config: MXConfig) -> tuple[bool, str]:
    """Validate LLM configuration and basic connectivity before planner/worker runs."""
    provider = (config.llm.provider or "").lower()

    if provider == "anthropic":
        if not (config.llm.api_key or os.environ.get("ANTHROPIC_API_KEY")):
            return False, "ANTHROPIC_API_KEY is missing for anthropic provider."
        return True, ""

    if provider == "openai":
        if not (config.llm.api_key or os.environ.get("OPENAI_API_KEY")):
            return False, "OPENAI_API_KEY is missing for openai provider."
        return True, ""

    if provider == "ollama":
        import urllib.error
        import urllib.parse
        import urllib.request

        base_url = (config.llm.base_url or "http://localhost:11434").rstrip("/")
        health_url = f"{base_url}/api/tags"
        if urllib.parse.urlparse(health_url).scheme not in {"http", "https"}:
            return False, f"Unsupported Ollama URL scheme in {health_url}"
        req = urllib.request.Request(health_url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=3):  # nosec B310
                return True, ""
        except (urllib.error.URLError, OSError) as exc:
            return False, f"Ollama is unreachable at {health_url}: {exc}"

    return True, ""


def _model_safe_worker_cap(config: MXConfig) -> int:
    """Return a conservative worker cap for local/weak model profiles."""
    provider = (config.llm.provider or "").lower()
    model = (config.llm.model or "").lower()
    if provider == "ollama" and "7b" in model:
        return 1
    return max(1, config.worker.max_workers)


async def _get_initial_queue_depth(config: MXConfig) -> int | None:
    """Best-effort queue depth lookup before worker launch."""
    queue = None
    try:
        queue = create_queue_adapter(config.queue.to_dict())
        await queue.connect()
        depth = await queue.get_queue_depth()
        if isinstance(depth, int) and depth >= 0:
            return depth
    except Exception:
        return None
    finally:
        if queue is not None:
            try:
                await queue.disconnect()
            except Exception:
                pass
    return None


def _resolve_effective_workers(
    requested_workers: int,
    model_cap: int,
    published_tasks: int,
    queue_depth: int | None,
) -> int:
    """Clamp workers by task availability and model safety profile."""
    caps = [max(1, requested_workers), max(1, model_cap)]
    if published_tasks > 0:
        caps.append(published_tasks)
    elif isinstance(queue_depth, int) and queue_depth > 0:
        caps.append(queue_depth)
    return max(1, min(caps))


def _reset_stale_in_progress_requirements(spec: Spec) -> int:
    """Reset stale IN_PROGRESS requirements so planner can regenerate tasks."""
    resets = 0
    for req in spec.requirements:
        if req.status == RequirementStatus.IN_PROGRESS and req.assigned_tasks:
            req.status = RequirementStatus.PENDING
            req.assigned_tasks = []
            req.completion_evidence.append(
                "Auto-reset stale in-progress requirement (queue empty at startup)"
            )
            resets += 1
    return resets


async def _run_python_tool(script_name: str, extra_args: list[str]) -> None:
    """Run a project script as a first-class CLI subcommand."""
    script_path = os.path.join(_project_root, "scripts", script_name)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        script_path,
        *extra_args,
    )
    return_code = await proc.wait()
    if return_code != 0:
        raise RuntimeError(f"{script_name} exited with code {return_code}")


def _create_planner(config: MXConfig, *, repo_path: str, prd_path: str) -> Planner:
    """Build planner with compatibility for older test doubles."""
    try:
        return Planner(config, repo_path=repo_path, prd_path=prd_path)
    except TypeError:
        return Planner(config, repo_path=repo_path)


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
    task_status_map: dict[str, str] = {}
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

        prd_path = getattr(args, "prd_path", None) or "PRD.md"

        # Check prerequisites
        if not os.path.exists(prd_path):
            print(f"Error: PRD not found at {prd_path}.")
            sys.exit(1)

        llm_ok, llm_error = await llm_preflight(config)
        if not llm_ok:
            raise RuntimeError(f"LLM preflight failed: {llm_error}")

        # 0. Sync with Aether (Status Sync Phase 1)
        if config.vcs.provider == "aether":
            spec = load_spec(".")
            if spec:
                await sync_spec_with_aether(spec, config)
                spec.save("SPEC.json")

        if not os.environ.get("GITHUB_TOKEN") and not config.github.token:
            print("Warning: GITHUB_TOKEN not set - Enabling Local Mode (Mock GitHub)")

        # Update config with CLI args
        if args.workers:
            config.worker.max_workers = args.workers
        requested_workers = max(1, config.worker.max_workers)
        model_safe_cap = _model_safe_worker_cap(config)

        print("Starting MX Swarm...")
        print(f"  Repository: {config.github.repo_owner}/{config.github.repo_name}")
        print(f"  PRD: {prd_path}")
        print(f"  Max workers (requested): {requested_workers}")
        print(f"  LLM: {config.llm.provider}/{config.llm.model}")
        if model_safe_cap < requested_workers:
            print(
                f"  Worker safety cap active for model profile: "
                f"{requested_workers} -> {model_safe_cap}"
            )
        print()

        config.paused = False
        config.save()

        # Run components

        # 1. Run planner first
        print("[1/3] Running planner...")
        planner = _create_planner(config, repo_path=".", prd_path=prd_path)

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

        queue_depth = await _get_initial_queue_depth(config)
        if (
            result["tasks_published"] == 0
            and queue_depth == 0
        ):
            reset_count = _reset_stale_in_progress_requirements(spec)
            if reset_count > 0:
                spec.save("SPEC.json")
                print(
                    f"  Reset {reset_count} stale in-progress requirements "
                    f"(queue empty). Regenerating tasks..."
                )
                tasks = await planner.generate_tasks(spec)
                published = await planner.publish_tasks(tasks)
                result["tasks_generated"] = len(tasks)
                result["tasks_published"] = published
                print(
                    f"  Regenerated {len(tasks)} tasks "
                    f"and published {published} task(s)"
                )
                queue_depth = await _get_initial_queue_depth(config)

        effective_workers = _resolve_effective_workers(
            requested_workers=requested_workers,
            model_cap=model_safe_cap,
            published_tasks=result["tasks_published"],
            queue_depth=queue_depth,
        )

        # 2. Start workers
        if effective_workers < requested_workers:
            print(
                f"\n[2/3] Starting {effective_workers} workers "
                f"(requested {requested_workers}, queue_depth={queue_depth})..."
            )
        else:
            print(f"\n[2/3] Starting {effective_workers} workers...")
        workers = []
        for i in range(effective_workers):
            worker = Worker(config, worker_id=f"worker-{i}")
            workers.append(worker)

        # 3. Start judge
        print(f"\n[3/3] Starting judge (interval: {config.judge.interval_minutes}min)...")
        judge = Judge(config, repo_path=".")

        # Track metrics for dashboard heartbeat
        swarm_metrics = {
            "active_workers": effective_workers,
            "tasks_completed": 0,
            "prs_opened": 0,
            "prs_merged": 0,
            "ci_passes": 0,
            "ci_fails": 0,
            "current_phase": "running",
        }

        # Handle shutdown (signal or judge-initiated stop)
        shutdown_event = asyncio.Event()
        shutdown_reason = {"source": "signal", "detail": ""}

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
            except asyncio.CancelledError:
                raise
            except Exception as e:
                shutdown_reason["source"] = "judge_error"
                shutdown_reason["detail"] = str(e)
                shutdown_event.set()
                raise
            else:
                shutdown_reason["source"] = "judge"
                shutdown_reason["detail"] = "Judge loop completed"
                shutdown_event.set()
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
                    submissions_total = sum(w.metrics.prs_opened for w in workers)
                    online_workers = len(workers)

                    queue_depth = None
                    if workers:
                        try:
                            queue_depth = await workers[0].queue.get_queue_depth()
                        except Exception:
                            queue_depth = None

                    vcs_provider = config.vcs.provider
                    prs_opened = submissions_total if vcs_provider == "github" else 0
                    submissions_opened = submissions_total if vcs_provider == "aether" else 0

                    # Send heartbeat
                    payload = {
                        "active_workers": online_workers,
                        "target_workers": effective_workers,
                        "runtime_cap": effective_workers,
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
                        "is_running": not shutdown_event.is_set(),
                        "current_phase": swarm_metrics["current_phase"],
                    }

                    from shared.utils.remote_logger import (
                        build_dashboard_auth_headers,
                        post_dashboard_json_sync,
                    )

                    hb_url = f"{dashboard_url}/api/swarm/heartbeat"
                    hb_headers = build_dashboard_auth_headers()
                    if use_aiohttp:
                        async with aiohttp.ClientSession() as session:
                            async with session.post(
                                hb_url,
                                json=payload,
                                headers=hb_headers,
                                timeout=aiohttp.ClientTimeout(total=2),
                            ) as resp:
                                if resp.status >= 400:
                                    sys.stderr.write(
                                        f"[heartbeat] {hb_url} rejected heartbeat with HTTP {resp.status}\n"
                                    )
                    else:
                        await asyncio.get_running_loop().run_in_executor(
                            None,
                            lambda u=hb_url, p=payload: post_dashboard_json_sync(
                                u,
                                p,
                                timeout=2.0,
                                error_stream=sys.stderr,
                            ),
                        )

                except Exception:
                    pass  # Ignore errors - dashboard might not be running

                await asyncio.sleep(heartbeat_interval)

        def signal_handler():
            print("\n\nShutdown signal received...")
            shutdown_reason["source"] = "signal"
            shutdown_reason["detail"] = "External signal"
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
            if shutdown_reason["source"] == "judge":
                print("\nJudge completed run loop and requested shutdown.")
            elif shutdown_reason["source"] == "judge_error":
                print(f"\nJudge failed and triggered shutdown: {shutdown_reason['detail']}")
        finally:
            swarm_metrics["current_phase"] = "stopping"
            # Cancel all tasks
            for task in worker_tasks:
                task.cancel()
            if not judge_task.done():
                judge_task.cancel()
            heartbeat_task.cancel()

            # Wait for cleanup
            await asyncio.gather(*worker_tasks, judge_task, heartbeat_task, return_exceptions=True)

    finally:
        if remote_logger:
            await remote_logger.stop()

    print("\nSwarm stopped.")


async def cmd_gate2(args):
    """Run the proven local parallel Gate 2 corpus from the CLI."""
    cmd = [
        "--runs",
        str(args.runs),
        "--workers",
        str(args.workers),
        "--timeout-seconds",
        str(args.timeout_seconds),
    ]
    if args.corpus:
        cmd.extend(["--corpus", args.corpus])
    await _run_python_tool("run_gate2_local_parallel.py", cmd)


async def cmd_qualify(args):
    """Run reusable model qualification from the CLI."""
    cmd = ["--model-label", args.model_label]
    if args.corpus:
        cmd.extend(["--corpus", args.corpus])
    if args.runs is not None:
        cmd.extend(["--runs", str(args.runs)])
    if args.workers is not None:
        cmd.extend(["--workers", str(args.workers)])
    if args.timeout_seconds is not None:
        cmd.extend(["--timeout-seconds", str(args.timeout_seconds)])
    await _run_python_tool("run_model_qualification.py", cmd)


async def cmd_proof(args):
    """Run a single end-to-end delivery proof from the CLI."""
    retry_max = getattr(args, "retry_max", 0)
    cmd = [
        "--slice",
        args.slice,
        "--workers",
        str(args.workers),
        "--timeout-seconds",
        str(args.timeout_seconds),
        "--retry-max",
        str(retry_max),
    ]
    if args.corpus:
        cmd.extend(["--corpus", args.corpus])
    await _run_python_tool("run_delivery_proof.py", cmd)


async def cmd_aether_adapter_proof(args):
    """Run the deterministic MX -> Aether adapter regression proof."""
    cmd: list[str] = []
    if args.aether_base_url:
        cmd.extend(["--aether-base-url", args.aether_base_url])
    if args.repo_name:
        cmd.extend(["--repo-name", args.repo_name])
    if args.run_id:
        cmd.extend(["--run-id", args.run_id])
    cmd.extend(["--poll-attempts", str(args.poll_attempts)])
    cmd.extend(["--poll-interval-seconds", str(args.poll_interval_seconds)])
    await _run_python_tool("run_aether_adapter_proof.py", cmd)


async def cmd_aether_worker_proof(args):
    """Run the canned worker -> Aether regression proof."""
    cmd = [
        "--slice",
        args.slice,
        "--timeout-seconds",
        str(args.timeout_seconds),
    ]
    if args.aether_base_url:
        cmd.extend(["--aether-base-url", args.aether_base_url])
    if args.repo_name:
        cmd.extend(["--repo-name", args.repo_name])
    if args.run_id:
        cmd.extend(["--run-id", args.run_id])
    if args.fixture:
        cmd.extend(["--fixture", args.fixture])
    await _run_python_tool("run_aether_worker_proof.py", cmd)


async def cmd_aether_live_proof(args):
    """Run the model-backed local/live MX -> Aether proof."""
    cmd = [
        "--slice",
        args.slice,
        "--timeout-seconds",
        str(args.timeout_seconds),
    ]
    if args.corpus:
        cmd.extend(["--corpus", args.corpus])
    if args.aether_base_url:
        cmd.extend(["--aether-base-url", args.aether_base_url])
    if args.repo_name:
        cmd.extend(["--repo-name", args.repo_name])
    if args.run_id:
        cmd.extend(["--run-id", args.run_id])
    if args.report_root:
        cmd.extend(["--report-root", args.report_root])
    if args.llm_provider:
        cmd.extend(["--llm-provider", args.llm_provider])
    if args.llm_model:
        cmd.extend(["--llm-model", args.llm_model])
    if args.llm_base_url:
        cmd.extend(["--llm-base-url", args.llm_base_url])
    if args.llm_api_key_env:
        cmd.extend(["--llm-api-key-env", args.llm_api_key_env])
    if args.canned_completion_file:
        cmd.extend(["--canned-completion-file", args.canned_completion_file])
    await _run_python_tool("run_aether_live_proof.py", cmd)


async def cmd_scale_control_plane(args):
    """Run the hybrid control-plane scale harness."""
    cmd = [
        "--logical-lanes",
        str(args.logical_lanes),
        "--seed",
        str(args.seed),
        "--report-root",
        args.report_root,
    ]
    if args.logical_tasks is not None:
        cmd.extend(["--logical-tasks", str(args.logical_tasks)])
    await _run_python_tool("run_scale_control_plane.py", cmd)


async def cmd_scale_worker_load(args):
    """Run the canned worker-load scale harness."""
    sink_mode = getattr(args, "sink_mode", "mock_github")
    cmd = [
        "--logical-workers",
        str(args.logical_workers),
        "--active-concurrency",
        str(args.active_concurrency),
        "--slice",
        args.slice,
        "--timeout-seconds",
        str(args.timeout_seconds),
        "--sink-mode",
        sink_mode,
        "--report-root",
        args.report_root,
    ]
    await _run_python_tool("run_scale_worker_load.py", cmd)


async def cmd_quarantine_audit(args):
    """Inventory Moltbook-linked boundary surface and secret hits."""
    cmd = ["--report-root", args.report_root]
    if args.fail_on_secrets:
        cmd.append("--fail-on-secrets")
    await _run_python_tool("run_quarantine_audit.py", cmd)


async def cmd_gcp_stage(args):
    """Prepare GCP Gate A/B staging artifacts from the CLI."""
    cmd = [
        "--gate",
        args.gate,
        "--project-id",
        args.project_id,
    ]
    if args.corpus:
        cmd.extend(["--corpus", args.corpus])
    if args.topic_prefix:
        cmd.extend(["--topic-prefix", args.topic_prefix])
    if args.subscription_prefix:
        cmd.extend(["--subscription-prefix", args.subscription_prefix])
    if args.config:
        cmd.extend(["--config", args.config])
    await _run_python_tool("run_gcp_gate_ab.py", cmd)


async def cmd_frontier_smoke(args):
    """Run a single constrained slice directly through the frontier lane."""
    cmd = [
        "--slice",
        args.slice,
        "--timeout-seconds",
        str(args.timeout_seconds),
    ]
    if args.corpus:
        cmd.extend(["--corpus", args.corpus])
    if args.provider:
        cmd.extend(["--provider", args.provider])
    if args.model:
        cmd.extend(["--model", args.model])
    if args.base_url:
        cmd.extend(["--base-url", args.base_url])
    if getattr(args, "api_key", None):
        cmd.extend(["--api-key", args.api_key])
    await _run_python_tool("run_frontier_smoke.py", cmd)


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
    llm_ok, llm_error = await llm_preflight(config)
    if not llm_ok:
        raise RuntimeError(f"LLM preflight failed: {llm_error}")

    print("Running planner...")
    prd_path = getattr(args, "prd_path", None) or "PRD.md"
    if not os.path.exists(prd_path):
        print(f"Error: PRD not found at {prd_path}.")
        sys.exit(1)

    planner = _create_planner(config, repo_path=".", prd_path=prd_path)
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
    """Show or set budget limits."""
    config = load_config(args.config)
    changed = False

    if args.daily:
        config.budget.daily_usd_cap = args.daily
        print(f"Daily budget cap set to ${args.daily}")
        changed = True
    if args.frontier_daily is not None:
        config.budget.frontier_daily_usd_cap = args.frontier_daily
        print(f"Frontier daily budget cap set to ${args.frontier_daily}")
        changed = True
    if args.control_plane_daily is not None:
        config.budget.control_plane_daily_usd_cap = args.control_plane_daily
        print(f"Control-plane daily budget cap set to ${args.control_plane_daily}")
        changed = True
    if args.bulk_lane_daily is not None:
        config.budget.bulk_lane_daily_usd_cap = args.bulk_lane_daily
        print(f"Bulk-lane daily budget cap set to ${args.bulk_lane_daily}")
        changed = True
    if args.frontier_hourly is not None:
        config.budget.max_frontier_escalations_per_hour = args.frontier_hourly
        print(f"Frontier escalation hourly cap set to {args.frontier_hourly}")
        changed = True
    if args.frontier_concurrency is not None:
        config.budget.max_frontier_concurrency = args.frontier_concurrency
        print(f"Frontier concurrency cap set to {args.frontier_concurrency}")
        changed = True
    if args.frontier_attempts is not None:
        config.budget.max_frontier_attempts_per_task_contract = args.frontier_attempts
        print(f"Frontier attempts per task contract set to {args.frontier_attempts}")
        changed = True
    if args.cost_per_success is not None:
        config.budget.cost_per_success_ceiling_usd = args.cost_per_success
        print(f"Cost-per-success ceiling set to ${args.cost_per_success}")
        changed = True
    if args.freeze_mode:
        config.budget.freeze_mode = args.freeze_mode
        print(f"Freeze mode set to {args.freeze_mode}")
        changed = True

    if changed:
        config.save()

    snapshot = get_budget_snapshot(config.budget)
    print("\nBudget Snapshot:")
    print(f"  Total spend today: ${snapshot['total_spend_today_usd']:.6f} / ${config.budget.daily_usd_cap:.2f}")
    print(
        f"  Frontier spend today: ${snapshot['frontier_spend_today_usd']:.6f} "
        f"/ ${config.budget.frontier_daily_usd_cap:.2f}"
    )
    print(
        f"  Control-plane spend today: ${snapshot['control_plane_spend_today_usd']:.6f} "
        f"/ ${config.budget.control_plane_daily_usd_cap:.2f}"
    )
    print(
        f"  Frontier claims last hour: {snapshot['frontier_claims_last_hour']} "
        f"/ {config.budget.max_frontier_escalations_per_hour}"
    )
    print(
        f"  Active frontier claims: {snapshot['active_frontier_claims']} "
        f"/ {config.budget.max_frontier_concurrency}"
    )
    print(
        f"  Cost per success: ${snapshot['cost_per_success_usd']:.6f} "
        f"(ceiling ${config.budget.cost_per_success_ceiling_usd:.2f})"
    )
    print(f"  Freeze mode: {config.budget.freeze_mode}")
    print(f"  Frontier frozen: {snapshot['frontier_frozen']}")
    print(f"  Total frozen: {snapshot['total_frozen']}")


async def cmd_top(args):
    """Show control-plane telemetry instead of per-agent chatter."""
    config = load_config(args.config)
    snapshot = build_control_plane_snapshot(
        repo_path=".",
        budget_config=config.budget,
        limit_rigs=args.limit_rigs,
    )
    print(render_mxtop(snapshot))


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
    llm_ok, llm_error = await llm_preflight(config)
    if not llm_ok:
        raise RuntimeError(f"LLM preflight failed: {llm_error}")
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

    from dashboard.api import app, assert_dashboard_bind_safe

    assert_dashboard_bind_safe(args.host)
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
    run_parser.add_argument(
        "--prd-path",
        help="Alternate PRD path relative to the repo root",
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
    plan_parser.add_argument(
        "--prd-path",
        help="Alternate PRD path relative to the repo root",
    )

    gate2_parser = subparsers.add_parser("gate2", help="Run the proven local Gate 2 corpus")
    gate2_parser.add_argument("--runs", type=int, default=3, help="Number of consecutive runs")
    gate2_parser.add_argument("--workers", type=int, default=5, help="Number of workers")
    gate2_parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=1200,
        help="Timeout per isolated run",
    )
    gate2_parser.add_argument(
        "--corpus",
        default="qualification/corpora/gate2_local_open_weight.json",
        help="Qualification corpus manifest",
    )

    qualify_parser = subparsers.add_parser("qualify", help="Run reusable model qualification")
    qualify_parser.add_argument(
        "--corpus",
        default="qualification/corpora/gate2_local_open_weight.json",
        help="Qualification corpus manifest",
    )
    qualify_parser.add_argument("--runs", type=int, help="Override corpus default run count")
    qualify_parser.add_argument("--workers", type=int, help="Override corpus default worker count")
    qualify_parser.add_argument("--timeout-seconds", type=int, help="Override corpus timeout")
    qualify_parser.add_argument(
        "--model-label",
        default="unspecified-model",
        help="Label to store in the qualification report",
    )

    proof_parser = subparsers.add_parser("proof", help="Run one end-to-end delivery proof")
    proof_parser.add_argument("--slice", default="jsonpeek", help="Slice key to prove")
    proof_parser.add_argument(
        "--corpus",
        default="qualification/corpora/gate2_local_open_weight.json",
        help="Qualification corpus manifest",
    )
    proof_parser.add_argument("--workers", type=int, default=1, help="Worker count for the proof")
    proof_parser.add_argument("--timeout-seconds", type=int, default=1200, help="Timeout for the proof")
    proof_parser.add_argument("--retry-max", type=int, default=0, help="Maximum retries per task during proof runs")

    aether_adapter_proof_parser = subparsers.add_parser(
        "aether-adapter-proof",
        help="Run the deterministic MX -> Aether adapter regression proof",
    )
    aether_adapter_proof_parser.add_argument(
        "--aether-base-url",
        default="http://127.0.0.1:8100",
        help="Aether base URL",
    )
    aether_adapter_proof_parser.add_argument("--repo-name", default=None, help="Optional Aether repository name")
    aether_adapter_proof_parser.add_argument("--run-id", default=None, help="Explicit MX run identifier")
    aether_adapter_proof_parser.add_argument("--poll-attempts", type=int, default=40, help="Maximum polling attempts")
    aether_adapter_proof_parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=1.0,
        help="Polling interval for visibility and terminal status",
    )

    aether_worker_proof_parser = subparsers.add_parser(
        "aether-worker-proof",
        help="Run the canned worker -> Aether regression proof",
    )
    aether_worker_proof_parser.add_argument("--slice", default="jsonpeek", help="Qualification slice to run")
    aether_worker_proof_parser.add_argument("--timeout-seconds", type=int, default=1200, help="Proof timeout")
    aether_worker_proof_parser.add_argument(
        "--aether-base-url",
        default="http://127.0.0.1:8100",
        help="Aether base URL",
    )
    aether_worker_proof_parser.add_argument("--repo-name", default=None, help="Optional Aether repository name")
    aether_worker_proof_parser.add_argument("--run-id", default=None, help="Explicit MX run identifier")
    aether_worker_proof_parser.add_argument(
        "--fixture",
        default="proof_fixtures/jsonpeek_canned_completion.json",
        help="Canned completion fixture JSON",
    )

    aether_live_proof_parser = subparsers.add_parser(
        "aether-live-proof",
        help="Run the model-backed MX -> Aether live proof",
    )
    aether_live_proof_parser.add_argument("--slice", default="jsonpeek", help="Qualification slice to run")
    aether_live_proof_parser.add_argument(
        "--corpus",
        default=None,
        help="Optional qualification corpus manifest",
    )
    aether_live_proof_parser.add_argument("--timeout-seconds", type=int, default=1200, help="Proof timeout")
    aether_live_proof_parser.add_argument(
        "--aether-base-url",
        default="http://127.0.0.1:8100",
        help="Aether base URL",
    )
    aether_live_proof_parser.add_argument("--repo-name", default=None, help="Optional Aether repository name")
    aether_live_proof_parser.add_argument("--run-id", default=None, help="Explicit MX run identifier")
    aether_live_proof_parser.add_argument("--report-root", default=None, help="Optional report root override")
    aether_live_proof_parser.add_argument("--llm-provider", default=None, help="Override llm.provider")
    aether_live_proof_parser.add_argument("--llm-model", default=None, help="Override llm.model")
    aether_live_proof_parser.add_argument("--llm-base-url", default=None, help="Override llm.base_url")
    aether_live_proof_parser.add_argument("--llm-api-key-env", default=None, help="Env var name for llm.api_key")
    aether_live_proof_parser.add_argument(
        "--canned-completion-file",
        default=None,
        help="Optional canned completion fixture JSON",
    )

    scale_control_plane_parser = subparsers.add_parser(
        "scale-control-plane",
        help="Run the hybrid 1000-lane control-plane scale harness",
    )
    scale_control_plane_parser.add_argument("--logical-lanes", type=int, default=1000, help="Logical lane count")
    scale_control_plane_parser.add_argument("--logical-tasks", type=int, default=None, help="Optional task count")
    scale_control_plane_parser.add_argument("--seed", type=int, default=7, help="Deterministic RNG seed")
    scale_control_plane_parser.add_argument(
        "--report-root",
        default="reports/scale_control_plane",
        help="Report root directory",
    )

    scale_worker_load_parser = subparsers.add_parser(
        "scale-worker-load",
        help="Run the canned worker-load scale harness",
    )
    scale_worker_load_parser.add_argument("--logical-workers", type=int, default=100, help="Logical worker count")
    scale_worker_load_parser.add_argument(
        "--active-concurrency",
        type=int,
        default=10,
        help="Maximum concurrent isolated worker proofs",
    )
    scale_worker_load_parser.add_argument("--slice", default="jsonpeek", help="Qualification slice to run")
    scale_worker_load_parser.add_argument("--timeout-seconds", type=int, default=1200, help="Per-proof timeout")
    scale_worker_load_parser.add_argument(
        "--sink-mode",
        choices=("mock_github", "aether"),
        default="mock_github",
        help="Sink mode for isolated proofs",
    )
    scale_worker_load_parser.add_argument(
        "--report-root",
        default="reports/scale_worker_load",
        help="Report root directory",
    )

    quarantine_audit_parser = subparsers.add_parser(
        "quarantine-audit",
        help="Inventory MX vs Moltbook boundary surface and secret hits",
    )
    quarantine_audit_parser.add_argument(
        "--report-root",
        default="reports/quarantine_audit",
        help="Report root directory",
    )
    quarantine_audit_parser.add_argument(
        "--fail-on-secrets",
        action="store_true",
        help="Exit non-zero if live Moltbook credentials are detected",
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
    budget_parser.add_argument("--frontier-daily", type=float, help="Frontier daily USD cap")
    budget_parser.add_argument("--control-plane-daily", type=float, help="Control-plane daily USD cap")
    budget_parser.add_argument("--bulk-lane-daily", type=float, help="Bulk lane daily USD cap")
    budget_parser.add_argument("--frontier-hourly", type=int, help="Max frontier escalations per hour")
    budget_parser.add_argument("--frontier-concurrency", type=int, help="Max concurrent frontier claims")
    budget_parser.add_argument("--frontier-attempts", type=int, help="Max frontier attempts per task contract")
    budget_parser.add_argument("--cost-per-success", type=float, help="Frontier cost-per-success ceiling")
    budget_parser.add_argument(
        "--freeze-mode",
        choices=["frontier_only", "all_new_claims", "throttle_only"],
        help="Budget freeze behavior once caps are reached",
    )

    top_parser = subparsers.add_parser(
        "top",
        aliases=["mxtop"],
        help="Show control-plane telemetry for rigs, lanes, holds, and governor recommendations",
    )
    top_parser.add_argument("--limit-rigs", type=int, default=5, help="Number of rigs to display")

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

    gcp_stage_parser = subparsers.add_parser("gcp-stage", help="Prepare GCP Gate A/B staging artifacts")
    gcp_stage_parser.add_argument("--gate", choices=["A", "B"], required=True, help="Gate profile to prepare")
    gcp_stage_parser.add_argument("--project-id", required=True, help="GCP project id")
    gcp_stage_parser.add_argument(
        "--corpus",
        default="qualification/corpora/gate2_local_open_weight.json",
        help="Qualification corpus manifest",
    )
    gcp_stage_parser.add_argument("--topic-prefix", default="mx-gate", help="Pub/Sub topic prefix")
    gcp_stage_parser.add_argument(
        "--subscription-prefix",
        default="mx-gate",
        help="Pub/Sub subscription prefix",
    )
    gcp_stage_parser.add_argument(
        "--config",
        default="mx.config.cloud.json",
        help="Cloud config used for frontier budget validation",
    )

    frontier_smoke_parser = subparsers.add_parser("frontier-smoke", help="Run one constrained slice through the frontier lane")
    frontier_smoke_parser.add_argument("--slice", default="jsonpeek", help="Slice key to run")
    frontier_smoke_parser.add_argument(
        "--corpus",
        default="qualification/corpora/gate2_local_open_weight.json",
        help="Qualification corpus manifest",
    )
    frontier_smoke_parser.add_argument("--provider", help="Override frontier provider")
    frontier_smoke_parser.add_argument("--model", help="Override frontier model")
    frontier_smoke_parser.add_argument("--base-url", help="Override frontier base URL")
    frontier_smoke_parser.add_argument("--api-key", help="Override frontier API key")
    frontier_smoke_parser.add_argument("--timeout-seconds", type=int, default=1200, help="Timeout per run")

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
        "gate2": cmd_gate2,
        "qualify": cmd_qualify,
        "proof": cmd_proof,
        "aether-adapter-proof": cmd_aether_adapter_proof,
        "aether-worker-proof": cmd_aether_worker_proof,
        "aether-live-proof": cmd_aether_live_proof,
        "scale-control-plane": cmd_scale_control_plane,
        "scale-worker-load": cmd_scale_worker_load,
        "quarantine-audit": cmd_quarantine_audit,
        "judge": cmd_judge,
        "budget": cmd_budget,
        "top": cmd_top,
        "mxtop": cmd_top,
        "ignite": cmd_ignite,
        "dashboard": cmd_dashboard,
        "gcp-stage": cmd_gcp_stage,
        "frontier-smoke": cmd_frontier_smoke,
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
