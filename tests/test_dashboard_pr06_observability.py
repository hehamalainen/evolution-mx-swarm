import json
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

import shared.utils.metrics as metrics_mod
from dashboard.api import app


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def test_pr06_observability_endpoints_expose_formulas_and_traces(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    metrics_mod._collector = None

    now = datetime.now(UTC)
    events = [
        {
            "event_id": "evt-1",
            "trace_id": "trace-abc",
            "task_id": "task-1",
            "attempt_id": "attempt-1",
            "state_from": "queued",
            "state_to": "claimed",
            "reason": None,
            "provider": "memory",
            "timestamp": now.isoformat(),
            "actor_id": "worker-1",
        },
        {
            "event_id": "evt-2",
            "trace_id": "trace-abc",
            "task_id": "task-1",
            "attempt_id": "attempt-1",
            "state_from": "claimed",
            "state_to": "executing",
            "reason": None,
            "provider": "memory",
            "timestamp": (now + timedelta(seconds=1)).isoformat(),
            "actor_id": "worker-1",
        },
        {
            "event_id": "evt-3",
            "trace_id": "trace-abc",
            "task_id": "task-1",
            "attempt_id": "attempt-1",
            "state_from": "executing",
            "state_to": "verified",
            "reason": None,
            "provider": "memory",
            "timestamp": (now + timedelta(seconds=2)).isoformat(),
            "actor_id": "worker-1",
        },
        {
            "event_id": "evt-4",
            "trace_id": "trace-abc",
            "task_id": "task-1",
            "attempt_id": "attempt-1",
            "state_from": "verified",
            "state_to": "submitted_pending_ledger",
            "reason": None,
            "provider": "memory",
            "timestamp": (now + timedelta(seconds=3)).isoformat(),
            "actor_id": "worker-1",
        },
        {
            "event_id": "evt-5",
            "trace_id": "trace-abc",
            "task_id": "task-1",
            "attempt_id": "attempt-1",
            "state_from": "submitted_pending_ledger",
            "state_to": "settled_ack",
            "reason": None,
            "provider": "memory",
            "timestamp": (now + timedelta(seconds=4)).isoformat(),
            "actor_id": "worker-1",
        },
    ]
    _write_jsonl(tmp_path / ".runtime" / "task_state_events.jsonl", events)

    spans = [
        {
            "span_id": "span-1",
            "trace_id": "trace-abc",
            "parent_span_id": None,
            "service": "planner",
            "operation": "publish_task",
            "task_id": "task-1",
            "attempt_id": "attempt-1",
            "status": "ok",
            "actor_id": "planner",
            "timestamp": now.isoformat(),
            "attributes": {},
        },
        {
            "span_id": "span-2",
            "trace_id": "trace-abc",
            "parent_span_id": "span-1",
            "service": "worker",
            "operation": "execute_task",
            "task_id": "task-1",
            "attempt_id": "attempt-1",
            "status": "ok",
            "actor_id": "worker-1",
            "timestamp": (now + timedelta(seconds=1)).isoformat(),
            "attributes": {},
        },
        {
            "span_id": "span-3",
            "trace_id": "trace-abc",
            "parent_span_id": "span-2",
            "service": "aether",
            "operation": "submit_change",
            "task_id": "task-1",
            "attempt_id": "attempt-1",
            "status": "ok",
            "actor_id": "worker-1",
            "timestamp": (now + timedelta(seconds=2)).isoformat(),
            "attributes": {},
        },
    ]
    _write_jsonl(tmp_path / ".runtime" / "trace_spans.jsonl", spans)

    with open(tmp_path / ".runtime" / "aether_reconciliation_backlog.json", "w") as handle:
        json.dump([], handle)

    client = TestClient(app)

    metrics_response = client.get("/api/metrics/event_sourced")
    assert metrics_response.status_code == 200
    metrics_payload = metrics_response.json()
    assert metrics_payload["metrics"]["false_success_rate"] == 0.0
    assert metrics_payload["metrics"]["trace_continuity_ratio"] == 1.0
    assert metrics_payload["gate_a_ready"] is True
    cards = {card["metric"]: card for card in metrics_payload["formula_cards"]}
    assert cards["false_success_rate"]["formula"] == "false_success_events / max(settled_ack_events, 1)"
    assert cards["false_success_rate"]["threshold"]["pass"] is True

    dictionary_response = client.get("/api/metrics/dictionary")
    assert dictionary_response.status_code == 200
    dictionary = dictionary_response.json()["metric_dictionary"]
    assert "settlement_correctness" in dictionary
    assert "formula" in dictionary["settlement_correctness"]

    traces_response = client.get("/api/traces?limit=50")
    assert traces_response.status_code == 200
    traces_payload = traces_response.json()
    assert traces_payload["count"] == 3

    trace_detail = client.get("/api/traces/trace-abc")
    assert trace_detail.status_code == 200
    detail_payload = trace_detail.json()
    assert detail_payload["span_count"] == 3
    assert detail_payload["event_count"] == 5
    assert detail_payload["services"] == ["aether", "planner", "worker"]

    not_found = client.get("/api/traces/trace-missing")
    assert not_found.status_code == 404


def test_submission_endpoints_expose_recent_settlements(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    metrics_mod._collector = None
    (tmp_path / ".runtime").mkdir(parents=True, exist_ok=True)

    now = datetime.now(UTC)
    _write_jsonl(
        tmp_path / ".runtime" / "submission_events.jsonl",
        [
            {
                "event_id": "sub-1",
                "task_id": "task-1",
                "trace_id": "trace-1",
                "attempt_id": "attempt-1",
                "branch_name": "task/alpha",
                "submission_id": "1",
                "submission_type": "pr",
                "provider": "github",
                "url": "http://example/pull/1",
                "status": "PENDING",
                "goal": "Alpha",
                "affected_paths": ["tools/textstats.py"],
                "local_mode": True,
                "created_at": now.isoformat(),
                "settled_at": None,
                "reason": None,
            },
            {
                "event_id": "sub-2",
                "task_id": "task-1",
                "trace_id": "trace-1",
                "attempt_id": "attempt-1",
                "branch_name": "task/alpha",
                "submission_id": "1",
                "submission_type": "pr",
                "provider": "github",
                "url": "http://example/pull/1",
                "status": "settled_ack",
                "goal": "Alpha",
                "affected_paths": ["tools/textstats.py"],
                "local_mode": True,
                "created_at": now.isoformat(),
                "settled_at": (now + timedelta(seconds=5)).isoformat(),
                "reason": None,
            },
        ],
    )
    _write_jsonl(tmp_path / ".runtime" / "task_state_events.jsonl", [])
    _write_jsonl(tmp_path / ".runtime" / "trace_spans.jsonl", [])
    with open(tmp_path / ".runtime" / "aether_reconciliation_backlog.json", "w") as handle:
        json.dump([], handle)

    client = TestClient(app)

    recent = client.get("/api/submissions/recent?limit=10")
    assert recent.status_code == 200
    recent_payload = recent.json()
    assert recent_payload["count"] == 1
    assert recent_payload["submissions"][0]["branch_name"] == "task/alpha"
    assert recent_payload["submissions"][0]["submission_id"] == "1"

    status = client.get("/api/status")
    assert status.status_code == 200
    submissions = status.json()["submissions"]
    assert submissions["latest_successful_branch"] == "task/alpha"
    assert submissions["latest_successful_submission_id"] == "1"
    assert submissions["recent_settlement_count"] == 1


def test_status_includes_pr06_observability_payload(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    metrics_mod._collector = None
    (tmp_path / ".runtime").mkdir(parents=True, exist_ok=True)

    with open(tmp_path / ".runtime" / "task_state_events.jsonl", "w") as handle:
        handle.write("")
    with open(tmp_path / ".runtime" / "trace_spans.jsonl", "w") as handle:
        handle.write("")
    with open(tmp_path / ".runtime" / "submission_events.jsonl", "w") as handle:
        handle.write("")
    with open(tmp_path / ".runtime" / "aether_reconciliation_backlog.json", "w") as handle:
        json.dump([], handle)

    client = TestClient(app)
    status = client.get("/api/status")
    assert status.status_code == 200
    payload = status.json()
    assert "observability" in payload
    assert "metrics" in payload["observability"]
    assert "formula_cards" in payload["observability"]
    assert "submissions" in payload
