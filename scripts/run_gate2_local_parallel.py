#!/usr/bin/env python3
# ruff: noqa: E402
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(REPO_ROOT))

from planner.service import Planner
from shared.adapters.queue import create_queue_adapter
from shared.schemas import ModelTier, RoutingTarget, TaskContract, TaskState
from shared.utils import MXConfig
from shared.utils.llm import Message, create_llm_client
from shared.utils.metrics import MetricsCollector
from shared.utils.qualification import load_qualification_corpus
from shared.utils.task_registry import load_task_snapshot
from worker.service import Worker


def _resolve_python_executable(repo_root: Path) -> Path:
    override = os.environ.get("MX_PYTHON_EXECUTABLE", "").strip()
    if override:
        return Path(override)
    if sys.executable:
        active = Path(sys.executable)
        if active.exists():
            return active
    for candidate in (
        repo_root / ".venv" / "bin" / "python",
        repo_root / "venv" / "bin" / "python",
    ):
        if candidate.exists():
            return candidate
    return Path("python3")


VENV_PYTHON = _resolve_python_executable(REPO_ROOT)
STATE_RESET_PATHS = [
    ".runtime",
    ".env",
    "SPEC.json",
    "TEAMPLAN.json",
    "JUDGE_REPORT.md",
    "mock_prs.json",
]
COPY_EXCLUDES = {
    ".git",
    ".venv",
    ".pytest_cache",
    ".mypy_cache",
    "__pycache__",
    "node_modules",
    ".runtime",
    ".coverage",
}
TERMINAL_STATES = {"settled_ack", "settled_nack", "dead_letter"}
LIFECYCLE_BUG_REASONS = {"ERR_QUEUE_LEASE"}


@dataclass(frozen=True)
class SliceSpec:
    key: str
    prd_path: str
    implementation_path: str
    test_paths: list[str]
    prerequisite_keys: list[str] = field(default_factory=list)


DEFAULT_SLICES: list[SliceSpec] = [
    SliceSpec(
        key="jsonpeek",
        prd_path="prds/golden_path_002_jsonpeek.md",
        implementation_path="tools/jsonpeek.py",
        test_paths=["tests/test_jsonpeek.py"],
    ),
    SliceSpec(
        key="envpeek",
        prd_path="prds/golden_path_003_envpeek.md",
        implementation_path="tools/envpeek.py",
        test_paths=["tests/test_envpeek.py"],
    ),
    SliceSpec(
        key="tomlpeek",
        prd_path="prds/golden_path_004_tomlpeek.md",
        implementation_path="tools/tomlpeek.py",
        test_paths=["tests/test_tomlpeek.py"],
    ),
]


def load_slice_specs(corpus_path: str | None = None) -> list[SliceSpec]:
    if not corpus_path:
        return list(DEFAULT_SLICES)

    corpus = load_qualification_corpus(corpus_path)
    return [
        SliceSpec(
            key=slice.key,
            prd_path=slice.prd_path,
            implementation_path=slice.implementation_path,
            test_paths=list(slice.test_paths),
            prerequisite_keys=list(slice.prerequisite_keys),
        )
        for slice in corpus.slices
    ]


@dataclass
class SliceResult:
    key: str
    task_id: str
    goal: str
    prerequisite_keys: list[str]
    prerequisite_task_ids: list[str]
    terminal_state: str | None
    terminal_reason: str | None
    branch_name: str | None
    submission_id: str | None
    submission_url: str | None
    settled_at: str | None
    verification_passed: bool
    changed_files: list[str]
    blocked_event_count: int = 0
    last_blocked_reason: str | None = None
    failure_reason: str | None = None


