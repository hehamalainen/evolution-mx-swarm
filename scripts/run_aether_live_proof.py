#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shared.utils.llm import Message, create_llm_client  # noqa: E402

MX_VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
REPORT_ROOT = REPO_ROOT / "reports" / "aether_live_proof"
COPY_EXCLUDES = {
    ".git",
    ".venv",
    "venv",
    ".runtime",
    "reports",
    "dashboard_v2",
    "dashboard_v3",
    "data",
    "temp_analysis",
    "vault",
    "node_modules",
    "dist",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".coverage",
}
COPY_INCLUDE_ROOTS = {
    "boss",
    "cli",
    "judge",
    "planner",
    "prds",
    "proof_fixtures",
    "qualification",
    "scripts",
    "shared",
    "simulation",
    "tests",
    "tools",
    "worker",
}
COPY_INCLUDE_FILES = {
    "mx.config.json",
    "pyproject.toml",
    "requirements.txt",
}


def _install_venv_shim(target: Path) -> None:
    venv_bin = target / ".venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    python_shim = venv_bin / "python"
    python_shim.write_text(
        "#!/bin/sh\n"
        f"exec '{MX_VENV_PYTHON}' \"$@\"\n",
        encoding="utf-8",
    )
    python_shim.chmod(0o755)


def _http_json(method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    import urllib.parse

    if urllib.parse.urlparse(url).scheme not in {"http", "https"}:
        raise ValueError(f"Unsupported URL scheme in {url!r}")
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310
        raw = response.read().decode("utf-8")
    return json.loads(raw)


def _safe_http_json(method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        return _http_json(method, url, payload)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {"error": f"HTTP {exc.code}", "body": body}
    except Exception as exc:  # pragma: no cover - live harness only
        return {"error": str(exc)}


def _copy_worktree(source: Path, target: Path) -> None:
    def ignore(_src: str, names: list[str]) -> set[str]:
        src_path = Path(_src)
        if src_path == source:
            ignored = {
                name
                for name in names
                if name not in COPY_INCLUDE_ROOTS and name not in COPY_INCLUDE_FILES
            }
        else:
            ignored = set()
        ignored.update({name for name in names if name in COPY_EXCLUDES})
        ignored.update({name for name in names if name.endswith(".pyc")})
        return ignored

    shutil.copytree(source, target, ignore=ignore)
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=str(target),
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "MX Live Proof"],
        cwd=str(target),
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "mx-live-proof@example.invalid"],
        cwd=str(target),
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=str(target),
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "add", "-A"],
        cwd=str(target),
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "MX live proof snapshot"],
        cwd=str(target),
        check=True,
        capture_output=True,
        text=True,
    )
    _install_venv_shim(target)


def _prepare_cloned_snapshot_repo(source: Path, target: Path) -> None:
    subprocess.run(
        ["git", "clone", "--quiet", str(source), str(target)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "MX Live Proof"],
        cwd=str(target),
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "mx-live-proof@example.invalid"],
        cwd=str(target),
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=str(target),
        check=True,
        capture_output=True,
        text=True,
    )
    _install_venv_shim(target)


def _materialize_worktree(source: Path, target: Path, *, source_is_template_repo: bool = False) -> None:
    if source_is_template_repo:
        _prepare_cloned_snapshot_repo(source, target)
        return
    _copy_worktree(source, target)


def _patch_config(
    config_path: Path,
    *,
    base_url: str,
    repo_id: str,
    llm_provider: str | None = None,
    llm_model: str | None = None,
    llm_base_url: str | None = None,
    llm_api_key_env: str | None = None,
) -> None:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    vcs = payload.setdefault("vcs", {})
    vcs["provider"] = "aether"
    aether = vcs.setdefault("aether", {})
    aether["base_url"] = base_url
    aether["repo_id"] = repo_id
    aether.setdefault("agent_id", "mx-swarm")
    vcs["canonical_enforcement"] = True
    vcs["post_landing_verification_required"] = True
    vcs["settlement_retry_max"] = 10
    vcs["settlement_retry_backoff_seconds"] = 2
    payload.setdefault("worker", {})["max_workers"] = 1
    llm = payload.setdefault("llm", {})
    if llm_provider:
        llm["provider"] = llm_provider
    if llm_model:
        llm["model"] = llm_model
    if llm_base_url is not None:
        llm["base_url"] = llm_base_url
    if llm_api_key_env:
        llm["api_key"] = os.environ.get(llm_api_key_env)
    config_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


