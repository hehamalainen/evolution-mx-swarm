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
    state.last_queue_depth = None

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
        "is_running": True,
        "current_phase": "running",
    }

    response = client.post("/api/swarm/heartbeat", json=payload)
    assert response.status_code == 200

    status = client.get("/api/status").json()
    assert status["tasks"]["completed"] == 2
    assert status["tasks"]["failed"] == 1
    assert status["queue"]["depth"] == 7