@dataclass
class RunResult:
    run_number: int
    status: str
    worker_count: int
    false_success_rate: float
    duplicate_settlement_events: int
    duplicate_claim_events: int
    orphaned_workers_after_stop: int
    stop_convergence_seconds: float
    queue_depth_end: int
    dead_letter_depth_end: int
    dashboard_event_truthful: bool
    lifecycle_bug_events: list[dict[str, Any]] = field(default_factory=list)
    slices: list[SliceResult] = field(default_factory=list)
    run_dir: str = ""
    log_path: str = ""
    failure_reason: str | None = None


def _run(cmd: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
    )


def _copy_working_tree(run_dir: Path) -> None:
    _run(["git", "clone", "--no-hardlinks", str(REPO_ROOT), str(run_dir)], cwd=REPO_ROOT)

    def ignore(_src: str, names: list[str]) -> set[str]:
        return {name for name in names if name in COPY_EXCLUDES}

    for entry in REPO_ROOT.iterdir():
        if entry.name in COPY_EXCLUDES:
            continue
        destination = run_dir / entry.name
        if entry.is_dir():
            shutil.copytree(entry, destination, dirs_exist_ok=True, ignore=ignore)
        elif entry.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry, destination)


def _reset_run_state(run_dir: Path) -> None:
    for rel_path in STATE_RESET_PATHS:
        target = run_dir / rel_path
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink()


def _copy_required_assets(run_dir: Path, rel_paths: list[str]) -> None:
    for rel_path in rel_paths:
        source = REPO_ROOT / rel_path
        if not source.is_file():
            continue
        destination = run_dir / rel_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _current_source_branch() -> str:
    result = _run(["git", "branch", "--show-current"], cwd=REPO_ROOT)
    branch = result.stdout.strip()
    return branch or "main"


def _prepare_run_config(
    run_dir: Path,
    default_branch: str,
    worker_count: int,
    retry_max: int | None = None,
    frontier_llm: dict[str, Any] | None = None,
) -> None:
    config_path = run_dir / "mx.config.json"
    if not config_path.exists():
        return

    with open(config_path) as handle:
        payload = json.load(handle)

    secrets = payload.setdefault("secrets", {})
    env_config = secrets.setdefault("env", {})
    env_config["env_file"] = None

    github = payload.setdefault("github", {})
    github["token"] = None
    github["default_branch"] = default_branch

    worker = payload.setdefault("worker", {})
    worker["max_workers"] = worker_count
    worker["poll_interval_seconds"] = 1
    if retry_max is not None:
        worker["retry_max"] = retry_max
    if frontier_llm is not None:
        payload["frontier_llm"] = frontier_llm

    payload["paused"] = False

    with open(config_path, "w") as handle:
        json.dump(payload, handle, indent=2)


def _set_paused(run_dir: Path, paused: bool) -> None:
    config_path = run_dir / "mx.config.json"
    with open(config_path) as handle:
        payload = json.load(handle)
    payload["paused"] = paused
    with open(config_path, "w") as handle:
        json.dump(payload, handle, indent=2)


@contextlib.contextmanager
def _forced_local_execution_env() -> Any:
    tracked_keys = ("GITHUB_TOKEN", "MX_GITHUB_TOKEN", "MX_FORCE_LOCAL_MODE")
    previous = {key: os.environ.get(key) for key in tracked_keys}
    os.environ.pop("GITHUB_TOKEN", None)
    os.environ.pop("MX_GITHUB_TOKEN", None)
    os.environ["MX_FORCE_LOCAL_MODE"] = "1"
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                records.append(payload)
    return records


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _build_observability(run_dir: Path) -> dict[str, Any]:
    collector = MetricsCollector()
    collector.task_state_event_path = str(run_dir / ".runtime" / "task_state_events.jsonl")
    collector.submission_event_path = str(run_dir / ".runtime" / "submission_events.jsonl")
    collector.trace_span_path = str(run_dir / ".runtime" / "trace_spans.jsonl")
    collector.reconciliation_path = str(run_dir / ".runtime" / "aether_reconciliation_backlog.json")
    return collector.compute_event_observability(window_minutes=24 * 60)


