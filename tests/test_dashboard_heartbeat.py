from fastapi.testclient import TestClient

from dashboard.api import app, state


def test_swarm_heartbeat_updates_status_metrics() -> None:
    client = TestClient(app)

    # Reset relevant state to a known baseline
    state.tasks_completed = 0
    state.tasks_failed = 0
    state.prs_opened = 0
    state.prs_merged = 0
    state.submissions_opened = 0
    state.submissions_accepted = 0
    state.tasks_in_progress_runtime = 0
    state.last_queue_depth = None
    state.last_phase = None
    state.workers.clear()
    state.active_tasks.clear()

    payload = {
        "active_workers": 2,
        "worker_details": [
            {"id": "worker-1", "status": "active", "task_id": "task-1", "task_goal": "Do work", "spec_ref": "REQ-1"},
            {"id": "worker-2", "status": "idle", "task_id": None},
        ],
        "tasks_in_progress": 1,
        "tasks_completed": 2,
        "tasks_failed": 1,
        "tasks_processed": 3,
        "prs_opened": 0,
        "prs_merged": 0,
        "submissions_opened": 2,
        "submissions_accepted": 0,
        "ci_passes": 0,
        "ci_fails": 0,
        "queue_depth": 7,
        "target_workers": 2,
        "runtime_cap": 2,
        "is_running": True,
        "current_phase": "running",
    }

    response = client.post("/api/swarm/heartbeat", json=payload)
    assert response.status_code == 200

    status = client.get("/api/status").json()
    assert status["tasks"]["completed"] == 2
    assert status["tasks"]["failed"] == 1
    assert status["queue"]["depth"] == 7
    assert status["worker_state"]["runtime_target"] == 2
    assert status["worker_state"]["runtime_cap"] == 2
    assert status["agents"]["max"] == 2


def test_swarm_heartbeat_phase_logging_suppresses_running_noise() -> None:
    client = TestClient(app)
    state.logs.clear()
    state.last_phase = None

    payload = {
        "active_workers": 1,
        "worker_details": [{"id": "worker-0", "status": "idle", "task_id": None}],
        "tasks_in_progress": 0,
        "tasks_completed": 0,
        "tasks_failed": 0,
        "tasks_processed": 0,
        "queue_depth": 0,
        "current_phase": "running",
    }

    assert client.post("/api/swarm/heartbeat", json=payload).status_code == 200
    assert client.post("/api/swarm/heartbeat", json=payload).status_code == 200

    running_phase_logs = [entry for entry in state.logs if "Swarm phase:" in entry.get("message", "")]
    assert len(running_phase_logs) == 0


def test_swarm_heartbeat_phase_logging_records_non_running_transition() -> None:
    client = TestClient(app)
    state.logs.clear()
    state.last_phase = "running"

    payload = {
        "active_workers": 1,
        "worker_details": [{"id": "worker-0", "status": "idle", "task_id": None}],
        "tasks_in_progress": 0,
        "tasks_completed": 0,
        "tasks_failed": 0,
        "tasks_processed": 0,
        "queue_depth": 0,
        "current_phase": "stopping",
    }

    assert client.post("/api/swarm/heartbeat", json=payload).status_code == 200
    phase_logs = [entry for entry in state.logs if entry.get("message") == "Swarm phase: stopping"]
    assert len(phase_logs) == 1


def test_swarm_heartbeat_replaces_worker_snapshot() -> None:
    client = TestClient(app)

    first = {
        "active_workers": 2,
        "worker_details": [
            {"id": "worker-0", "status": "active", "task_id": "task-a", "task_goal": "A", "spec_ref": "REQ-1"},
            {"id": "worker-1", "status": "idle", "task_id": None},
        ],
        "tasks_in_progress": 1,
        "tasks_completed": 0,
        "tasks_failed": 0,
        "tasks_processed": 1,
        "queue_depth": 1,
    }
    second = {
        "active_workers": 1,
        "worker_details": [{"id": "worker-1", "status": "idle", "task_id": None}],
        "tasks_in_progress": 0,
        "tasks_completed": 0,
        "tasks_failed": 0,
        "tasks_processed": 1,
        "queue_depth": 0,
    }

    assert client.post("/api/swarm/heartbeat", json=first).status_code == 200
    assert client.post("/api/swarm/heartbeat", json=second).status_code == 200

    assert set(state.workers.keys()) == {"worker-1"}
    assert state.active_tasks == {}
