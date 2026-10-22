#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(REPO_ROOT))


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
DEFAULT_PRD = "prds/golden_path_002_jsonpeek.md"
DEFAULT_TESTS = ["tests/test_jsonpeek.py"]
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


@dataclass
class RunResult:
    run_number: int
    status: str
    branch_name: str | None
    submission_id: str | None
    submission_url: str | None
    settled_at: str | None
    false_success_rate: float
    duplicate_settlement_events: int
    terminal_settlement_events: int
    verification_passed: bool
    changed_files: list[str]
    run_dir: str
    log_path: str
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


def _prepare_run_config(run_dir: Path, default_branch: str) -> None:
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

    payload.setdefault("paused", True)

    with open(config_path, "w") as handle:
        json.dump(payload, handle, indent=2)


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


def _latest_settled_ack(submission_events: list[dict[str, Any]]) -> dict[str, Any] | None:
    settled = [
        event
        for event in submission_events
        if str(event.get("status", "")).strip().lower() == "settled_ack"
    ]
    if not settled:
        return None
    settled.sort(key=lambda item: str(item.get("settled_at") or item.get("created_at") or ""))
    return settled[-1]


def _build_observability(run_dir: Path) -> dict[str, Any]:
    from shared.utils.metrics import MetricsCollector

    collector = MetricsCollector()
    collector.task_state_event_path = str(run_dir / ".runtime" / "task_state_events.jsonl")
    collector.submission_event_path = str(run_dir / ".runtime" / "submission_events.jsonl")
    collector.trace_span_path = str(run_dir / ".runtime" / "trace_spans.jsonl")
    collector.reconciliation_path = str(run_dir / ".runtime" / "aether_reconciliation_backlog.json")
    return collector.compute_event_observability(window_minutes=24 * 60)


def _stop_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=30)
        return
    except subprocess.TimeoutExpired:
        pass
    proc.terminate()
    try:
        proc.wait(timeout=10)
        return
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def _verify_branch(
    run_dir: Path,
    branch_name: str,
    test_paths: list[str],
) -> tuple[bool, list[str], str | None]:
    try:
        _run(["git", "rev-parse", "--verify", branch_name], cwd=run_dir)
    except subprocess.CalledProcessError as exc:
        return False, [], exc.stderr.strip() or exc.stdout.strip() or "branch not found"

    diff = _run(["git", "diff", "--name-only", "HEAD", branch_name], cwd=run_dir)
    changed_files = [line.strip() for line in diff.stdout.splitlines() if line.strip()]

    verify_dir = Path(tempfile.mkdtemp(prefix="mx_gate1_verify_"))
    try:
        _run(["git", "worktree", "add", "--detach", str(verify_dir), branch_name], cwd=run_dir)

        for rel_path in test_paths:
            source = run_dir / rel_path
            if source.exists():
                destination = verify_dir / rel_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)

        pytest_cmd = [str(VENV_PYTHON), "-m", "pytest", *test_paths, "-q"]
        ruff_cmd = [str(VENV_PYTHON), "-m", "ruff", "check", "tools/jsonpeek.py", *test_paths]

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

    return True, changed_files, None


def _execute_single_run(
    run_number: int,
    prd_path: str,
    dashboard_url: str,
    timeout_seconds: int,
    report_dir: Path,
    test_paths: list[str],
) -> RunResult:
    run_dir = Path(tempfile.mkdtemp(prefix=f"mx_gate1_run_{run_number}_"))
    _copy_working_tree(run_dir)
    _reset_run_state(run_dir)
    _prepare_run_config(run_dir, default_branch=_current_source_branch())
    _copy_required_assets(
        run_dir,
        [
            prd_path,
            *test_paths,
            "tools/jsonpeek.py",
            "AGENTS.md",
        ],
    )

    log_path = report_dir / f"run_{run_number}.log"
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env.pop("GITHUB_TOKEN", None)

    cmd = [
        str(VENV_PYTHON),
        "-m",
        "cli.main",
        "run",
        "--workers",
        "1",
        "--dashboard-url",
        dashboard_url,
        "--prd-path",
        prd_path,
    ]

    with open(log_path, "w") as log_handle:
        proc = subprocess.Popen(
            cmd,
            cwd=run_dir,
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )

        success_event: dict[str, Any] | None = None
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            submission_events = _read_jsonl(run_dir / ".runtime" / "submission_events.jsonl")
            success_event = _latest_settled_ack(submission_events)
            if success_event is not None:
                break
            if proc.poll() is not None:
                break
            time.sleep(2)

        _stop_process(proc)

    observability = _build_observability(run_dir)
    metrics = observability.get("metrics", {})

    if success_event is None:
        failure_reason = "no settled_ack observed before timeout"
        return RunResult(
            run_number=run_number,
            status="failed",
            branch_name=None,
            submission_id=None,
            submission_url=None,
            settled_at=None,
            false_success_rate=float(metrics.get("false_success_rate", 1.0)),
            duplicate_settlement_events=int(metrics.get("duplicate_settlement_events", 0)),
            terminal_settlement_events=int(metrics.get("terminal_settlement_events", 0)),
            verification_passed=False,
            changed_files=[],
            run_dir=str(run_dir),
            log_path=str(log_path),
            failure_reason=failure_reason,
        )

    branch_name = str(success_event.get("branch_name") or "")
    verification_passed, changed_files, verify_error = _verify_branch(
        run_dir=run_dir,
        branch_name=branch_name,
        test_paths=test_paths,
    )

    failure_reason = None
    if float(metrics.get("false_success_rate", 1.0)) != 0.0:
        failure_reason = "false-success rate was non-zero"
    elif int(metrics.get("duplicate_settlement_events", 0)) != 0:
        failure_reason = "duplicate settlement events detected"
    elif not verification_passed:
        failure_reason = verify_error or "independent verification failed"

    status = "passed" if failure_reason is None else "failed"
    return RunResult(
        run_number=run_number,
        status=status,
        branch_name=branch_name or None,
        submission_id=str(success_event.get("submission_id") or "") or None,
        submission_url=str(success_event.get("url") or "") or None,
        settled_at=str(success_event.get("settled_at") or success_event.get("created_at") or "") or None,
        false_success_rate=float(metrics.get("false_success_rate", 0.0)),
        duplicate_settlement_events=int(metrics.get("duplicate_settlement_events", 0)),
        terminal_settlement_events=int(metrics.get("terminal_settlement_events", 0)),
        verification_passed=verification_passed,
        changed_files=changed_files,
        run_dir=str(run_dir),
        log_path=str(log_path),
        failure_reason=failure_reason,
    )