async def _prewarm_local_llm(config: MXConfig) -> None:
    if os.environ.get("MX_CANNED_COMPLETION_FIXTURE", "").strip():
        print("[gate2] Skipping ollama prewarm due to canned completion fixture")
        return
    provider = (config.llm.provider or "").lower()
    if provider != "ollama":
        return

    client = create_llm_client(config.llm.to_dict())
    started = time.monotonic()
    await asyncio.wait_for(
        client.chat(
            messages=[Message(role="user", content="Reply with ok.")],
            system="Return only ok.",
            max_tokens=8,
            temperature=0.0,
        ),
        timeout=120,
    )
    elapsed = time.monotonic() - started
    print(f"[gate2] Prewarmed ollama model {config.llm.model} in {elapsed:.2f}s")


def _latest_terminal_events(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for event in events:
        state_to = str(event.get("state_to", "")).strip().lower()
        task_id = str(event.get("task_id", "")).strip()
        if not task_id or state_to not in TERMINAL_STATES:
            continue
        timestamp = str(event.get("timestamp", "")).strip()
        current = latest.get(task_id)
        if current is None or timestamp >= str(current.get("timestamp", "")):
            latest[task_id] = event
    return latest


def _duplicate_claim_events(events: list[dict[str, Any]]) -> int:
    counts: dict[tuple[str, str], int] = {}
    for event in events:
        if str(event.get("state_to", "")).strip().lower() != "claimed":
            continue
        task_id = str(event.get("task_id", "")).strip()
        attempt_id = str(event.get("attempt_id", "")).strip()
        if not task_id or not attempt_id:
            continue
        key = (task_id, attempt_id)
        counts[key] = counts.get(key, 0) + 1
    return sum(max(count - 1, 0) for count in counts.values())


def _lifecycle_bug_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for event in events:
        state_to = str(event.get("state_to", "")).strip().lower()
        reason = str(event.get("reason", "")).strip()
        if state_to in TERMINAL_STATES and reason in LIFECYCLE_BUG_REASONS:
            matches.append(event)
    return matches


def _latest_submission_by_task(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for event in events:
        task_id = str(event.get("task_id", "")).strip()
        if not task_id:
            continue
        timestamp = str(event.get("settled_at") or event.get("created_at") or "")
        current = latest.get(task_id)
        current_ts = str(current.get("settled_at") or current.get("created_at") or "") if current else ""
        if current is None or timestamp >= current_ts:
            latest[task_id] = event
    return latest


def _verify_branch(
    run_dir: Path,
    *,
    branch_name: str,
    implementation_path: str,
    test_paths: list[str],
) -> tuple[bool, list[str], str | None]:
    try:
        _run(["git", "rev-parse", "--verify", branch_name], cwd=run_dir)
    except subprocess.CalledProcessError as exc:
        return False, [], exc.stderr.strip() or exc.stdout.strip() or "branch not found"

    diff = _run(["git", "diff", "--name-only", "HEAD", branch_name], cwd=run_dir)
    changed_files = [line.strip() for line in diff.stdout.splitlines() if line.strip()]

    verify_dir = Path(tempfile.mkdtemp(prefix="mx_gate2_verify_"))
    try:
        _run(["git", "worktree", "add", "--detach", str(verify_dir), branch_name], cwd=run_dir)

        for rel_path in test_paths:
            source = run_dir / rel_path
            if source.exists():
                destination = verify_dir / rel_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)

        pytest_cmd = [str(VENV_PYTHON), "-m", "pytest", *test_paths, "-q"]
        ruff_cmd = [str(VENV_PYTHON), "-m", "ruff", "check", implementation_path, *test_paths]

        _run(pytest_cmd, cwd=verify_dir)
        _run(ruff_cmd, cwd=verify_dir)
    except subprocess.CalledProcessError as exc:
        error = exc.stderr.strip() or exc.stdout.strip() or "verification failed"
        return False, changed_files, error
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(verify_dir)],
            cwd=run_dir,
            check=False,
            capture_output=True,
            text=True,
        )
        shutil.rmtree(verify_dir, ignore_errors=True)

    if set(changed_files) != {implementation_path}:
        return False, changed_files, f"unexpected branch diff: {changed_files}"

    return True, changed_files, None


