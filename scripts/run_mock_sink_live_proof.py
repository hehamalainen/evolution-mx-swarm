#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.run_aether_live_proof import (  # noqa: E402
    MX_VENV_PYTHON,
    REPORT_ROOT,
    _checkpoint,
    _classify_live_failure,
    _copy_if_exists,
    _find_latest_report,
    _latest_submission_by_task,
    _materialize_worktree,
    _model_response_latency_seconds,
    _read_json,
    _read_jsonl,
)


def _patch_config(config_path: Path) -> None:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    vcs = payload.setdefault("vcs", {})
    vcs["provider"] = "github"
    vcs["canonical_enforcement"] = False
    vcs["post_landing_verification_required"] = True
    vcs["settlement_retry_max"] = 1
    vcs["settlement_retry_backoff_seconds"] = 1
    vcs["reconciliation_enabled"] = False

    github = payload.setdefault("github", {})
    github["repo_owner"] = "local"
    github["repo_name"] = "swarm"
    github["default_branch"] = "main"
    github["token"] = None

    payload.setdefault("worker", {})["max_workers"] = 1
    config_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _load_mock_prs(*paths: Path) -> dict[str, Any]:
    for base in paths:
        path = base / "mock_prs.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    return {"prs": [], "next_id": 1}


def _is_mock_merge_diff_gap(delivery_payload: dict[str, Any] | None) -> bool:
    if not isinstance(delivery_payload, dict):
        return False
    proof = delivery_payload.get("proof") or {}
    if str(proof.get("failure_point") or "") != "artifact_verification":
        return False
    run = delivery_payload.get("run") or {}
    slices = run.get("slices") or []
    if not isinstance(slices, list) or not slices:
        return False
    failure_reason = str((slices[0] or {}).get("failure_reason") or "")
    return "unexpected branch diff: []" in failure_reason


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a narrow live MX -> local mock-sink proof.")
    parser.add_argument("--slice", default="jsonpeek", help="Qualification slice to run.")
    parser.add_argument("--corpus", default=None, help="Optional qualification corpus manifest.")
    parser.add_argument("--timeout-seconds", type=int, default=1200, help="Proof timeout.")
    parser.add_argument("--run-id", default=None, help="Explicit MX run identifier.")
    parser.add_argument("--report-root", default=None, help="Optional report root override.")
    parser.add_argument("--source-root", default=None, help="Optional source repo root to snapshot from.")
    parser.add_argument(
        "--source-is-template-repo",
        action="store_true",
        help="Treat --source-root as an already-prepared snapshot repo and clone it instead of rebuilding it.",
    )
    parser.add_argument("--canned-completion-file", default=None, help="Optional canned completion fixture JSON.")
    args = parser.parse_args()

    report_stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_id = args.run_id or f"mx-mock-sink-proof-{report_stamp.lower()}"
    report_root = Path(args.report_root) if args.report_root else (REPORT_ROOT.parent / "mock_sink_live_proof")
    source_root = Path(args.source_root).resolve() if args.source_root else REPO_ROOT
    report_dir = report_root / report_stamp
    report_dir.mkdir(parents=True, exist_ok=True)

    workspace = Path(tempfile.mkdtemp(prefix="mx_mock_sink_live_proof_"))
    shutil.rmtree(workspace)
    _materialize_worktree(source_root, workspace, source_is_template_repo=args.source_is_template_repo)
    _patch_config(workspace / "mx.config.json")

    env = os.environ.copy()
    env["MX_RUN_ID"] = run_id
    env["MX_FORCE_LOCAL_MODE"] = "1"
    if args.canned_completion_file:
        env["MX_CANNED_COMPLETION_FIXTURE"] = str(Path(args.canned_completion_file).resolve())

    proof_cmd = [
        str(MX_VENV_PYTHON),
        "scripts/run_delivery_proof.py",
        "--slice",
        args.slice,
        "--workers",
        "1",
        "--timeout-seconds",
        str(args.timeout_seconds),
        "--retry-max",
        "0",
    ]
    if args.corpus:
        proof_cmd.extend(["--corpus", args.corpus])

    proof_result = subprocess.run(
        proof_cmd,
        cwd=workspace,
        env=env,
        text=True,
        capture_output=True,
    )

    temp_report_dir = _find_latest_report(workspace / "reports" / "delivery_proof")
    copied_report_dir: Path | None = None
    delivery_payload: dict[str, Any] | None = None
    stage_checkpoints: dict[str, dict[str, Any]] = {
        "task_selected": {"reached": False, "timestamp": None, "status": None, "attributes": {}},
        "model_bypassed_canned": {"reached": False, "timestamp": None, "status": None, "attributes": {}},
        "model_started": {"reached": False, "timestamp": None, "status": None, "attributes": {}},
        "model_response_received": {"reached": False, "timestamp": None, "status": None, "attributes": {}},
        "model_timeout": {"reached": False, "timestamp": None, "status": None, "attributes": {}},
        "model_completed": {"reached": False, "timestamp": None, "status": None, "attributes": {}},
        "submit_started": {"reached": False, "timestamp": None, "status": None, "attributes": {}},
        "submit_completed": {"reached": False, "timestamp": None, "status": None, "attributes": {}},
        "proposal_id_received": {"reached": False, "timestamp": None, "status": None, "attributes": {}},
        "proposal_visible": {"reached": False, "timestamp": None, "status": None, "attributes": {}},
        "terminal_status_seen": {"reached": False, "timestamp": None, "status": None, "attributes": {}},
    }
    if temp_report_dir is not None:
        copied_report_dir = report_dir / "delivery_proof"
        shutil.copytree(temp_report_dir, copied_report_dir, dirs_exist_ok=True)
        delivery_json = temp_report_dir / "delivery_proof.json"
        if delivery_json.exists():
            delivery_payload = _read_json(delivery_json)
            run = delivery_payload.get("run") or {}
            run_dir_raw = str(run.get("run_dir") or "").strip()
            log_path_raw = str(run.get("log_path") or "").strip()
            run_dir = Path(run_dir_raw) if run_dir_raw else None
            log_path = Path(log_path_raw) if log_path_raw else None
            if log_path and log_path.exists():
                _copy_if_exists(log_path, report_dir / "mx_run.log")
            if run_dir:
                runtime_dir = run_dir / ".runtime"
                _copy_if_exists(runtime_dir / "task_state_events.jsonl", report_dir / "task_state_events.jsonl")
                _copy_if_exists(runtime_dir / "submission_events.jsonl", report_dir / "submission_events.jsonl")
                _copy_if_exists(runtime_dir / "trace_spans.jsonl", report_dir / "trace_spans.jsonl")
                _copy_if_exists(runtime_dir / "llm_usage_events.jsonl", report_dir / "llm_usage_events.jsonl")

                trace_spans = _read_jsonl(runtime_dir / "trace_spans.jsonl")
                submission_events = _read_jsonl(runtime_dir / "submission_events.jsonl")
                stage_checkpoints["task_selected"] = _checkpoint(trace_spans, operation="task_selected")
                stage_checkpoints["model_bypassed_canned"] = _checkpoint(trace_spans, operation="model_bypassed_canned")
                stage_checkpoints["model_started"] = _checkpoint(trace_spans, operation="llm_chat", status="started")
                stage_checkpoints["model_response_received"] = _checkpoint(
                    trace_spans, operation="model_response_received"
                )
                stage_checkpoints["model_timeout"] = _checkpoint(trace_spans, operation="model_timeout")
                stage_checkpoints["model_completed"] = _checkpoint(trace_spans, operation="llm_chat", status="ok")
                stage_checkpoints["submit_started"] = _checkpoint(trace_spans, operation="submit_change", status="started")
                submit_completed = _checkpoint(trace_spans, operation="submit_change", status="ok")
                if not submit_completed["reached"]:
                    submit_completed = _checkpoint(trace_spans, operation="submit_change", status="error")
                stage_checkpoints["submit_completed"] = submit_completed
                proposal_id_received = _checkpoint(trace_spans, operation="proposal_id_received")
                if not proposal_id_received["reached"]:
                    latest_by_task = _latest_submission_by_task(submission_events)
                    proof = delivery_payload.get("proof") or {}
                    proof_task_id = str(proof.get("task_id") or "").strip()
                    latest_submission = latest_by_task.get(proof_task_id, {}) if proof_task_id else {}
                    if latest_submission.get("submission_id"):
                        proposal_id_received = {
                            "reached": True,
                            "timestamp": latest_submission.get("created_at"),
                            "status": latest_submission.get("status"),
                            "attributes": {
                                "submission_id": latest_submission.get("submission_id"),
                            },
                        }
                stage_checkpoints["proposal_id_received"] = proposal_id_received

    proof_payload = (delivery_payload or {}).get("proof") or {}
    run_payload = (delivery_payload or {}).get("run") or {}
    run_dir_raw = str(run_payload.get("run_dir") or "").strip()
    run_dir = Path(run_dir_raw) if run_dir_raw else None
    mock_prs = _load_mock_prs(*(path for path in (run_dir, workspace) if path is not None))
    submission_id = str(proof_payload.get("submission_id") or "").strip()
    selected_pr = None
    if submission_id:
        for pr in mock_prs.get("prs", []):
            if str(pr.get("number")) == submission_id:
                selected_pr = pr
                break
    if selected_pr is None and mock_prs.get("prs"):
        selected_pr = mock_prs["prs"][-1]

    terminal_status = None
    if selected_pr is not None:
        stage_checkpoints["proposal_visible"] = {
            "reached": True,
            "timestamp": selected_pr.get("created_at"),
            "status": selected_pr.get("state"),
            "attributes": {"submission_id": selected_pr.get("number")},
        }
        if selected_pr.get("merged"):
            terminal_status = "merged"
        else:
            state = str(selected_pr.get("state") or "").strip()
            terminal_status = state or None
        if terminal_status:
            stage_checkpoints["terminal_status_seen"] = {
                "reached": True,
                "timestamp": selected_pr.get("merged_at") or selected_pr.get("created_at"),
                "status": terminal_status,
                "attributes": {"submission_id": selected_pr.get("number")},
            }

    failure_stage = _classify_live_failure(
        stage_checkpoints,
        proposal_visible=stage_checkpoints["proposal_visible"]["reached"],
        terminal_status=terminal_status,
    )
    phase = "worker_canned_proof" if args.canned_completion_file else "model_live_proof"
    chain = (delivery_payload or {}).get("chain") or {}
    post_landing_verify = "pass" if chain.get("post_landing_verified") else "fail"
    normalized_merge_gap = _is_mock_merge_diff_gap(delivery_payload)
    effective_success = (
        bool(selected_pr)
        and terminal_status in {"merged", "closed"}
        and post_landing_verify == "pass"
        and (proof_result.returncode == 0 or normalized_merge_gap)
    )
    run_scoped_confirmation = {
        "proposal_visible": bool(selected_pr),
        "proposal_id": (selected_pr or {}).get("number"),
        "proposal_status": terminal_status,
        "submission_url": proof_payload.get("submission_url"),
        "submission_source_run_id": proof_payload.get("source_run_id"),
        "submission_source_task_id": proof_payload.get("source_task_id"),
        "submission_source_agent_id": proof_payload.get("source_agent_id"),
        "submission_source_attempt_id": proof_payload.get("source_attempt_id"),
        "intent_type": proof_payload.get("intent_type"),
    }

    model_response_latency_seconds = _model_response_latency_seconds(stage_checkpoints)

    summary = {
        "generated_at": datetime.now(UTC).isoformat(),
        "phase": phase,
        "run_id": run_id,
        "sink_mode": "mock_github",
        "proof_exit_code": proof_result.returncode,
        "proof_stdout": proof_result.stdout,
        "proof_stderr": proof_result.stderr,
        "copied_report_dir": str(copied_report_dir) if copied_report_dir else None,
        "delivery_payload": delivery_payload,
        "stage_checkpoints": stage_checkpoints,
        "model_response_latency_seconds": model_response_latency_seconds,
        "failure_stage": failure_stage,
        "post_landing_verify": post_landing_verify,
        "normalized_merge_diff_gap": normalized_merge_gap,
        "effective_success": effective_success,
        "run_scoped_confirmation": run_scoped_confirmation,
        "mock_prs": mock_prs,
        "selected_pr": selected_pr,
    }
    (report_dir / "mock_sink_live_proof.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    lines = [
        "# Mock Sink Live Proof",
        "",
        f"- generated_at: `{summary['generated_at']}`",
        f"- phase: `{phase}`",
        f"- run_id: `{run_id}`",
        "- sink_mode: `mock_github`",
        f"- proof_exit_code: `{proof_result.returncode}`",
        f"- copied_report_dir: `{summary['copied_report_dir']}`",
        f"- model_response_latency_seconds: `{model_response_latency_seconds}`",
        f"- failure_stage: `{failure_stage}`",
        f"- post_landing_verify: `{post_landing_verify}`",
        f"- normalized_merge_diff_gap: `{normalized_merge_gap}`",
        f"- effective_success: `{effective_success}`",
        f"- selected_submission_id: `{(selected_pr or {}).get('number')}`",
        f"- selected_submission_state: `{(selected_pr or {}).get('state')}`",
        f"- selected_submission_merged: `{(selected_pr or {}).get('merged')}`",
        f"- run_scoped_confirmation: `{run_scoped_confirmation}`",
        "",
        "## Stage Checkpoints",
        "",
        *[
            (
                f"- {stage}: reached=`{payload['reached']}` "
                f"timestamp=`{payload['timestamp']}` "
                f"status=`{payload['status']}`"
            )
            for stage, payload in stage_checkpoints.items()
        ],
    ]
    (report_dir / "mock_sink_live_proof.md").write_text("\n".join(lines), encoding="utf-8")

    if not effective_success and proof_result.returncode != 0:
        return proof_result.returncode
    if not selected_pr:
        return 2
    if terminal_status not in {"merged", "closed"}:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
