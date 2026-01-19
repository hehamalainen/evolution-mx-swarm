"""Coordination utilities for preventing worker collisions.

This module handles:
1. File-level locking to prevent concurrent modifications
2. Task dependency ordering
3. Merge conflict detection and resolution
4. Base branch synchronization
"""

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Set
from enum import Enum


class LockStatus(str, Enum):
    AVAILABLE = "available"
    LOCKED = "locked"
    EXPIRED = "expired"


@dataclass
class FileLock:
    """Lock on a file being modified."""
    file_path: str
    task_id: str
    worker_id: str
    acquired_at: str
    expires_at: str

    def is_expired(self) -> bool:
        return datetime.utcnow() > datetime.fromisoformat(self.expires_at)


@dataclass
class TaskDependency:
    """Tracks dependencies between tasks."""
    task_id: str
    depends_on: list[str]  # Task IDs that must complete first
    files_modified: list[str]  # Files this task will touch
    status: str = "pending"  # pending, ready, in_progress, completed


class CoordinationService:
    """
    Coordinates workers to prevent collisions.

    Strategies:
    1. File locking - only one worker can modify a file at a time
    2. Dependency graph - tasks wait for dependencies
    3. Optimistic locking - detect conflicts at PR time
    4. Rebase on conflict - auto-rebase when main changes
    """

    def __init__(self, lock_file: str = ".mx-locks.json"):
        self.lock_file = lock_file
        self._locks: dict[str, FileLock] = {}
        self._load_locks()

    def _load_locks(self) -> None:
        """Load locks from file."""
        if os.path.exists(self.lock_file):
            try:
                with open(self.lock_file) as f:
                    data = json.load(f)
                    for path, lock_data in data.items():
                        self._locks[path] = FileLock(**lock_data)
            except (json.JSONDecodeError, KeyError):
                self._locks = {}

    def _save_locks(self) -> None:
        """Save locks to file."""
        data = {
            path: {
                "file_path": lock.file_path,
                "task_id": lock.task_id,
                "worker_id": lock.worker_id,
                "acquired_at": lock.acquired_at,
                "expires_at": lock.expires_at,
            }
            for path, lock in self._locks.items()
        }
        with open(self.lock_file, "w") as f:
            json.dump(data, f, indent=2)

    def _cleanup_expired(self) -> None:
        """Remove expired locks."""
        expired = [path for path, lock in self._locks.items() if lock.is_expired()]
        for path in expired:
            del self._locks[path]
        if expired:
            self._save_locks()

    def acquire_file_lock(
        self,
        file_path: str,
        task_id: str,
        worker_id: str,
        timeout_minutes: int = 60,
    ) -> tuple[bool, Optional[str]]:
        """
        Attempt to acquire lock on a file.

        Returns:
            (success, holder_task_id if locked by another)
        """
        self._cleanup_expired()

        # Check if already locked
        if file_path in self._locks:
            existing = self._locks[file_path]
            if not existing.is_expired() and existing.task_id != task_id:
                return False, existing.task_id

        # Acquire lock
        now = datetime.utcnow()
        self._locks[file_path] = FileLock(
            file_path=file_path,
            task_id=task_id,
            worker_id=worker_id,
            acquired_at=now.isoformat(),
            expires_at=(now + timedelta(minutes=timeout_minutes)).isoformat(),
        )
        self._save_locks()
        return True, None

    def release_file_lock(self, file_path: str, task_id: str) -> bool:
        """Release a file lock."""
        if file_path in self._locks:
            if self._locks[file_path].task_id == task_id:
                del self._locks[file_path]
                self._save_locks()
                return True
        return False

    def acquire_files_for_task(
        self,
        files: list[str],
        task_id: str,
        worker_id: str,
    ) -> tuple[bool, list[str]]:
        """
        Attempt to acquire locks on all files for a task.

        Returns:
            (success, list of files that couldn't be locked)
        """
        failed = []
        acquired = []

        for file_path in files:
            success, holder = self.acquire_file_lock(file_path, task_id, worker_id)
            if success:
                acquired.append(file_path)
            else:
                failed.append(file_path)

        # If any failed, release all acquired (atomic acquisition)
        if failed:
            for file_path in acquired:
                self.release_file_lock(file_path, task_id)
            return False, failed

        return True, []

    def release_files_for_task(self, files: list[str], task_id: str) -> None:
        """Release all file locks for a task."""
        for file_path in files:
            self.release_file_lock(file_path, task_id)

    def get_lock_status(self, file_path: str) -> tuple[LockStatus, Optional[FileLock]]:
        """Get lock status for a file."""
        self._cleanup_expired()

        if file_path not in self._locks:
            return LockStatus.AVAILABLE, None

        lock = self._locks[file_path]
        if lock.is_expired():
            return LockStatus.EXPIRED, lock

        return LockStatus.LOCKED, lock