def _verify_aether_submission(
    *,
    submission_event: dict[str, Any],
    implementation_path: str,
) -> tuple[bool, list[str], str | None]:
    canonical_status = str(submission_event.get("canonical_status") or "").strip().lower()
    landing_verification_status = str(submission_event.get("landing_verification_status") or "").strip().lower()
    affected_paths = [
        str(path).strip()
        for path in (submission_event.get("affected_paths") or [])
        if str(path).strip()
    ]

    accepted_statuses = {"accepted", "promoted", "committed", "merged", "completed", "complete"}
    if canonical_status not in accepted_statuses:
        return False, affected_paths, f"unexpected canonical status: {canonical_status or 'missing'}"
    if landing_verification_status != "verified":
        detail = str(submission_event.get("landing_verification_detail") or "").strip()
        message = detail or f"unexpected landing verification status: {landing_verification_status or 'missing'}"
        return False, affected_paths, message
    if set(affected_paths) != {implementation_path}:
        return False, affected_paths, f"unexpected landed paths: {affected_paths}"
    return True, affected_paths, None


async def _publish_constrained_tasks(
    run_dir: Path,
    config: MXConfig,
    queue: Any,
    slices: list[SliceSpec],
    model_tier: ModelTier = ModelTier.LOCAL_OPEN_WEIGHT,
    routing_target: RoutingTarget = RoutingTarget.LOCAL,
    routing_reason: str | None = None,
) -> dict[str, SliceSpec]:
    task_map: dict[str, SliceSpec] = {}
    published_by_key: dict[str, TaskContract] = {}
    for slice_spec in slices:
        planner = Planner(
            config=config,
            queue=queue,
            repo_path=str(run_dir),
            prd_path=slice_spec.prd_path,
        )
        prd = planner._read_prd()
        requirement = planner._build_constrained_local_requirement(prd)
        task = planner._build_constrained_local_task(requirement, prd)
        task.model_tier = model_tier
        task.routing_target = routing_target
        task.routing_reason = routing_reason
        task.escalation_reason = routing_reason
        task.frontier_eligible = True
        task.estimated_cost_bucket = "high" if model_tier == ModelTier.FRONTIER else "low"
        task.shard_key = f"{task.stream.value}.{task.type.value}.{task.model_tier.value}"
        missing_prerequisites = [
            prerequisite_key
            for prerequisite_key in slice_spec.prerequisite_keys
            if prerequisite_key not in published_by_key
        ]
        if missing_prerequisites:
            missing = ", ".join(sorted(missing_prerequisites))
            raise ValueError(
                f"Slice '{slice_spec.key}' references unpublished prerequisite key(s): {missing}"
            )
        task.prerequisite_task_ids = [
            published_by_key[prerequisite_key].id
            for prerequisite_key in slice_spec.prerequisite_keys
        ]
        await planner.publish_tasks([task])
        task_map[task.id] = slice_spec
        published_by_key[slice_spec.key] = task
    return task_map


