"""Runtime task snapshot registry for judge-side rerouting."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

from shared.schemas import TaskContract

TASK_SNAPSHOT_DIR = os.path.join(".runtime", "task_snapshots")


def _task_snapshot_path(task_id: str, *, repo_path: str = ".") -> str:
    normalized = str(task_id).strip()
    return os.path.join(repo_path, TASK_SNAPSHOT_DIR, f"{normalized}.json")


def snapshot_task(task: TaskContract, *, repo_path: str = ".", actor_id: str = "system") -> None:
    """Persist the latest task contract so the judge can re-route it later."""
    path = _task_snapshot_path(task.id, repo_path=repo_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "updated_at": datetime.now(UTC).isoformat(),
        "actor_id": actor_id,
        "task": task.to_dict(),
    }
    with open(path, "w") as handle:
        json.dump(payload, handle, indent=2)


def load_task_snapshot(task_id: str, *, repo_path: str = ".") -> TaskContract | None:
    """Load the latest persisted task contract for a task id."""
    path = _task_snapshot_path(task_id, repo_path=repo_path)
    if not os.path.exists(path):
        return None
    with open(path) as handle:
        payload = json.load(handle)
    task_payload = payload.get("task", payload)
    if not isinstance(task_payload, dict):
        return None
    return TaskContract.from_dict(task_payload)
