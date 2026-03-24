#!/usr/bin/env python3
# ruff: noqa: E402
from __future__ import annotations

import argparse
import asyncio
import json
import os
import traceback
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(REPO_ROOT))

from scripts.run_gate2_local_parallel import (  # noqa: E402
    _latest_submission_by_task,
    _latest_terminal_events,
    _read_jsonl,
    execute_parallel_corpus,
    load_slice_specs,
)
from shared.utils import MXConfig  # noqa: E402
from shared.utils.metrics import get_budget_snapshot  # noqa: E402


def _resolve_slices(slice_key: str, corpus: str | None) -> list[Any]:
    slices = load_slice_specs(corpus)
    if slice_key == "all":
        return slices
    requested_keys = [item.strip() for item in slice_key.split(",") if item.strip()]
    selected = [slice_spec for slice_spec in slices if slice_spec.key in requested_keys]
    if len(selected) != len(requested_keys):
        available = ", ".join(sorted(slice.key for slice in slices))
        raise ValueError(f"Unknown slice selection '{slice_key}'. Available: {available}, all")
    return selected


def _build_proof_payload(*, result: Any, config: MXConfig) -> dict[str, Any]:
    slice_result = result.slices[0] if len(result.slices) == 1 else None
    run_dir = Path(result.run_dir)
    submission_events = _latest_submission_by_task(
        _read_jsonl(run_dir / ".runtime" / "submission_events.jsonl")
    )
    task_events = _latest_terminal_events(_read_jsonl(run_dir / ".runtime" / "task_state_events.jsonl"))
    all_task_events = _read_jsonl(run_dir / ".runtime" / "task_state_events.jsonl")

    per_slice_dependency = []
    blocked_event_count = 0
    for item in result.slices:
        task_id = str(item.task_id)
        item_events = [
            event for event in all_task_events if str(event.get("task_id", "")).strip() == task_id
        ]
        blocked_events = [
            event
            for event in item_events
            if str(event.get("state_to", "")).strip().lower() == "blocked"
        ]
        blocked_event_count += len(blocked_events)
        per_slice_dependency.append(
            {
                "key": item.key,
                "task_id": task_id,
                "prerequisite_keys": list(getattr(item, "prerequisite_keys", [])),
                "prerequisite_task_ids": list(getattr(item, "prerequisite_task_ids", [])),
                "source": submission_events.get(task_id, {}).get("source"),
                "source_run_id": submission_events.get(task_id, {}).get("source_run_id"),
                "source_task_id": submission_events.get(task_id, {}).get("source_task_id"),
                "source_agent_id": submission_events.get(task_id, {}).get("source_agent_id"),
                "source_attempt_id": submission_events.get(task_id, {}).get("source_attempt_id"),
                "intent_type": submission_events.get(task_id, {}).get("intent_type"),
                "blocked_event_count": len(blocked_events),
                "last_blocked_reason": getattr(item, "last_blocked_reason", None),
                "terminal_state": item.terminal_state,
                "verification_passed": bool(item.verification_passed),
            }
        )

    provider = config.vcs.provider
    if result.slices:
        first_event = submission_events.get(result.slices[0].task_id, {})
        provider = str(first_event.get("provider") or provider)

    chain = {
        "intent_compiled": bool(result.slices),
        "artifact_verified_locally": bool(result.slices) and all(
            bool(item.verification_passed) for item in result.slices
        ),
        "submission_recorded": bool(result.slices) and all(
            bool(submission_events.get(item.task_id, {})) for item in result.slices
        ),
        "canonical_accepted": bool(result.slices) and all(
            (
                str(submission_events.get(item.task_id, {}).get("canonical_status") or "") == "accepted"
                or str(submission_events.get(item.task_id, {}).get("status") or task_events.get(item.task_id, {}).get("state_to") or "")
                == "settled_ack"
            )
            for item in result.slices
        ),
        "post_landing_verified": True,
    }
    if provider == "aether":
        chain["post_landing_verified"] = bool(result.slices) and all(
            str(submission_events.get(item.task_id, {}).get("landing_verification_status") or "") == "verified"
            for item in result.slices
        )
    success = all(chain.values()) and result.status == "passed"

    failure_point = None
    if not chain["intent_compiled"]:
        failure_point = "intent_compilation"
    elif not chain["artifact_verified_locally"]:
        failure_point = "artifact_verification"
    elif not chain["submission_recorded"]:
        failure_point = "submission"
    elif not chain["canonical_accepted"]:
        failure_point = "canonical_acceptance"
    elif not chain["post_landing_verified"]:
        failure_point = "post_landing_verification"

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "config_provider": config.vcs.provider,
        "budget_snapshot": get_budget_snapshot(config.budget),
        "run": asdict(result),
        "dependency_flow": {
            "multi_step": len(result.slices) > 1,
            "blocked_event_count": blocked_event_count,
            "slices": per_slice_dependency,
        },
        "proof": {
            "success": success,
            "failure_point": failure_point,
            "provider": provider,
            "task_id": slice_result.task_id if slice_result else None,
            "trace_id": (
                task_events.get(slice_result.task_id, {}).get("trace_id")
                if slice_result
                else None
            ),
            "branch_name": (
                submission_events.get(slice_result.task_id, {}).get("branch_name")
                if slice_result
                else None
            ),
            "submission_id": (
                submission_events.get(slice_result.task_id, {}).get("submission_id")
                if slice_result
                else None
            ),
            "submission_url": (
                submission_events.get(slice_result.task_id, {}).get("url")
                if slice_result
                else None
            ),
            "source": (
                submission_events.get(slice_result.task_id, {}).get("source")
                if slice_result
                else None
            ),
            "source_run_id": (
                submission_events.get(slice_result.task_id, {}).get("source_run_id")
                if slice_result
                else None
            ),
            "source_task_id": (
                submission_events.get(slice_result.task_id, {}).get("source_task_id")
                if slice_result
                else None
            ),
            "source_agent_id": (
                submission_events.get(slice_result.task_id, {}).get("source_agent_id")
                if slice_result
                else None
            ),
            "source_attempt_id": (
                submission_events.get(slice_result.task_id, {}).get("source_attempt_id")
                if slice_result
                else None
            ),
            "intent_type": (
                submission_events.get(slice_result.task_id, {}).get("intent_type")
                if slice_result
                else None
            ),
            "terminal_state": (
                str(
                    submission_events.get(slice_result.task_id, {}).get("status")
                    or task_events.get(slice_result.task_id, {}).get("state_to")
                    or ""
                )
                or None
                if slice_result
                else None
            ),
            "settlement_reason": (
                submission_events.get(slice_result.task_id, {}).get("reason")
                or task_events.get(slice_result.task_id, {}).get("reason")
                if slice_result
                else None
            ),
            "canonical_status": (
                str(submission_events.get(slice_result.task_id, {}).get("canonical_status") or "") or None
                if slice_result
                else None
            ),
            "landing_verification_status": (
                str(submission_events.get(slice_result.task_id, {}).get("landing_verification_status") or "") or None
                if slice_result
                else None
            ),
            "landing_verification_detail": (
                str(submission_events.get(slice_result.task_id, {}).get("landing_verification_detail") or "") or None
                if slice_result
                else None
            ),
            "local_verification_passed": bool(slice_result and slice_result.verification_passed),
            "changed_files": slice_result.changed_files if slice_result else [],
            "goal": slice_result.goal if slice_result else None,
        },
        "chain": chain,
    }