async def _run_workers_until_terminal(
    run_dir: Path,
    worker_count: int,
    timeout_seconds: int,
    slices: list[SliceSpec],
    task_model_tier: ModelTier = ModelTier.LOCAL_OPEN_WEIGHT,
    routing_target: RoutingTarget = RoutingTarget.LOCAL,
    routing_reason: str | None = None,
) -> tuple[dict[str, SliceSpec], datetime | None, int, int, float, int]:
    old_cwd = os.getcwd()
    os.chdir(run_dir)
    try:
        with _forced_local_execution_env():
            config = MXConfig.load("mx.config.json")
            await _prewarm_local_llm(config)
            queue = create_queue_adapter(config.queue.to_dict())
            await queue.connect()
            await queue.purge()
            Worker._global_inflight_task_ids.clear()

            task_map = await _publish_constrained_tasks(
                run_dir,
                config,
                queue,
                slices,
                model_tier=task_model_tier,
                routing_target=routing_target,
                routing_reason=routing_reason,
            )
            workers = [
                Worker(
                    config=config,
                    worker_id=f"worker-{index}",
                    queue=queue,
                )
                for index in range(worker_count)
            ]
            for worker in workers:
                await worker.initialize()
            worker_tasks = [asyncio.create_task(worker.run_loop()) for worker in workers]

            deadline = time.time() + timeout_seconds
            final_terminal_ts: datetime | None = None
            while time.time() < deadline:
                task_events = _read_jsonl(run_dir / ".runtime" / "task_state_events.jsonl")
                terminal_by_task = _latest_terminal_events(task_events)
                if len(terminal_by_task) == len(task_map) and all(task_id in terminal_by_task for task_id in task_map):
                    final_terminal_ts = max(
                        (
                            _parse_timestamp(str(event.get("timestamp", "")))
                            for event in terminal_by_task.values()
                        ),
                        default=None,
                    )
                    break
                await asyncio.sleep(2)

            _set_paused(run_dir, True)
            stop_started = datetime.now(UTC)
            done, pending = await asyncio.wait(worker_tasks, timeout=35)
            orphaned_workers = len(pending)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for worker in workers:
                await worker.shutdown()
            stop_completed_at = datetime.now(UTC)

            queue_depth_end = await queue.get_queue_depth()
            dead_letter_depth_end = await queue.get_dead_letter_depth() if hasattr(queue, "get_dead_letter_depth") else 0
            await queue.disconnect()

            if final_terminal_ts is None and done:
                task_events = _read_jsonl(run_dir / ".runtime" / "task_state_events.jsonl")
                terminal_by_task = _latest_terminal_events(task_events)
                final_terminal_ts = max(
                    (
                        _parse_timestamp(str(event.get("timestamp", "")))
                        for event in terminal_by_task.values()
                    ),
                    default=None,
                )

            if final_terminal_ts is None:
                stop_convergence_seconds = (stop_completed_at - stop_started).total_seconds()
            else:
                stop_convergence_seconds = (stop_completed_at - final_terminal_ts).total_seconds()
            return (
                task_map,
                final_terminal_ts,
                queue_depth_end,
                dead_letter_depth_end,
                stop_convergence_seconds,
                orphaned_workers,
            )
    finally:
        os.chdir(old_cwd)


def _evaluate_dashboard_truth(
    run_dir: Path,
    task_ids: set[str],
) -> bool:
    collector = MetricsCollector()
    collector.task_state_event_path = str(run_dir / ".runtime" / "task_state_events.jsonl")
    collector.submission_event_path = str(run_dir / ".runtime" / "submission_events.jsonl")
    terminal_by_task = _latest_terminal_events(_read_jsonl(run_dir / ".runtime" / "task_state_events.jsonl"))
    expected_success_tasks = {
        task_id
        for task_id, event in terminal_by_task.items()
        if str(event.get("state_to", "")).strip().lower() == "settled_ack"
    }
    if not expected_success_tasks:
        return True

    summary = collector.get_submission_summary()
    recent = collector.get_recent_submission_events(limit=max(20, len(task_ids) * 4), settled_only=True)

    seen_tasks = {
        str(event.get("task_id", "")).strip()
        for event in recent
        if str(event.get("status", "")).strip().lower() == "settled_ack"
    }
    return summary.get("recent_settlement_count", 0) >= len(expected_success_tasks) and expected_success_tasks.issubset(seen_tasks)


