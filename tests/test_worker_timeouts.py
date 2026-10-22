import asyncio
from pathlib import Path

import pytest

from shared.schemas import TaskContract, TaskType
from shared.utils.config import MXConfig
from worker.service import Worker


class _StubQueue:
    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None


class _StubLLM:
    async def chat(self, *args, **kwargs):
        raise AssertionError("LLM should not be called in this unit test")


def test_dependency_tasks_get_longer_local_ollama_timeout(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    config = MXConfig()
    config.llm.provider = "ollama"
    config.llm.model = "mistral:7b"
    worker = Worker(
        config=config,
        worker_id="worker-test",
        queue=_StubQueue(),
        llm=_StubLLM(),
    )

    base_task = TaskContract.create(task_type=TaskType.FEATURE, goal="Implement feature")
    dependent_task = TaskContract.create(task_type=TaskType.FEATURE, goal="Implement dependent feature")
    dependent_task.prerequisite_task_ids = ["task-upstream"]

    assert worker._llm_chat_timeout_seconds(base_task) == 180
    assert worker._llm_chat_timeout_seconds(dependent_task) == 240


@pytest.mark.asyncio
async def test_run_command_timeout_terminates_and_reaps_child_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    worker = Worker(
        config=MXConfig(),
        worker_id="worker-timeout",
        queue=_StubQueue(),
        llm=_StubLLM(),
    )
    marker = tmp_path / "late_child_marker.txt"
    cmd = (
        "python3 -c \"import subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', "
        "'import pathlib, time; time.sleep(0.5); pathlib.Path(\\\"late_child_marker.txt\\\").write_text(\\\"escaped\\\")']); "
        "time.sleep(5)\""
    )
    ok, output = await worker._run_command(cmd, cwd=str(tmp_path), timeout=0.1)
    assert ok is False
    assert "timed out" in output

    await asyncio.sleep(0.7)
    assert not marker.exists(), "Timed-out child process continued running and wrote marker file"