async def _prewarm_ollama(llm_payload: dict[str, Any]) -> dict[str, Any]:
    client = create_llm_client(llm_payload)
    started = datetime.now(UTC)
    await asyncio.wait_for(
        client.chat(
            messages=[Message(role="user", content="Reply with ok.")],
            system="Return only ok.",
            max_tokens=8,
            temperature=0.0,
        ),
        timeout=120,
    )
    finished = datetime.now(UTC)
    return {
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "elapsed_seconds": round((finished - started).total_seconds(), 3),
        "provider": llm_payload.get("provider"),
        "model": llm_payload.get("model"),
    }


def _find_latest_report(report_root: Path) -> Path | None:
    if not report_root.exists():
        return None
    candidates = [path for path in report_root.iterdir() if path.is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.name)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _copy_if_exists(source: Path, destination: Path) -> None:
    if source.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _latest_submission_by_task(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for event in events:
        task_id = str(event.get("task_id") or "").strip()
        if not task_id:
            continue
        timestamp = str(event.get("settled_at") or event.get("created_at") or "")
        current = latest.get(task_id)
        current_ts = str(current.get("settled_at") or current.get("created_at") or "") if current else ""
        if current is None or timestamp >= current_ts:
            latest[task_id] = event
    return latest


def _checkpoint(trace_spans: list[dict[str, Any]], *, operation: str, status: str | None = None) -> dict[str, Any]:
    for span in trace_spans:
        if str(span.get("operation") or "") != operation:
            continue
        if status is not None and str(span.get("status") or "") != status:
            continue
        return {
            "reached": True,
            "timestamp": span.get("timestamp"),
            "status": span.get("status"),
            "attributes": span.get("attributes") or {},
        }
    return {
        "reached": False,
        "timestamp": None,
        "status": None,
        "attributes": {},
    }


def _landed_paths_source(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return "missing"

    explicit_keys = ("affected_paths", "paths", "changed_paths", "landed_paths")
    for key in explicit_keys:
        value = payload.get(key)
        if isinstance(value, list) and value:
            return "explicit"

    proposal = payload.get("proposal")
    if isinstance(proposal, dict):
        for key in explicit_keys:
            value = proposal.get(key)
            if isinstance(value, list) and value:
                return "explicit"

    diff_payload = str(
        payload.get("change_content")
        or payload.get("landed_change_content")
        or (proposal.get("change_content") if isinstance(proposal, dict) else "")
        or (proposal.get("landed_change_content") if isinstance(proposal, dict) else "")
        or ""
    ).strip()
    if diff_payload:
        return "derived_from_diff"

    return "missing"


def _classify_live_failure(checkpoints: dict[str, dict[str, Any]], *, proposal_visible: bool, terminal_status: str | None) -> str | None:
    if not checkpoints["task_selected"]["reached"]:
        return "task_selection_failed"
    if not checkpoints["model_started"]["reached"] and not checkpoints["model_bypassed_canned"]["reached"]:
        return "model_not_invoked"
    if checkpoints["model_timeout"]["reached"]:
        return "model_execution_failed"
    if not checkpoints["model_bypassed_canned"]["reached"] and not checkpoints["model_completed"]["reached"]:
        return "model_execution_failed"
    if not checkpoints["submit_started"]["reached"]:
        return "pre_submit_execution_failed"
    if not checkpoints["submit_completed"]["reached"]:
        return "submit_transport_failed"
    if not checkpoints["proposal_id_received"]["reached"]:
        return "submit_response_missing_proposal_id"
    if not proposal_visible:
        return "proposal_not_visible_after_submit"
    if terminal_status is None:
        return "settlement_observation_gap"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a narrow live MX -> Aether proof.")
    parser.add_argument("--slice", default="jsonpeek", help="Qualification slice to run.")
    parser.add_argument("--corpus", default=None, help="Optional qualification corpus manifest.")
    parser.add_argument("--timeout-seconds", type=int, default=1200, help="Proof timeout.")
    parser.add_argument("--aether-base-url", default="http://127.0.0.1:8100", help="Aether base URL.")
    parser.add_argument("--repo-name", default=None, help="Optional Aether repository name.")
    parser.add_argument("--run-id", default=None, help="Explicit MX run identifier.")
    parser.add_argument("--report-root", default=None, help="Optional report root override.")
    parser.add_argument("--source-root", default=None, help="Optional source repo root to snapshot from.")
    parser.add_argument(
        "--source-is-template-repo",
        action="store_true",
        help="Treat --source-root as an already-prepared snapshot repo and clone it instead of rebuilding it.",
    )
    parser.add_argument("--llm-provider", default=None, help="Override llm.provider for the proof run.")
    parser.add_argument("--llm-model", default=None, help="Override llm.model for the proof run.")
    parser.add_argument("--llm-base-url", default=None, help="Override llm.base_url for the proof run.")
    parser.add_argument("--llm-api-key-env", default=None, help="Env var name to source llm.api_key from.")
    parser.add_argument("--canned-completion-file", default=None, help="Optional canned completion fixture JSON.")
    args = parser.parse_args()

    report_stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_id = args.run_id or f"mx-aether-proof-{report_stamp.lower()}"
    repo_name = args.repo_name or f"mx-proof-{report_stamp.lower()}"
    report_root = Path(args.report_root) if args.report_root else REPORT_ROOT
    source_root = Path(args.source_root).resolve() if args.source_root else REPO_ROOT
    report_dir = report_root / report_stamp
    report_dir.mkdir(parents=True, exist_ok=True)

    health = _safe_http_json("GET", f"{args.aether_base_url}/v1/health")
    if health.get("status") != "healthy":
        (report_dir / "failure.json").write_text(json.dumps({"stage": "health", "response": health}, indent=2), encoding="utf-8")
        return 1

    repo_response = _safe_http_json(
        "POST",
        f"{args.aether_base_url}/v1/repos",
        {"name": repo_name, "description": f"MX live proof {run_id}"},
    )
    repo_id = str(repo_response.get("id") or "")
    if not repo_id:
        (report_dir / "failure.json").write_text(json.dumps({"stage": "create_repo", "response": repo_response}, indent=2), encoding="utf-8")
        return 1

    workspace = Path(tempfile.mkdtemp(prefix="mx_aether_live_proof_"))
    shutil.rmtree(workspace)
    _materialize_worktree(source_root, workspace, source_is_template_repo=args.source_is_template_repo)
    _patch_config(
        workspace / "mx.config.json",
        base_url=args.aether_base_url,
        repo_id=repo_id,
        llm_provider=args.llm_provider,
        llm_model=args.llm_model,
        llm_base_url=args.llm_base_url,
        llm_api_key_env=args.llm_api_key_env,
    )
    config_payload = _read_json(workspace / "mx.config.json")
    llm_payload = dict(config_payload.get("llm") or {})

    env = os.environ.copy()
    env["MX_RUN_ID"] = run_id
    prewarm_result: dict[str, Any] | None = None
    if args.canned_completion_file:
        env["MX_CANNED_COMPLETION_FIXTURE"] = str(Path(args.canned_completion_file).resolve())
    elif str(llm_payload.get("provider") or "").lower() == "ollama":
        env["MX_LOCAL_MODEL_PROOF_MODE"] = "1"
        try:
            prewarm_result = asyncio.run(_prewarm_ollama(llm_payload))
        except Exception as exc:
            prewarm_result = {
                "error": str(exc),
                "provider": llm_payload.get("provider"),
                "model": llm_payload.get("model"),
            }

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
        "first_token_received": {"reached": False, "timestamp": None, "status": None, "attributes": {}},
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
                stage_checkpoints["first_token_received"] = _checkpoint(trace_spans, operation="first_token_received")
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

    proposals = _safe_http_json("GET", f"{args.aether_base_url}/v1/repos/{repo_id}/proposals")
    physics = _safe_http_json("GET", f"{args.aether_base_url}/v1/system/physics?window_seconds=600")

    selected_proposal: dict[str, Any] | None = None
    if isinstance(proposals.get("proposals"), list):
        for proposal in proposals["proposals"]:
            if str(proposal.get("source_run_id") or "") == run_id:
                selected_proposal = proposal
                break

    proposal_detail = None
    if selected_proposal and selected_proposal.get("id"):
        proposal_detail = _safe_http_json(
            "GET",
            f"{args.aether_base_url}/v1/repos/{repo_id}/proposals/{selected_proposal['id']}",
        )
        stage_checkpoints["proposal_visible"] = {
            "reached": True,
            "timestamp": selected_proposal.get("submitted_at"),
            "status": selected_proposal.get("status"),
            "attributes": {
                "proposal_id": selected_proposal.get("id"),
            },
        }
        terminal_status = str(proposal_detail.get("status") or "").strip() or None
        if terminal_status:
            stage_checkpoints["terminal_status_seen"] = {
                "reached": True,
                "timestamp": proposal_detail.get("promoted_at") or proposal_detail.get("submitted_at"),
                "status": terminal_status,
                "attributes": {
                    "proposal_id": selected_proposal.get("id"),
                },
            }
    else:
        terminal_status = None

    failure_stage = _classify_live_failure(
        stage_checkpoints,
        proposal_visible=stage_checkpoints["proposal_visible"]["reached"],
        terminal_status=terminal_status,
    )
    phase = "worker_canned_proof" if args.canned_completion_file else "model_live_proof"
    post_landing_verify = "unknown"
    if delivery_payload is not None:
        proof_payload = delivery_payload.get("proof") or {}
        verify_status = str(proof_payload.get("landing_verification_status") or "").strip().lower()
        if verify_status == "verified":
            post_landing_verify = "pass"
        elif verify_status in {"failed", "unsupported"}:
            post_landing_verify = "fail"
    landed_paths_source = _landed_paths_source(proposal_detail or selected_proposal)
    run_scoped_confirmation = {
        "proposal_visible": bool(selected_proposal),
        "proposal_id": (selected_proposal or {}).get("id"),
        "proposal_status": (proposal_detail or selected_proposal or {}).get("status"),
        "proposal_source_run_id": (selected_proposal or {}).get("source_run_id"),
        "proposal_source_task_id": (selected_proposal or {}).get("source_task_id"),
        "proposal_source_agent_id": (selected_proposal or {}).get("source_agent_id"),
        "terminal_status": terminal_status,
    }
    physics_window_seconds = physics.get("window_seconds")
    physics_windowed_mx_swarm = ((physics.get("by_source") or {}).get("mx_swarm"))

    summary = {
        "generated_at": datetime.now(UTC).isoformat(),
        "phase": phase,
        "run_id": run_id,
        "repo_name": repo_name,
        "repo_id": repo_id,
        "aether_base_url": args.aether_base_url,
        "llm_provider": llm_payload.get("provider"),
        "llm_model": llm_payload.get("model"),
        "local_model_proof_mode": env.get("MX_LOCAL_MODEL_PROOF_MODE") == "1",
        "prewarm_result": prewarm_result,
        "proof_exit_code": proof_result.returncode,
        "proof_stdout": proof_result.stdout,
        "proof_stderr": proof_result.stderr,
        "copied_report_dir": str(copied_report_dir) if copied_report_dir else None,
        "delivery_payload": delivery_payload,
        "stage_checkpoints": stage_checkpoints,
        "failure_stage": failure_stage,
        "post_landing_verify": post_landing_verify,
        "landed_paths_source": landed_paths_source,
        "run_scoped_confirmation": run_scoped_confirmation,
        "physics_window_seconds": physics_window_seconds,
        "physics_windowed_mx_swarm": physics_windowed_mx_swarm,
        "health": health,
        "repo_response": repo_response,
        "physics": physics,
        "proposals": proposals,
        "selected_proposal": selected_proposal,
        "proposal_detail": proposal_detail,
    }
    (report_dir / "aether_live_proof.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    lines = [
        "# Aether Live Proof",
        "",
        f"- generated_at: `{summary['generated_at']}`",
        f"- phase: `{phase}`",
        f"- run_id: `{run_id}`",
        f"- repo_id: `{repo_id}`",
        f"- proof_exit_code: `{proof_result.returncode}`",
        f"- copied_report_dir: `{summary['copied_report_dir']}`",
        f"- failure_stage: `{failure_stage}`",
        f"- post_landing_verify: `{post_landing_verify}`",
        f"- landed_paths_source: `{landed_paths_source}`",
        f"- selected_proposal_id: `{(selected_proposal or {}).get('id')}`",
        f"- selected_proposal_status: `{(selected_proposal or {}).get('status')}`",
        f"- selected_proposal_source: `{(selected_proposal or {}).get('source')}`",
        f"- selected_proposal_source_run_id: `{(selected_proposal or {}).get('source_run_id')}`",
        f"- selected_proposal_source_task_id: `{(selected_proposal or {}).get('source_task_id')}`",
        f"- selected_proposal_source_agent_id: `{(selected_proposal or {}).get('source_agent_id')}`",
        f"- selected_proposal_source_attempt_id: `{(selected_proposal or {}).get('source_attempt_id')}`",
        f"- selected_proposal_intent_type: `{(selected_proposal or {}).get('intent_type')}`",
        f"- run_scoped_confirmation: `{run_scoped_confirmation}`",
        f"- physics_window_seconds: `{physics_window_seconds}`",
        f"- physics_windowed_mx_swarm: `{physics_windowed_mx_swarm}`",
    ]
    lines.extend(
        [
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
    )
    (report_dir / "aether_live_proof.md").write_text("\n".join(lines), encoding="utf-8")

    if proof_result.returncode != 0:
        return proof_result.returncode
    if not selected_proposal:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