def _build_slice_results(
    run_dir: Path,
    task_map: dict[str, SliceSpec],
) -> list[SliceResult]:
    task_events = _read_jsonl(run_dir / ".runtime" / "task_state_events.jsonl")
    terminal_by_task = _latest_terminal_events(task_events)
    submission_by_task = _latest_submission_by_task(_read_jsonl(run_dir / ".runtime" / "submission_events.jsonl"))

    results: list[SliceResult] = []
    for task_id, slice_spec in task_map.items():
        terminal_event = terminal_by_task.get(task_id, {})
        submission_event = submission_by_task.get(task_id, {})
        blocked_events = [
            event
            for event in task_events
            if str(event.get("task_id", "")).strip() == task_id
            and str(event.get("state_to", "")).strip().lower() == TaskState.BLOCKED.value
        ]
        task_snapshot = load_task_snapshot(task_id, repo_path=str(run_dir))
        terminal_state = str(terminal_event.get("state_to") or "") or None
        terminal_reason = str(terminal_event.get("reason") or "") or None
        branch_name = str(submission_event.get("branch_name") or "") or None
        verification_passed = False
        changed_files: list[str] = []
        failure_reason: str | None = None

        if terminal_state != "settled_ack":
            failure_reason = f"terminal state was {terminal_state or 'missing'}"
        elif branch_name is None:
            failure_reason = "missing submission provenance for settled task"
        elif str(submission_event.get("provider") or "").strip().lower() == "aether":
            verification_passed, changed_files, verify_error = _verify_aether_submission(
                submission_event=submission_event,
                implementation_path=slice_spec.implementation_path,
            )
            if not verification_passed:
                failure_reason = verify_error or "aether submission verification failed"
        else:
            verification_passed, changed_files, verify_error = _verify_branch(
                run_dir,
                branch_name=branch_name,
                implementation_path=slice_spec.implementation_path,
                test_paths=slice_spec.test_paths,
            )
            if not verification_passed:
                failure_reason = verify_error or "independent verification failed"

        results.append(
            SliceResult(
                key=slice_spec.key,
                task_id=task_id,
                goal=str(submission_event.get("goal") or f"Implement {slice_spec.key}"),
                prerequisite_keys=list(slice_spec.prerequisite_keys),
                prerequisite_task_ids=list(getattr(task_snapshot, "prerequisite_task_ids", [])),
                terminal_state=terminal_state,
                terminal_reason=terminal_reason,
                branch_name=branch_name,
                submission_id=str(submission_event.get("submission_id") or "") or None,
                submission_url=str(submission_event.get("url") or "") or None,
                settled_at=str(submission_event.get("settled_at") or submission_event.get("created_at") or "") or None,
                verification_passed=verification_passed,
                changed_files=changed_files,
                blocked_event_count=len(blocked_events),
                last_blocked_reason=(
                    str(blocked_events[-1].get("reason") or "") or None
                    if blocked_events
                    else None
                ),
                failure_reason=failure_reason,
            )
        )
    return results


def _summarize_failure(
    result: RunResult,
) -> str | None:
    if result.false_success_rate != 0.0:
        return "false-success rate was non-zero"
    if result.duplicate_settlement_events != 0:
        return "duplicate settlement events detected"
    if result.duplicate_claim_events != 0:
        return "duplicate claim events detected"
    if result.orphaned_workers_after_stop != 0:
        return "orphaned workers remained after stop"
    if result.stop_convergence_seconds > 30:
        return "stop convergence exceeded 30 seconds"
    if result.queue_depth_end != 0:
        return "queue depth did not drain to zero"
    if result.lifecycle_bug_events:
        return "lifecycle bug events detected"
    if not result.dashboard_event_truthful:
        return "dashboard event stream did not reflect terminal states"
    for slice_result in result.slices:
        if not slice_result.verification_passed:
            return f"{slice_result.key} failed verification: {slice_result.failure_reason}"
    return None