class DependencyGraph:
    """
    Manages task dependencies to ensure correct execution order.

    Tasks that modify the same files should be serialized.
    Tasks with explicit dependencies wait for predecessors.
    """

    def __init__(self, graph_file: str = ".mx-deps.json"):
        self.graph_file = graph_file
        self._tasks: dict[str, TaskDependency] = {}
        self._load_graph()

    def _load_graph(self) -> None:
        """Load dependency graph from file."""
        if os.path.exists(self.graph_file):
            try:
                with open(self.graph_file) as f:
                    data = json.load(f)
                    for task_id, task_data in data.items():
                        self._tasks[task_id] = TaskDependency(**task_data)
            except (json.JSONDecodeError, KeyError):
                self._tasks = {}

    def _save_graph(self) -> None:
        """Save dependency graph to file."""
        data = {
            task_id: {
                "task_id": dep.task_id,
                "depends_on": dep.depends_on,
                "files_modified": dep.files_modified,
                "status": dep.status,
            }
            for task_id, dep in self._tasks.items()
        }
        with open(self.graph_file, "w") as f:
            json.dump(data, f, indent=2)

    def register_task(
        self,
        task_id: str,
        files_to_modify: list[str],
        explicit_deps: list[str] = None,
    ) -> list[str]:
        """
        Register a task and compute its dependencies.

        Returns:
            List of task IDs this task must wait for
        """
        # Find implicit dependencies (tasks modifying same files)
        implicit_deps = set()
        for other_id, other_task in self._tasks.items():
            if other_id == task_id:
                continue
            if other_task.status in ("pending", "in_progress"):
                # Check file overlap
                overlap = set(files_to_modify) & set(other_task.files_modified)
                if overlap:
                    implicit_deps.add(other_id)

        all_deps = list(implicit_deps)
        if explicit_deps:
            all_deps.extend(explicit_deps)
        all_deps = list(set(all_deps))  # Dedupe

        self._tasks[task_id] = TaskDependency(
            task_id=task_id,
            depends_on=all_deps,
            files_modified=files_to_modify,
            status="pending",
        )
        self._save_graph()

        return all_deps

    def is_task_ready(self, task_id: str) -> tuple[bool, list[str]]:
        """
        Check if a task is ready to execute.

        Returns:
            (ready, list of blocking task IDs)
        """
        if task_id not in self._tasks:
            return True, []

        task = self._tasks[task_id]
        blocking = []

        for dep_id in task.depends_on:
            if dep_id in self._tasks:
                dep_task = self._tasks[dep_id]
                if dep_task.status != "completed":
                    blocking.append(dep_id)

        return len(blocking) == 0, blocking

    def mark_task_started(self, task_id: str) -> None:
        """Mark task as in progress."""
        if task_id in self._tasks:
            self._tasks[task_id].status = "in_progress"
            self._save_graph()

    def mark_task_completed(self, task_id: str) -> None:
        """Mark task as completed."""
        if task_id in self._tasks:
            self._tasks[task_id].status = "completed"
            self._save_graph()

    def get_ready_tasks(self) -> list[str]:
        """Get all tasks that are ready to execute."""
        ready = []
        for task_id, task in self._tasks.items():
            if task.status == "pending":
                is_ready, _ = self.is_task_ready(task_id)
                if is_ready:
                    ready.append(task_id)
        return ready


class ConflictResolver:
    """
    Handles merge conflicts when they occur.

    Strategies:
    1. Auto-rebase: Rebase branch on latest main
    2. Re-execute: Re-run the task with fresh context
    3. Manual: Flag for human review
    """

    @staticmethod
    async def detect_conflict(repo_path: str, branch: str) -> bool:
        """Check if branch has conflicts with main."""
        from shared.utils.git import run_git

        # Fetch latest main
        await run_git(["fetch", "origin", "main"], cwd=repo_path)

        # Try merge with --no-commit to detect conflicts
        result = await run_git(
            ["merge", "--no-commit", "--no-ff", "origin/main"],
            cwd=repo_path,
        )

        # Abort the merge
        await run_git(["merge", "--abort"], cwd=repo_path)

        return not result.success

    @staticmethod
    async def auto_rebase(repo_path: str, branch: str) -> bool:
        """Attempt to auto-rebase branch on main."""
        from shared.utils.git import run_git

        # Fetch latest
        await run_git(["fetch", "origin", "main"], cwd=repo_path)

        # Attempt rebase
        result = await run_git(["rebase", "origin/main"], cwd=repo_path)

        if not result.success:
            # Abort failed rebase
            await run_git(["rebase", "--abort"], cwd=repo_path)
            return False

        return True

    @staticmethod
    async def force_push_after_rebase(repo_path: str, branch: str) -> bool:
        """Force push after successful rebase."""
        from shared.utils.git import run_git

        result = await run_git(
            ["push", "--force-with-lease", "origin", branch],
            cwd=repo_path,
        )
        return result.success


# Integration with Worker
def should_worker_proceed(
    task_id: str,
    worker_id: str,
    files_hint: list[str],
    coordination: CoordinationService,
    deps: DependencyGraph,
) -> tuple[bool, str]:
    """
    Check if worker should proceed with task.

    Returns:
        (should_proceed, reason_if_not)
    """
    # Check dependencies
    is_ready, blocking = deps.is_task_ready(task_id)
    if not is_ready:
        return False, f"Waiting for tasks: {blocking}"

    # Try to acquire file locks
    if files_hint:
        success, failed = coordination.acquire_files_for_task(
            files_hint, task_id, worker_id
        )
        if not success:
            return False, f"Files locked: {failed}"

    return True, ""