def _write_report(report_dir: Path, prd_path: str, results: list[RunResult]) -> None:
    json_path = report_dir / "gate1_repeatability.json"
    md_path = report_dir / "gate1_repeatability.md"

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "prd_path": prd_path,
        "repo_root": str(REPO_ROOT),
        "results": [asdict(result) for result in results],
    }
    with open(json_path, "w") as handle:
        json.dump(payload, handle, indent=2)

    passed = [result for result in results if result.status == "passed"]
    failed = [result for result in results if result.status != "passed"]
    lines = [
        "# Gate 1 Repeatability Report",
        "",
        f"- generated_at: `{payload['generated_at']}`",
        f"- prd_path: `{prd_path}`",
        f"- total_runs: `{len(results)}`",
        f"- passed_runs: `{len(passed)}`",
        f"- failed_runs: `{len(failed)}`",
        "",
        "## Runs",
        "",
    ]
    for result in results:
        lines.extend(
            [
                f"### Run {result.run_number}",
                "",
                f"- status: `{result.status}`",
                f"- branch: `{result.branch_name}`",
                f"- submission_id: `{result.submission_id}`",
                f"- settled_at: `{result.settled_at}`",
                f"- false_success_rate: `{result.false_success_rate}`",
                f"- duplicate_settlement_events: `{result.duplicate_settlement_events}`",
                f"- verification_passed: `{result.verification_passed}`",
                f"- changed_files: `{', '.join(result.changed_files) if result.changed_files else '-'}`",
                f"- run_dir: `{result.run_dir}`",
                f"- log_path: `{result.log_path}`",
            ]
        )
        if result.failure_reason:
            lines.append(f"- failure_reason: `{result.failure_reason}`")
        lines.append("")

    with open(md_path, "w") as handle:
        handle.write("\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Gate 1 repeatability proofs in isolated workspaces.")
    parser.add_argument("--runs", type=int, default=5, help="Number of consecutive runs required.")
    parser.add_argument("--prd-path", default=DEFAULT_PRD, help="Constrained PRD path relative to repo root.")
    parser.add_argument(
        "--dashboard-url",
        default="http://127.0.0.1:8081",
        help="Dashboard URL for heartbeat and settlement visibility.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=900, help="Timeout per run.")
    args = parser.parse_args()

    report_dir = REPO_ROOT / "reports" / "gate1_repeatability" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report_dir.mkdir(parents=True, exist_ok=True)

    results: list[RunResult] = []
    for run_number in range(1, args.runs + 1):
        result = _execute_single_run(
            run_number=run_number,
            prd_path=args.prd_path,
            dashboard_url=args.dashboard_url,
            timeout_seconds=args.timeout_seconds,
            report_dir=report_dir,
            test_paths=DEFAULT_TESTS,
        )
        results.append(result)
        _write_report(report_dir=report_dir, prd_path=args.prd_path, results=results)
        if result.status != "passed":
            break

    print(f"Gate 1 report written to {report_dir}")
    failures = [result for result in results if result.status != 'passed']
    return 1 if failures or len(results) != args.runs else 0


if __name__ == "__main__":
    raise SystemExit(main())