async def _execute_single_run(
    run_number: int,
    worker_count: int,
    timeout_seconds: int,
    report_dir: Path,
    slices: list[SliceSpec],
    retry_max: int | None = None,
    frontier_llm_override: dict[str, Any] | None = None,
    task_model_tier: ModelTier = ModelTier.LOCAL_OPEN_WEIGHT,
    routing_target: RoutingTarget = RoutingTarget.LOCAL,
    routing_reason: str | None = None,
) -> RunResult:
    run_dir = Path(tempfile.mkdtemp(prefix=f"mx_gate2_run_{run_number}_"))
    _copy_working_tree(run_dir)
    _reset_run_state(run_dir)
    _prepare_run_config(
        run_dir,
        default_branch=_current_source_branch(),
        worker_count=worker_count,
        retry_max=retry_max,
        frontier_llm=frontier_llm_override,
    )

    required_assets = ["AGENTS.md"]
    for slice_spec in slices:
        required_assets.append(slice_spec.prd_path)
        required_assets.append(slice_spec.implementation_path)
        required_assets.extend(slice_spec.test_paths)
    _copy_required_assets(run_dir, required_assets)

    log_path = report_dir / f"run_{run_number}.log"
    with open(log_path, "w") as log_handle:
        with contextlib.redirect_stdout(log_handle), contextlib.redirect_stderr(log_handle):
            (
                task_map,
                _final_terminal_ts,
                queue_depth_end,
                dead_letter_depth_end,
                stop_convergence_seconds,
                orphaned_workers,
            ) = await _run_workers_until_terminal(
                run_dir=run_dir,
                worker_count=worker_count,
                timeout_seconds=timeout_seconds,
                slices=slices,
                task_model_tier=task_model_tier,
                routing_target=routing_target,
                routing_reason=routing_reason,
            )

    observability = _build_observability(run_dir)
    metrics = observability.get("metrics", {})
    task_events = _read_jsonl(run_dir / ".runtime" / "task_state_events.jsonl")
    lifecycle_events = _lifecycle_bug_events(task_events)
    slice_results = _build_slice_results(run_dir, task_map)
    dashboard_truthful = _evaluate_dashboard_truth(run_dir, set(task_map))

    result = RunResult(
        run_number=run_number,
        status="passed",
        worker_count=worker_count,
        false_success_rate=float(metrics.get("false_success_rate", 1.0)),
        duplicate_settlement_events=int(metrics.get("duplicate_settlement_events", 0)),
        duplicate_claim_events=_duplicate_claim_events(task_events),
        orphaned_workers_after_stop=orphaned_workers,
        stop_convergence_seconds=float(stop_convergence_seconds),
        queue_depth_end=queue_depth_end,
        dead_letter_depth_end=dead_letter_depth_end,
        dashboard_event_truthful=dashboard_truthful,
        lifecycle_bug_events=lifecycle_events,
        slices=slice_results,
        run_dir=str(run_dir),
        log_path=str(log_path),
    )
    result.failure_reason = _summarize_failure(result)
    result.status = "passed" if result.failure_reason is None else "failed"
    return result


