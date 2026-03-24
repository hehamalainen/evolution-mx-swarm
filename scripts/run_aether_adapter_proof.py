#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shared.adapters.aether_vcs import AetherVCS  # noqa: E402
from shared.schemas import TaskContext, TaskContract, TaskType  # noqa: E402


def _http_json(method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _safe_http_json(method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        return _http_json(method, url, payload)
    except urllib.error.HTTPError as exc:
        return {
            "error": f"HTTP {exc.code}",
            "body": exc.read().decode("utf-8", errors="replace"),
        }
    except Exception as exc:  # pragma: no cover - live harness only
        return {"error": str(exc)}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _build_diff(path: str, run_id: str, task_id: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        "@@ -0,0 +1,3 @@\n"
        f'+PROOF_RUN_ID = "{run_id}"\n'
        f'+PROOF_TASK_ID = "{task_id}"\n'
        '+STATUS = "deterministic"\n'
    )


def _classify_failure(
    *,
    repo_response: dict[str, Any],
    submit_invoked: bool,
    submit_result: dict[str, Any] | None,
    proposal_visible: bool,
    provenance_ok: bool,
    physics_ok: bool,
    terminal_status: str | None,
    proposal_id: str | None,
) -> str | None:
    if not repo_response.get("id"):
        return "repo_create_failed"
    if not submit_invoked:
        return "adapter_not_invoked"
    if submit_result is None:
        return "submit_started_no_response"
    if not proposal_id:
        return "submit_response_no_proposal_id"
    if not proposal_visible:
        return "proposal_id_returned_not_persisted"
    if not provenance_ok:
        return "proposal_persisted_provenance_missing"
    if not physics_ok:
        return "proposal_persisted_physics_missing"
    if terminal_status is None:
        return "proposal_visible_no_terminal_status"
    return None


async def _run_proof(args: argparse.Namespace) -> tuple[dict[str, Any], Path]:
    report_dir = REPO_ROOT / "reports" / "aether_adapter_proof" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report_dir.mkdir(parents=True, exist_ok=True)

    run_id = args.run_id or f"mx-aether-adapter-proof-{report_dir.name.lower()}"
    os.environ["MX_RUN_ID"] = run_id

    _write_json(report_dir / "aether_health.json", _safe_http_json("GET", f"{args.aether_base_url}/v1/health"))
    repo_response = _safe_http_json(
        "POST",
        f"{args.aether_base_url}/v1/repos",
        {"name": args.repo_name or f"mx-adapter-proof-{report_dir.name.lower()}", "description": f"MX adapter proof {run_id}"},
    )
    repo_id = str(repo_response.get("id") or "")

    task = TaskContract.create(
        task_type=TaskType.FEATURE,
        goal="Deterministic MX -> Aether adapter proof",
        context=TaskContext(files_hint=[f"proofs/{run_id}.py"]),
    )
    task.attempt_id = "attempt-1"
    worker_id = "proof-worker-0"
    intent_type = "feature"
    affected_path = f"proofs/{run_id}.py"
    diff = _build_diff(affected_path, run_id, task.id)
    evidence_markdown = (
        "## Deterministic Adapter Proof\n\n"
        f"- run_id: `{run_id}`\n"
        f"- task_id: `{task.id}`\n"
        f"- worker_id: `{worker_id}`\n"
    )

    adapter = AetherVCS(
        base_url=args.aether_base_url,
        repo_id=repo_id or None,
        agent_id="mx-swarm",
        max_retries=1,
        base_backoff_s=0,
    )

    submit_payload = adapter._build_proposal_payload(
        task_id=task.id,
        goal=task.goal,
        branch_name=task.branch_name(),
        diff=diff,
        affected_paths=[affected_path],
        evidence_markdown=evidence_markdown,
        confidence=1.0,
        model_id="deterministic-proof",
        risk_tier=0,
        source="mx_swarm",
        source_run_id=run_id,
        source_task_id=task.id,
        source_agent_id=worker_id,
        source_attempt_id=task.attempt_id,
        intent_type=intent_type,
    )
    _write_json(report_dir / "mx_submit_payload.json", submit_payload)

    submit_invoked = False
    submit_result = None
    if repo_id:
        submit_invoked = True
        result = await adapter.submit_change(
            task_id=task.id,
            goal=task.goal,
            branch_name=task.branch_name(),
            diff=diff,
            affected_paths=[affected_path],
            evidence_markdown=evidence_markdown,
            confidence=1.0,
            model_id="deterministic-proof",
            risk_tier=0,
            draft=False,
            source="mx_swarm",
            source_run_id=run_id,
            source_task_id=task.id,
            source_agent_id=worker_id,
            source_attempt_id=task.attempt_id,
            intent_type=intent_type,
        )
        submit_result = asdict(result)
        _write_json(report_dir / "mx_submit_response.json", submit_result)

    _write_json(report_dir / "aether_repo.json", repo_response)

    proposal_id = str((submit_result or {}).get("submission_id") or "") or None
    proposals_payload: dict[str, Any] = {"proposals": [], "total": 0}
    proposal_detail: dict[str, Any] | None = None
    proposal_visible = False
    provenance_ok = False
    physics_ok = False
    terminal_status: str | None = None
    terminal_statuses = {"PROMOTED", "REJECTED", "QUARANTINED", "SUPERSEDED"}

    for _ in range(max(1, args.poll_attempts)):
        proposals_payload = _safe_http_json("GET", f"{args.aether_base_url}/v1/repos/{repo_id}/proposals")
        proposals = proposals_payload.get("proposals") if isinstance(proposals_payload, dict) else []
        selected = None
        if isinstance(proposals, list):
            for proposal in proposals:
                if str(proposal.get("id") or "") == (proposal_id or ""):
                    selected = proposal
                    break
        if selected is not None:
            proposal_visible = True
            proposal_detail = _safe_http_json(
                "GET",
                f"{args.aether_base_url}/v1/repos/{repo_id}/proposals/{proposal_id}",
            )
            observed_status = str(proposal_detail.get("status") or "") or None
            terminal_status = observed_status
            tuple_values = {
                "source": proposal_detail.get("source"),
                "source_run_id": proposal_detail.get("source_run_id"),
                "source_task_id": proposal_detail.get("source_task_id"),
                "source_agent_id": proposal_detail.get("source_agent_id"),
                "source_attempt_id": proposal_detail.get("source_attempt_id"),
                "intent_type": proposal_detail.get("intent_type"),
            }
            provenance_ok = tuple_values == {
                "source": "mx_swarm",
                "source_run_id": run_id,
                "source_task_id": task.id,
                "source_agent_id": worker_id,
                "source_attempt_id": task.attempt_id,
                "intent_type": intent_type,
            }
            if observed_status in terminal_statuses:
                break
        await asyncio.sleep(args.poll_interval_seconds)

    physics_payload = _safe_http_json(
        "GET",
        f"{args.aether_base_url}/v1/system/physics?window_seconds=600",
    )
    mx_source = ((physics_payload.get("by_source") or {}).get("mx_swarm") or {})
    submit_accepted = int(((mx_source.get("events") or {}).get("submit_accepted")) or 0)
    physics_ok = submit_accepted >= 1

    _write_json(report_dir / "aether_proposals.json", proposals_payload)
    if proposal_detail is not None and proposal_id is not None:
        _write_json(report_dir / f"aether_proposal_{proposal_id}.json", proposal_detail)
    _write_json(report_dir / "aether_physics.json", physics_payload)

    failure_stage = _classify_failure(
        repo_response=repo_response,
        submit_invoked=submit_invoked,
        submit_result=submit_result,
        proposal_visible=proposal_visible,
        provenance_ok=provenance_ok,
        physics_ok=physics_ok,
        terminal_status=terminal_status,
        proposal_id=proposal_id,
    )
    phase1_pass = all([
        bool(repo_id),
        bool(proposal_id),
        proposal_visible,
        provenance_ok,
        physics_ok,
    ])
    pass_value = phase1_pass

    proof_result = {
        "timestamps": {
            "generated_at": datetime.now(UTC).isoformat(),
        },
        "mx_run_id": run_id,
        "repo_id": repo_id,
        "proposal_id": proposal_id,
        "source_tuple": {
            "source": "mx_swarm",
            "source_run_id": run_id,
            "source_task_id": task.id,
            "source_agent_id": worker_id,
            "source_attempt_id": task.attempt_id,
            "intent_type": intent_type,
        },
        "submit_http_status": (submit_result or {}).get("http_status"),
        "proposal_visible": proposal_visible,
        "physics_visible": physics_ok,
        "terminal_status": terminal_status,
        "pass": pass_value,
        "phase1_pass": phase1_pass,
        "failure_stage": failure_stage,
    }
    _write_json(report_dir / "proof_result.json", proof_result)

    summary_lines = [
        "# Deterministic MX -> Aether Adapter Proof",
        "",
        f"- mx_run_id: `{run_id}`",
        f"- repo_id: `{repo_id}`",
        f"- proposal_id: `{proposal_id}`",
        f"- submit_http_status: `{proof_result['submit_http_status']}`",
        f"- proposal_visible: `{proposal_visible}`",
        f"- physics_visible: `{physics_ok}`",
        f"- terminal_status: `{terminal_status}`",
        f"- phase1_pass: `{phase1_pass}`",
        f"- pass: `{pass_value}`",
        f"- failure_stage: `{failure_stage}`",
    ]
    (report_dir / "proof_summary.md").write_text("\n".join(summary_lines), encoding="utf-8")

    return proof_result, report_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a deterministic MX -> Aether adapter proof.")
    parser.add_argument("--aether-base-url", default="http://127.0.0.1:8100", help="Aether base URL.")
    parser.add_argument("--repo-name", default=None, help="Optional proof repository name.")
    parser.add_argument("--run-id", default=None, help="Explicit MX run identifier.")
    parser.add_argument("--poll-attempts", type=int, default=20, help="Maximum polling attempts.")
    parser.add_argument("--poll-interval-seconds", type=float, default=1.0, help="Polling interval.")
    args = parser.parse_args()

    proof_result, report_dir = asyncio.run(_run_proof(args))
    print(f"Deterministic adapter proof written to {report_dir}")
    return 0 if proof_result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