def _write_report(report_dir: Path, payload: dict[str, Any]) -> None:
    json_path = report_dir / "delivery_proof.json"
    md_path = report_dir / "delivery_proof.md"
    with open(json_path, "w") as handle:
        json.dump(payload, handle, indent=2)

    proof = payload["proof"]
    chain = payload["chain"]
    dependency_flow = payload["dependency_flow"]
    run = payload.get("run") or {}
    lines = [
        "# Delivery Proof Report",
        "",
        f"- generated_at: `{payload['generated_at']}`",
        f"- provider: `{proof['provider']}`",
        f"- success: `{proof['success']}`",
        f"- failure_point: `{proof['failure_point']}`",
        f"- task_id: `{proof['task_id']}`",
        f"- trace_id: `{proof['trace_id']}`",
        f"- branch_name: `{proof['branch_name']}`",
        f"- submission_id: `{proof['submission_id']}`",
        f"- submission_url: `{proof['submission_url']}`",
        f"- source: `{proof['source']}`",
        f"- source_run_id: `{proof['source_run_id']}`",
        f"- source_task_id: `{proof['source_task_id']}`",
        f"- source_agent_id: `{proof['source_agent_id']}`",
        f"- source_attempt_id: `{proof['source_attempt_id']}`",
        f"- intent_type: `{proof['intent_type']}`",
        f"- terminal_state: `{proof['terminal_state']}`",
        f"- settlement_reason: `{proof['settlement_reason']}`",
        f"- canonical_status: `{proof['canonical_status']}`",
        f"- landing_verification_status: `{proof['landing_verification_status']}`",
        f"- landing_verification_detail: `{proof['landing_verification_detail']}`",
        "",
        "## Delivery Chain",
        "",
        f"- intent_compiled: `{chain['intent_compiled']}`",
        f"- artifact_verified_locally: `{chain['artifact_verified_locally']}`",
        f"- submission_recorded: `{chain['submission_recorded']}`",
        f"- canonical_accepted: `{chain['canonical_accepted']}`",
        f"- post_landing_verified: `{chain['post_landing_verified']}`",
        "",
        "## Dependency Flow",
        "",
        f"- multi_step: `{dependency_flow['multi_step']}`",
        f"- blocked_event_count: `{dependency_flow['blocked_event_count']}`",
        "",
        "## Run Summary",
        "",
        f"- run_status: `{run.get('status')}`",
        f"- false_success_rate: `{run.get('false_success_rate')}`",
        f"- duplicate_settlement_events: `{run.get('duplicate_settlement_events')}`",
        f"- duplicate_claim_events: `{run.get('duplicate_claim_events')}`",
        f"- orphaned_workers_after_stop: `{run.get('orphaned_workers_after_stop')}`",
        f"- queue_depth_end: `{run.get('queue_depth_end')}`",
        f"- dead_letter_depth_end: `{run.get('dead_letter_depth_end')}`",
        f"- dashboard_event_truthful: `{run.get('dashboard_event_truthful')}`",
        f"- run_dir: `{run.get('run_dir')}`",
        f"- log_path: `{run.get('log_path')}`",
    ]

    for slice_dependency in dependency_flow["slices"]:
        lines.extend(
            [
                "",
                f"### Slice `{slice_dependency['key']}`",
                f"- task_id: `{slice_dependency['task_id']}`",
                f"- prerequisite_keys: `{slice_dependency['prerequisite_keys']}`",
                f"- prerequisite_task_ids: `{slice_dependency['prerequisite_task_ids']}`",
                f"- source: `{slice_dependency['source']}`",
                f"- source_run_id: `{slice_dependency['source_run_id']}`",
                f"- source_task_id: `{slice_dependency['source_task_id']}`",
                f"- source_agent_id: `{slice_dependency['source_agent_id']}`",
                f"- source_attempt_id: `{slice_dependency['source_attempt_id']}`",
                f"- intent_type: `{slice_dependency['intent_type']}`",
                f"- blocked_event_count: `{slice_dependency['blocked_event_count']}`",
                f"- last_blocked_reason: `{slice_dependency['last_blocked_reason']}`",
                f"- terminal_state: `{slice_dependency['terminal_state']}`",
                f"- verification_passed: `{slice_dependency['verification_passed']}`",
            ]
        )

    with open(md_path, "w") as handle:
        handle.write("\n".join(lines))