def _write_report(report_dir: Path, results: list[RunResult], worker_count: int) -> None:
    json_path = report_dir / "gate2_local_parallel.json"
    md_path = report_dir / "gate2_local_parallel.md"

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "repo_root": str(REPO_ROOT),
        "worker_count": worker_count,
        "slice_keys": sorted({slice_result.key for result in results for slice_result in result.slices}),
        "results": [asdict(result) for result in results],
    }
    with open(json_path, "w") as handle:
        json.dump(payload, handle, indent=2)

    passed = [result for result in results if result.status == "passed"]
    failed = [result for result in results if result.status != "passed"]
    lines = [
        "# Gate 2 Local Parallel Report",
        "",
        f"- generated_at: `{payload['generated_at']}`",
        f"- worker_count: `{worker_count}`",
        f"- slices: `{', '.join(payload['slice_keys'])}`",
        f"- total_runs: `{len(results)}`",
        f"- passed_runs: `{len(passed)}`",
        f"- failed_runs: `{len(failed)}`",
        "",
    ]

    for result in results:
        lines.extend(
            [
                f"## Run {result.run_number}",
                "",
                f"- status: `{result.status}`",
                f"- false_success_rate: `{result.false_success_rate}`",
                f"- duplicate_settlement_events: `{result.duplicate_settlement_events}`",
                f"- duplicate_claim_events: `{result.duplicate_claim_events}`",
                f"- stop_convergence_seconds: `{result.stop_convergence_seconds}`",
                f"- queue_depth_end: `{result.queue_depth_end}`",
                f"- dead_letter_depth_end: `{result.dead_letter_depth_end}`",
                f"- dashboard_event_truthful: `{result.dashboard_event_truthful}`",
                f"- lifecycle_bug_events: `{len(result.lifecycle_bug_events)}`",
                f"- run_dir: `{result.run_dir}`",
                f"- log_path: `{result.log_path}`",
            ]
        )
        if result.failure_reason:
            lines.append(f"- failure_reason: `{result.failure_reason}`")
        lines.append("")
        lines.append("### Slices")
        lines.append("")
        for slice_result in result.slices:
            lines.extend(
                [
                    f"- `{slice_result.key}` task `{slice_result.task_id}` -> terminal `{slice_result.terminal_state}` branch `{slice_result.branch_name}` submission `{slice_result.submission_id}` verification `{slice_result.verification_passed}`",
                ]
            )
            if slice_result.failure_reason:
                lines.append(f"  failure: `{slice_result.failure_reason}`")
        lines.append("")

    with open(md_path, "w") as handle:
        handle.write("\n".join(lines))


async def execute_parallel_corpus(
    *,
    runs: int,
    worker_count: int,
    timeout_seconds: int,
    report_dir: Path,
    slices: list[SliceSpec],
    retry_max: int | None = None,
    frontier_llm_override: dict[str, Any] | None = None,
    task_model_tier: ModelTier = ModelTier.LOCAL_OPEN_WEIGHT,
    routing_target: RoutingTarget = RoutingTarget.LOCAL,
    routing_reason: str | None = None,
) -> list[RunResult]:
    results: list[RunResult] = []
    for run_number in range(1, runs + 1):
        result = await _execute_single_run(
            run_number=run_number,
            worker_count=worker_count,
            timeout_seconds=timeout_seconds,
            report_dir=report_dir,
            slices=slices,
            retry_max=retry_max,
            frontier_llm_override=frontier_llm_override,
            task_model_tier=task_model_tier,
            routing_target=routing_target,
            routing_reason=routing_reason,
        )
        results.append(result)
        _write_report(report_dir=report_dir, results=results, worker_count=worker_count)
        if result.status != "passed":
            break
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Gate 2 local parallel proofs in isolated workspaces.")
    parser.add_argument("--runs", type=int, default=3, help="Number of consecutive runs required.")
    parser.add_argument("--workers", type=int, default=5, help="Number of local workers to run.")
    parser.add_argument("--timeout-seconds", type=int, default=1200, help="Timeout per run.")
    parser.add_argument(
        "--corpus",
        default=None,
        help="Optional qualification corpus manifest to load slice definitions from.",
    )
    args = parser.parse_args()
    slices = load_slice_specs(args.corpus)

    report_dir = REPO_ROOT / "reports" / "gate2_local_parallel" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report_dir.mkdir(parents=True, exist_ok=True)

    results = asyncio.run(
        execute_parallel_corpus(
            runs=args.runs,
            worker_count=args.workers,
            timeout_seconds=args.timeout_seconds,
            report_dir=report_dir,
            slices=slices,
        )
    )

    print(f"Gate 2 report written to {report_dir}")
    failures = [result for result in results if result.status != "passed"]
    return 1 if failures or len(results) != args.runs else 0


if __name__ == "__main__":
    raise SystemExit(main())
