import json
from pathlib import Path

from scripts.run_delivery_proof import _build_proof_payload, _resolve_slices
from scripts.run_gate2_local_parallel import RunResult, SliceResult
from shared.utils.config import MXConfig


def test_resolve_slices_supports_all_for_dependency_corpus() -> None:
    slices = _resolve_slices("all", "qualification/corpora/dependency_chain_local_open_weight.json")

    assert [slice.key for slice in slices] == ["slugcore", "slugcli"]
    assert slices[1].prerequisite_keys == ["slugcore"]


def test_build_proof_payload_includes_dependency_flow(tmp_path: Path) -> None:
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    (runtime / "submission_events.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "task_id": "task-a",
                        "provider": "github",
                        "status": "settled_ack",
                        "goal": "Implement slugcore",
                        "submission_id": "1",
                        "branch_name": "task/task-a",
                        "source": "mx_swarm",
                        "source_run_id": "run-a",
                        "source_task_id": "task-a",
                        "source_agent_id": "worker-a",
                        "source_attempt_id": "attempt-1",
                        "intent_type": "feature",
                    }
                ),
                json.dumps(
                    {
                        "task_id": "task-b",
                        "provider": "github",
                        "status": "settled_ack",
                        "goal": "Implement slugcli",
                        "submission_id": "2",
                        "branch_name": "task/task-b",
                        "source": "mx_swarm",
                        "source_run_id": "run-b",
                        "source_task_id": "task-b",
                        "source_agent_id": "worker-b",
                        "source_attempt_id": "attempt-1",
                        "intent_type": "feature",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (runtime / "task_state_events.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"task_id": "task-a", "trace_id": "trace-a", "state_to": "settled_ack", "timestamp": "2026-03-12T00:00:00+00:00"}),
                json.dumps({"task_id": "task-b", "trace_id": "trace-b", "state_to": "blocked", "reason": "DEPENDENCY_WAIT", "timestamp": "2026-03-12T00:00:01+00:00"}),
                json.dumps({"task_id": "task-b", "trace_id": "trace-b", "state_to": "queued", "reason": "DEPENDENCY_READY", "timestamp": "2026-03-12T00:00:02+00:00"}),
                json.dumps({"task_id": "task-b", "trace_id": "trace-b", "state_to": "settled_ack", "timestamp": "2026-03-12T00:00:03+00:00"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    config = MXConfig()
    result = RunResult(
        run_number=1,
        run_dir=str(tmp_path),
        status="passed",
        worker_count=2,
        false_success_rate=0.0,
        duplicate_settlement_events=0,
        duplicate_claim_events=0,
        orphaned_workers_after_stop=0,
        stop_convergence_seconds=0.5,
        queue_depth_end=0,
        dead_letter_depth_end=0,
        dashboard_event_truthful=True,
        slices=[
            SliceResult(
                key="slugcore",
                task_id="task-a",
                goal="Implement slugcore",
                prerequisite_keys=[],
                prerequisite_task_ids=[],
                terminal_state="settled_ack",
                terminal_reason=None,
                branch_name="task/task-a",
                submission_id="1",
                submission_url=None,
                settled_at=None,
                verification_passed=True,
                changed_files=["tools/slugcore.py"],
                blocked_event_count=0,
                last_blocked_reason=None,
                failure_reason=None,
            ),
            SliceResult(
                key="slugcli",
                task_id="task-b",
                goal="Implement slugcli",
                prerequisite_keys=["slugcore"],
                prerequisite_task_ids=["task-a"],
                terminal_state="settled_ack",
                terminal_reason=None,
                branch_name="task/task-b",
                submission_id="2",
                submission_url=None,
                settled_at=None,
                verification_passed=True,
                changed_files=["tools/slugcli.py"],
                blocked_event_count=1,
                last_blocked_reason="DEPENDENCY_WAIT",
                failure_reason=None,
            ),
        ],
    )

    payload = _build_proof_payload(result=result, config=config)

    assert payload["proof"]["success"] is True
    assert payload["dependency_flow"]["multi_step"] is True
    assert payload["dependency_flow"]["blocked_event_count"] == 1
    assert payload["dependency_flow"]["slices"][1]["prerequisite_keys"] == ["slugcore"]
    assert payload["proof"]["source"] is None
    assert payload["dependency_flow"]["slices"][0]["source"] == "mx_swarm"
    assert payload["dependency_flow"]["slices"][0]["source_task_id"] == "task-a"
    assert payload["dependency_flow"]["slices"][0]["source_agent_id"] == "worker-a"
    assert payload["dependency_flow"]["slices"][0]["source_attempt_id"] == "attempt-1"
    assert payload["dependency_flow"]["slices"][0]["intent_type"] == "feature"