def _write_bootstrap_failure_report(
    report_dir: Path,
    *,
    config: MXConfig,
    error: str,
    traceback_text: str | None = None,
) -> None:
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "config_provider": config.vcs.provider,
        "budget_snapshot": get_budget_snapshot(config.budget),
        "run": None,
        "dependency_flow": {
            "multi_step": False,
            "blocked_event_count": 0,
            "slices": [],
        },
        "proof": {
            "success": False,
            "failure_point": "execution_bootstrap",
            "provider": config.vcs.provider,
            "task_id": None,
            "trace_id": None,
            "branch_name": None,
            "submission_id": None,
            "submission_url": None,
            "source": None,
            "source_run_id": None,
            "source_task_id": None,
            "source_agent_id": None,
            "source_attempt_id": None,
            "intent_type": None,
            "terminal_state": None,
            "settlement_reason": error,
            "canonical_status": None,
            "landing_verification_status": None,
            "landing_verification_detail": None,
            "local_verification_passed": False,
            "changed_files": [],
            "goal": None,
        },
        "chain": {
            "intent_compiled": False,
            "artifact_verified_locally": False,
            "submission_recorded": False,
            "canonical_accepted": False,
            "post_landing_verified": False,
        },
    }
    _write_report(report_dir, payload)
    if traceback_text:
        (report_dir / "bootstrap_traceback.txt").write_text(traceback_text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a single end-to-end delivery proof.")
    parser.add_argument(
        "--slice",
        default="jsonpeek",
        help="Qualification slice key to prove, comma-separated keys, or 'all'.",
    )
    parser.add_argument("--corpus", default=None, help="Optional qualification corpus manifest.")
    parser.add_argument("--workers", type=int, default=1, help="Worker count for the proof run.")
    parser.add_argument("--timeout-seconds", type=int, default=1200, help="Timeout for the proof run.")
    parser.add_argument(
        "--retry-max",
        type=int,
        default=0,
        help="Maximum retries per task during proof runs. Defaults to 0 for fail-fast proofs.",
    )
    args = parser.parse_args()

    slice_specs = _resolve_slices(args.slice, args.corpus)
    report_dir = REPO_ROOT / "reports" / "delivery_proof" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report_dir.mkdir(parents=True, exist_ok=True)

    config = MXConfig.load("mx.config.json")
    try:
        results = asyncio.run(
            execute_parallel_corpus(
                runs=1,
                worker_count=max(1, args.workers),
                timeout_seconds=max(60, args.timeout_seconds),
                report_dir=report_dir,
                slices=slice_specs,
                retry_max=max(0, args.retry_max),
            )
        )
        result = results[0]
        payload = _build_proof_payload(result=result, config=config)
        _write_report(report_dir, payload)
        print(f"Delivery proof written to {report_dir}")
        return 0 if payload["proof"]["success"] else 1
    except Exception as exc:
        error = str(exc).strip() or type(exc).__name__
        traceback_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        _write_bootstrap_failure_report(
            report_dir,
            config=config,
            error=error,
            traceback_text=traceback_text,
        )
        print(f"Delivery proof written to {report_dir}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
