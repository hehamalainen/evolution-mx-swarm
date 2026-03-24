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
