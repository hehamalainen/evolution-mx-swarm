import asyncio
import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

import worker.service as worker_service
from shared.adapters.queue import QueueMessage
from shared.adapters.vcs_adapter import LandingVerificationResult, SubmitResult
from shared.schemas import (
    AcceptanceCriteria,
    ModelTier,
    NackReason,
    RiskLevel,
    RoutingTarget,
    TaskConstraints,
    TaskContext,
    TaskContract,
    TaskState,
    TaskStream,
    TaskType,
)
from shared.utils.config import MXConfig
from shared.utils.git import GitResult
from worker.service import EvidencePack, Worker


class StubQueue:
    def __init__(self) -> None:
        self.acks: list[str] = []
        self.nacks: list[tuple[str, int, str | None]] = []
        self.dead_letters: list[tuple[str, str]] = []
        self.extended: list[tuple[str, int]] = []

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def ack(self, receipt_handle: str) -> None:
        self.acks.append(receipt_handle)

    async def nack(
        self,
        receipt_handle: str,
        delay: int = 0,
        reason: str | None = None,
    ) -> None:
        self.nacks.append((receipt_handle, delay, reason))

    async def dead_letter(self, receipt_handle: str, reason: str) -> None:
        self.dead_letters.append((receipt_handle, reason))

    async def extend_visibility(self, receipt_handle: str, seconds: int) -> None:
        self.extended.append((receipt_handle, seconds))

    def supports_depth(self) -> bool:
        return False

    def supports_dead_letter(self) -> bool:
        return True

    async def receive(self, max_messages: int = 1, visibility_timeout: int = 300) -> list[QueueMessage]:
        return []


class _LLMResponse:
    def __init__(self, content: str):
        self.content = content


class StubLLM:
    def __init__(self, content: str = "") -> None:
        self.content = content

    async def chat(self, *args, **kwargs):
        return _LLMResponse(self.content)


class PromptCapturingLLM(StubLLM):
    def __init__(self, content: str = "") -> None:
        super().__init__(content)
        self.last_prompt: str | None = None

    async def chat(self, *args, **kwargs):
        messages = kwargs.get("messages") or []
        if messages:
            self.last_prompt = messages[0].content
        return await super().chat(*args, **kwargs)


class RaisingLLM:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def chat(self, *args, **kwargs):
        raise self.exc


class SlowLLM:
    async def chat(self, *args, **kwargs):
        await asyncio.sleep(60)
        return _LLMResponse("")


class CapturingVCS:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def submit_change(self, **kwargs):
        self.calls.append(kwargs)
        return SubmitResult(
            success=True,
            submission_type="proposal",
            submission_id="prop-123",
            status="PENDING",
            message="ok",
            url="http://example/prop-123",
        )

    async def get_status(self, submission_id: str):
        return {"status": "accepted", "submission_id": submission_id, "affected_paths": ["ok.py"]}

    async def verify_landed_change(self, submission_id: str, *, expected_paths: list[str], expected_diff: str | None = None):
        return LandingVerificationResult(
            supported=True,
            verified=True,
            canonical_status="accepted",
            provider="aether",
            detail="verified",
            verified_paths=list(expected_paths),
            raw_response={"submission_id": submission_id},
        )


class FailingLandingVCS(CapturingVCS):
    async def verify_landed_change(self, submission_id: str, *, expected_paths: list[str], expected_diff: str | None = None):
        return LandingVerificationResult(
            supported=True,
            verified=False,
            canonical_status="accepted",
            provider="aether",
            detail="landed artifact integrity mismatch",
            verified_paths=[],
            raw_response={"submission_id": submission_id},
        )


class PromotedStatusVCS(CapturingVCS):
    async def get_status(self, submission_id: str):
        return {"status": "PROMOTED", "submission_id": submission_id}


class FailingCloneRepo:
    def __init__(self, *args, **kwargs) -> None:
        self.local_path = kwargs.get("local_path") or "."

    async def clone(self, branch: str | None = None, depth: int = 1) -> GitResult:
        return GitResult(False, "", "clone failed", 1)

    async def checkout(self, branch: str, create: bool = False) -> GitResult:
        return GitResult(True, "", "", 0)

    def cleanup(self) -> None:
        return None


class SuccessfulRepo:
    def __init__(self, *args, **kwargs) -> None:
        self.local_path = kwargs.get("local_path") or "."
        os.makedirs(self.local_path, exist_ok=True)

    async def clone(self, branch: str | None = None, depth: int = 1) -> GitResult:
        os.makedirs(self.local_path, exist_ok=True)
        return GitResult(True, "", "", 0)

    async def checkout(self, branch: str, create: bool = False) -> GitResult:
        return GitResult(True, "", "", 0)

    async def add(self, paths: list[str] | None = None) -> GitResult:
        return GitResult(True, "", "", 0)

    async def commit(self, message: str, author: str | None = None) -> GitResult:
        return GitResult(True, "", "", 0)

    async def push(self, branch: str, force: bool = False) -> GitResult:
        return GitResult(True, "", "", 0)

    async def diff(self, ref_from: str, ref_to: str) -> GitResult:
        return GitResult(True, "diff --git a/a.py b/a.py\n+print('ok')\n", "", 0)

    async def get_diff(self, staged: bool = False) -> GitResult:
        return GitResult(True, "diff --git a/a.py b/a.py\n+print('ok')\n", "", 0)

    def cleanup(self) -> None:
        return None


class StagingCaptureRepo(SuccessfulRepo):
    last_add_paths: list[str] | None = None

    async def add(self, paths: list[str] | None = None) -> GitResult:
        type(self).last_add_paths = paths
        return await super().add(paths=paths)


class CloneArgCaptureRepo(SuccessfulRepo):
    last_clone_branch: str | None = "unset"

    async def clone(self, branch: str | None = None, depth: int = 1) -> GitResult:
        type(self).last_clone_branch = branch
        return await super().clone(branch=branch, depth=depth)


class DiffFallbackRepo:
    def __init__(self) -> None:
        self.local_path = "."

    async def diff(self, ref_from: str, ref_to: str) -> GitResult:
        return GitResult(False, "", "missing parent", 1)

    async def get_diff(self, staged: bool = False) -> GitResult:
        return GitResult(True, "diff --git a/a.py b/a.py\n+print('ok')\n", "", 0)


def test_overlay_local_workspace_files_copies_selected_untracked_files(tmp_path) -> None:
    source_root = tmp_path / "source"
    workspace_root = tmp_path / "workspace"
    (source_root / "tools").mkdir(parents=True)
    (source_root / "tests").mkdir(parents=True)
    (source_root / "tools" / "textstats.py").write_text("print('tool')\n")
    (source_root / "tests" / "test_textstats.py").write_text("def test_ok():\n    assert True\n")
    (source_root / "PRD.md").write_text("# PRD\n")

    worker = Worker(
        config=MXConfig(),
        worker_id="w1",
        queue=StubQueue(),
        llm=StubLLM(),
        vcs=CapturingVCS(),
    )

    copied = worker._overlay_local_workspace_files(
        str(source_root),
        str(workspace_root),
        [
            "tools/textstats.py",
            "tests/test_textstats.py",
            "PRD.md",
            "tools/textstats.py",
            "../bad.py",
            "missing.py",
        ],
    )

    assert copied == ["tools/textstats.py", "tests/test_textstats.py", "PRD.md"]
    assert (workspace_root / "tools" / "textstats.py").read_text() == "print('tool')\n"
    assert (workspace_root / "tests" / "test_textstats.py").read_text() == (
        "def test_ok():\n    assert True\n"
    )


def test_build_canned_completion_uses_fixture_for_allowed_path(tmp_path, monkeypatch) -> None:
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(
        json.dumps(
            {
                "note": "synthetic worker proof",
                "files": {
                    "tools/jsonpeek.py": "print('ok')\n",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MX_CANNED_COMPLETION_FIXTURE", str(fixture_path))

    worker = Worker(
        config=MXConfig(),
        worker_id="w1",
        queue=StubQueue(),
        llm=PromptCapturingLLM(),
        vcs=CapturingVCS(),
    )
    task = TaskContract.create(
        task_type=TaskType.FEATURE,
        goal="Implement jsonpeek",
        context=TaskContext(files_hint=["tools/jsonpeek.py"], do_not_touch=["tests/test_jsonpeek.py"]),
    )

    canned = worker._build_canned_completion(task)

    assert canned is not None
    response, metadata = canned
    assert "```python:tools/jsonpeek.py" in response.content
    assert "print('ok')" in response.content
    assert metadata["fixture_path"] == str(fixture_path)
    assert metadata["emitted_paths"] == ["tools/jsonpeek.py"]


@pytest.mark.asyncio
async def test_await_canonical_acceptance_treats_promoted_as_accepted() -> None:
    worker = Worker(
        config=MXConfig(),
        worker_id="w1",
        queue=StubQueue(),
        llm=StubLLM(),
        vcs=PromotedStatusVCS(),
    )

    result = await worker._await_canonical_acceptance("prop-123")

    assert result == "accepted"


def test_merge_partial_python_update_rehydrates_existing_scaffold() -> None:
    worker = Worker(
        config=MXConfig(),
        worker_id="w1",
        queue=StubQueue(),
        llm=StubLLM(),
        vcs=CapturingVCS(),
    )

    existing = (
        '"""Example."""\n\n'
        "from pathlib import Path\n\n"
        "def helper() -> str:\n"
        "    return 'ok'\n\n"
        "def summarize_object(payload: dict[str, object]) -> dict[str, object]:\n"
        "    raise NotImplementedError\n\n"
        "if __name__ == \"__main__\":\n"
        "    raise SystemExit(0)\n"
    )
    emitted = (
        "def summarize_object(payload: dict[str, object]) -> dict[str, object]:\n"
        "    keys = sorted(payload.keys())\n"
        "    return {\"key_count\": len(keys), \"keys\": keys}\n"
    )

    merged = worker._merge_partial_python_update(existing, emitted)

    assert merged is not None
    assert '"""Example."""' in merged
    assert "from pathlib import Path" in merged
    assert "def helper() -> str" in merged
    assert "keys = sorted(payload.keys())" in merged
    assert "raise NotImplementedError" not in merged


def test_frontier_claim_block_reason_uses_budget_envelopes(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    config = MXConfig()
    config.budget.daily_usd_cap = 25.0
    config.budget.frontier_daily_usd_cap = 10.0
    config.budget.max_frontier_concurrency = 1
    config.budget.max_frontier_escalations_per_hour = 5
    config.budget.max_frontier_attempts_per_task_contract = 2
    config.budget.cost_per_success_ceiling_usd = 5.0

    worker = Worker(
        config=config,
        worker_id="w1",
        queue=StubQueue(),
        llm=StubLLM(),
        vcs=CapturingVCS(),
    )
    task = TaskContract.create(
        task_type=TaskType.FEATURE,
        goal="frontier",
        context=TaskContext(files_hint=["tools/jsonpeek.py"]),
        acceptance=AcceptanceCriteria(criteria=["works"], checks=[]),
        model_tier=ModelTier.FRONTIER,
        routing_target=RoutingTarget.FRONTIER,
    )
    task.frontier_eligible = True
    task.frontier_attempt_count = 1

    from shared.utils.metrics import append_budget_event

    append_budget_event(
        event_type="frontier_claim_started",
        trace_id=task.trace_id,
        task_id="other-task",
        attempt_id="attempt-1",
        actor_id="worker-9",
        lane="frontier",
        detail="started",
    )

    reason = worker._frontier_claim_block_reason(task)
    assert reason == "frontier concurrency cap reached"


def test_merge_partial_python_update_rehydrates_stub_from_full_module_output() -> None:
    worker = Worker(
        config=MXConfig(),
        worker_id="w1",
        queue=StubQueue(),
        llm=StubLLM(),
        vcs=CapturingVCS(),
    )

    existing = (
        '"""Example."""\n\n'
        "import json\n"
        "from pathlib import Path\n\n"
        "def summarize_document(payload: dict[str, object]) -> dict[str, object]:\n"
        "    raise NotImplementedError\n\n"
        "def main() -> int:\n"
        "    return 0\n"
    )
    emitted = (
        '"""Model tried to rewrite the module."""\n\n'
        "import json\n"
        "from pathlib import Path\n\n"
        "def summarize_document(payload: dict[str, object]) -> dict[str, object]:\n"
        "    keys = sorted(payload.keys())\n"
        "    return {\"key_count\": len(keys), \"keys\": keys}\n"
    )

    merged = worker._merge_partial_python_update(existing, emitted)

    assert merged is not None
    assert '"""Example."""' in merged
    assert "def main() -> int" in merged
    assert "keys = sorted(payload.keys())" in merged
    assert "raise NotImplementedError" not in merged


@pytest.mark.asyncio
async def test_get_codebase_context_includes_read_only_reference_files(tmp_path) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "tools").mkdir(parents=True)
    (repo_root / "tests").mkdir(parents=True)
    (repo_root / "tools" / "textstats.py").write_text("def count_text(text):\n    return {}\n")
    (repo_root / "tests" / "test_textstats.py").write_text(
        "def test_counts() -> None:\n"
        "    assert True\n"
    )
    (repo_root / "README.md").write_text("# noisy context\n")

    worker = Worker(
        config=MXConfig(),
        worker_id="w1",
        queue=StubQueue(),
        llm=StubLLM(),
        vcs=CapturingVCS(),
    )
    repo = SuccessfulRepo(local_path=str(repo_root))
    task = TaskContract.create(
        task_type=TaskType.FEATURE,
        goal="Implement textstats",
        context=TaskContext(
            files_hint=["tools/textstats.py"],
            do_not_touch=["tests/test_textstats.py"],
        ),
    )

    context = await worker._get_codebase_context(repo, task)

    assert "### Editable Target: tools/textstats.py" in context
    assert "### Read-Only Reference: tests/test_textstats.py" in context
    assert "def test_counts() -> None:" in context
    assert "### README.md" not in context
    assert "### File Structure" not in context


@pytest.mark.asyncio
async def test_build_strict_editing_guidance_anchors_existing_scaffold(tmp_path) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "tools").mkdir(parents=True)
    (repo_root / "tools" / "textstats.py").write_text(
        "import argparse\n\n"
        "def build_parser() -> argparse.ArgumentParser:\n"
        "    return argparse.ArgumentParser()\n\n"
        "def count_text(text: str) -> dict[str, int]:\n"
        "    raise NotImplementedError\n"
    )

    worker = Worker(
        config=MXConfig(),
        worker_id="w1",
        queue=StubQueue(),
        llm=StubLLM(),
        vcs=CapturingVCS(),
    )
    repo = SuccessfulRepo(local_path=str(repo_root))

    guidance = await worker._build_strict_editing_guidance(repo, ["tools/textstats.py"])

    assert "Single editable file task" in guidance
    assert "Replace the existing NotImplementedError stub implementations" in guidance
    assert "def build_parser() -> argparse.ArgumentParser:" in guidance
    assert "def count_text(text: str) -> dict[str, int]:" in guidance
    assert "Do not add non-standard-library imports." in guidance


@pytest.mark.asyncio
async def test_implement_task_includes_retry_context_in_prompt(tmp_path) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "tools").mkdir(parents=True)
    (repo_root / "tools" / "jsonpeek.py").write_text(
        "def summarize_object(payload: dict[str, object]) -> dict[str, object]:\n"
        "    raise NotImplementedError\n"
    )

    llm = PromptCapturingLLM(
        "```python:tools/jsonpeek.py\n"
        "def summarize_object(payload: dict[str, object]) -> dict[str, object]:\n"
        "    keys = sorted(payload)\n"
        "    return {\"key_count\": len(keys), \"keys\": keys}\n"
        "```"
    )
    worker = Worker(
        config=MXConfig(),
        worker_id="w1",
        queue=StubQueue(),
        llm=llm,
        vcs=CapturingVCS(),
    )
    repo = SuccessfulRepo(local_path=str(repo_root))
    task = TaskContract.create(
        task_type=TaskType.FEATURE,
        goal="Implement jsonpeek",
        context=TaskContext(files_hint=["tools/jsonpeek.py"]),
        acceptance=AcceptanceCriteria(criteria=["works"], checks=[]),
    )
    task.attempt_count = 2
    task.previous_failure_reason = NackReason.ERR_NO_DIFF
    task.previous_failure_detail = "No file blocks found. Expected ```python:path headers."
    task.previous_attempt_id = "attempt-1"
    task.previous_attempt_number = 1

    changed_files, _notes = await worker._implement_task(repo, task)

    assert changed_files == ["tools/jsonpeek.py"]
    assert llm.last_prompt is not None
    assert "Previous attempt `attempt-1` failed with `ERR_NO_DIFF`." in llm.last_prompt
    assert "You MUST emit at least one fenced code block" in llm.last_prompt


def test_build_task_prompt_uses_compact_shape_for_ollama_strict_tasks() -> None:
    config = MXConfig()
    config.llm.provider = "ollama"
    config.llm.model = "mistral:7b"
    worker = Worker(config=config, worker_id="w1", queue=StubQueue(), llm=StubLLM(), vcs=CapturingVCS())
    task = TaskContract.create(
        task_type=TaskType.FEATURE,
        goal="Implement jsonpeek",
        context=TaskContext(files_hint=["tools/jsonpeek.py"], do_not_touch=["tests/test_jsonpeek.py"]),
        acceptance=AcceptanceCriteria(criteria=["works"], checks=[]),
    )

    prompt = worker._build_task_prompt(
        task=task,
        context="### Editable Target: tools/jsonpeek.py\n```py\npass\n```",
        agents_md="# Huge AGENTS\n" + ("x" * 5000),
        allowed_paths_text="- tools/jsonpeek.py",
        strict_editing_guidance="- Keep scaffold intact.",
        retry_guidance="- No prior attempts.",
    )

    assert "Task: Implement jsonpeek" in prompt
    assert "Agent Guidelines" not in prompt
    assert "OUTPUT CONTRACT:" in prompt
    assert "The final non-whitespace characters must be closing ```." in prompt
    assert len(prompt) < 4000


def test_extract_file_blocks_tolerates_missing_closing_fence() -> None:
    worker = Worker(
        config=MXConfig(),
        worker_id="w1",
        queue=StubQueue(),
        llm=StubLLM(),
        vcs=CapturingVCS(),
    )

    matches = worker._extract_file_blocks(
        "```python:tools/jsonpeek.py\n"
        "def summarize_object(payload: dict[str, object]) -> dict[str, object]:\n"
        "    keys = sorted(payload.keys())\n"
        "    return {\"key_count\": len(keys), \"keys\": keys}\n"
    )

    assert matches == [
        (
            "tools/jsonpeek.py",
            "def summarize_object(payload: dict[str, object]) -> dict[str, object]:\n"
            "    keys = sorted(payload.keys())\n"
            "    return {\"key_count\": len(keys), \"keys\": keys}",
        )
    ]


@pytest.mark.asyncio
async def test_canonicalize_changed_python_files_runs_deterministic_ruff_passes(tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    worker = Worker(
        config=MXConfig(),
        worker_id="w1",
        queue=StubQueue(),
        llm=StubLLM(),
        vcs=CapturingVCS(),
    )
    repo = SuccessfulRepo(local_path=str(repo_root))
    commands: list[str] = []

    async def fake_run_command(command: str, cwd: str, timeout: int) -> tuple[bool, str]:
        commands.append(command)
        return True, "ok"

    worker._run_command = fake_run_command  # type: ignore[method-assign]

    await worker._canonicalize_changed_python_files(
        repo,
        ["tools/textstats.py", "README.md", "tests/test_textstats.py"],
    )

    assert commands == [
        "python -m ruff check --fix tools/textstats.py tests/test_textstats.py",
        "python -m ruff format tools/textstats.py tests/test_textstats.py",
    ]


@pytest.mark.asyncio
async def test_process_task_clone_failure_nacks_for_retry(monkeypatch) -> None:
    monkeypatch.setattr(worker_service, "GitRepo", FailingCloneRepo)

    config = MXConfig()
    queue = StubQueue()
    worker = Worker(config=config, worker_id="w1", queue=queue, llm=StubLLM(), vcs=CapturingVCS())

    task = TaskContract.create(task_type=TaskType.BUGFIX, goal="Fix cloning")
    message = QueueMessage(
        id="m1",
        body=task.to_json(),
        receipt_handle="rh-1",
        receive_count=1,
        enqueued_at=datetime.now(UTC),
        attributes={},
    )

    result = await worker.process_task(message)

    assert result is False
    assert queue.nacks == [("rh-1", 60, "ERR_TASK_FAILED")]
    assert queue.acks == []
    assert queue.dead_letters == []
    assert worker.metrics.tasks_failed == 1
    assert worker.metrics.tasks_processed == 1
    assert worker._current_task is None


@pytest.mark.asyncio
async def test_process_task_local_mode_clone_ignores_default_branch(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(worker_service, "GitRepo", CloneArgCaptureRepo)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)

    config = MXConfig()
    config.github.default_branch = "feat/agent-marketplace"
    queue = StubQueue()
    worker = Worker(config=config, worker_id="w1", queue=queue, llm=StubLLM(), vcs=CapturingVCS())

    task = TaskContract.create(task_type=TaskType.BUGFIX, goal="Local clone branch handling")
    message = QueueMessage(
        id="m1",
        body=task.to_json(),
        receipt_handle="rh-1",
        receive_count=1,
        enqueued_at=datetime.now(UTC),
        attributes={},
    )

    result = await worker.process_task(message)

    assert result is False
    assert CloneArgCaptureRepo.last_clone_branch is None


@pytest.mark.asyncio
async def test_process_task_clone_failure_routes_to_dlq_after_five_attempts(monkeypatch) -> None:
    monkeypatch.setattr(worker_service, "GitRepo", FailingCloneRepo)

    config = MXConfig()
    queue = StubQueue()
    worker = Worker(config=config, worker_id="w1", queue=queue, llm=StubLLM(), vcs=CapturingVCS())

    task = TaskContract.create(
        task_type=TaskType.BUGFIX,
        goal="Fix cloning",
        constraints=TaskConstraints(retry_max=1),
    )
    message = QueueMessage(
        id="m1",
        body=task.to_json(),
        receipt_handle="rh-1",
        receive_count=5,
        enqueued_at=datetime.now(UTC),
        attributes={},
    )

    result = await worker.process_task(message)

    assert result is False
    assert queue.acks == []
    assert queue.nacks == []
    assert queue.dead_letters == [("rh-1", "ERR_TASK_FAILED")]
    assert worker.metrics.tasks_failed == 1
    assert worker.metrics.tasks_processed == 1
    assert worker._current_task is None


@pytest.mark.asyncio
async def test_settle_failure_check_fail_routes_to_dlq_early() -> None:
    config = MXConfig()
    queue = StubQueue()
    worker = Worker(config=config, worker_id="w1", queue=queue, llm=StubLLM(), vcs=CapturingVCS())

    task = TaskContract.create(task_type=TaskType.BUGFIX, goal="check fail")
    task.transition_to(TaskState.CLAIMED)
    task.attempt_id = "attempt-3"
    message = QueueMessage(
        id="m1",
        body=task.to_json(),
        receipt_handle="rh-1",
        receive_count=3,
        enqueued_at=datetime.now(UTC),
        attributes={},
    )

    await worker._settle_failure(task, message, NackReason.ERR_CHECK_FAIL)

    assert queue.acks == []
    assert queue.nacks == []
    assert queue.dead_letters == [("rh-1", "ERR_CHECK_FAIL")]


@pytest.mark.asyncio
async def test_process_task_duplicate_single_flight_nacks_queue_lease(monkeypatch) -> None:
    monkeypatch.setattr(worker_service, "GitRepo", SuccessfulRepo)

    config = MXConfig()
    queue = StubQueue()
    worker = Worker(config=config, worker_id="w1", queue=queue, llm=StubLLM(), vcs=CapturingVCS())

    task = TaskContract.create(task_type=TaskType.BUGFIX, goal="single-flight duplicate")
    message = QueueMessage(
        id="m1",
        body=task.to_json(),
        receipt_handle="rh-1",
        receive_count=1,
        enqueued_at=datetime.now(UTC),
        attributes={},
    )

    worker_service.Worker._global_inflight_task_ids.add(task.id)
    try:
        result = await worker.process_task(message)
    finally:
        worker_service.Worker._global_inflight_task_ids.discard(task.id)

    assert result is False
    assert queue.acks == []
    assert queue.dead_letters == []
    assert queue.nacks == [("rh-1", 5, "ERR_QUEUE_LEASE")]
    assert worker.metrics.tasks_failed == 1
    assert worker.metrics.tasks_processed == 1
    assert worker._current_task is None


@pytest.mark.asyncio
async def test_process_task_invalid_python_fast_fails_and_deletes_workspace(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(worker_service, "GitRepo", SuccessfulRepo)

    fixed_uuid = uuid.UUID("11111111-1111-1111-1111-111111111111")
    monkeypatch.setattr(worker_service.uuid, "uuid4", lambda: fixed_uuid)
    monkeypatch.setattr(worker_service.tempfile, "gettempdir", lambda: str(tmp_path))

    config = MXConfig()
    queue = StubQueue()
    llm = StubLLM(
        "```file:broken.py\n"
        "def broken(:\n"
        "    pass\n"
        "```"
    )
    worker = Worker(config=config, worker_id="w1", queue=queue, llm=llm, vcs=CapturingVCS())

    task = TaskContract.create(task_type=TaskType.BUGFIX, goal="Write valid python")
    message = QueueMessage(
        id="m1",
        body=task.to_json(),
        receipt_handle="rh-1",
        receive_count=1,
        enqueued_at=datetime.now(UTC),
        attributes={},
    )

    start = time.perf_counter()
    result = await worker.process_task(message)
    elapsed_ms = (time.perf_counter() - start) * 1000

    workspace = tmp_path / f"mx_worker_{fixed_uuid.hex}"
    assert result is False
    assert elapsed_ms < 800
    assert queue.acks == []
    assert queue.nacks == [("rh-1", 60, "ERR_SYNTAX")]
    assert queue.dead_letters == []
    assert not workspace.exists()
    assert worker.metrics.tasks_failed == 1
    assert worker.metrics.tasks_processed == 1


@pytest.mark.asyncio
async def test_process_task_no_diff_nacks_with_err_no_diff(monkeypatch) -> None:
    monkeypatch.setattr(worker_service, "GitRepo", SuccessfulRepo)

    config = MXConfig()
    queue = StubQueue()
    llm = StubLLM("Implementation plan: edit auth.py and models.py.")
    worker = Worker(config=config, worker_id="w1", queue=queue, llm=llm, vcs=CapturingVCS())

    task = TaskContract.create(task_type=TaskType.BUGFIX, goal="Do work")
    message = QueueMessage(
        id="m1",
        body=task.to_json(),
        receipt_handle="rh-1",
        receive_count=1,
        enqueued_at=datetime.now(UTC),
        attributes={},
    )

    result = await worker.process_task(message)

    assert result is False
    assert queue.acks == []
    assert queue.nacks == [("rh-1", 60, "ERR_NO_DIFF")]
    assert queue.dead_letters == []
    assert worker.metrics.tasks_failed == 1
    assert worker.metrics.tasks_processed == 1
    retry_task = TaskContract.from_json(message.body)
    assert retry_task.state == TaskState.QUEUED
    assert retry_task.previous_failure_reason == NackReason.ERR_NO_DIFF
    assert retry_task.previous_attempt_id == "attempt-1"
    assert retry_task.previous_attempt_number == 1
    assert retry_task.previous_failure_detail == "No file blocks produced for task " + task.id


@pytest.mark.asyncio
async def test_process_task_directory_path_block_nacks_with_err_no_diff(monkeypatch) -> None:
    monkeypatch.setattr(worker_service, "GitRepo", SuccessfulRepo)

    config = MXConfig()
    queue = StubQueue()
    llm = StubLLM("```file:planner/\nprint('bad path')\n```")
    worker = Worker(config=config, worker_id="w1", queue=queue, llm=llm, vcs=CapturingVCS())

    task = TaskContract.create(task_type=TaskType.BUGFIX, goal="Do work")
    message = QueueMessage(
        id="m1",
        body=task.to_json(),
        receipt_handle="rh-1",
        receive_count=1,
        enqueued_at=datetime.now(UTC),
        attributes={},
    )

    result = await worker.process_task(message)

    assert result is False
    assert queue.acks == []
    assert queue.nacks == [("rh-1", 60, "ERR_NO_DIFF")]
    assert queue.dead_letters == []
    assert worker.metrics.tasks_failed == 1
    assert worker.metrics.tasks_processed == 1


@pytest.mark.asyncio
async def test_process_task_refusal_nacks_with_err_refusal(monkeypatch) -> None:
    monkeypatch.setattr(worker_service, "GitRepo", SuccessfulRepo)

    config = MXConfig()
    queue = StubQueue()
    llm = StubLLM("I cannot complete this task due to insufficient information.")
    worker = Worker(config=config, worker_id="w1", queue=queue, llm=llm, vcs=CapturingVCS())

    task = TaskContract.create(task_type=TaskType.BUGFIX, goal="Do work")
    message = QueueMessage(
        id="m1",
        body=task.to_json(),
        receipt_handle="rh-1",
        receive_count=1,
        enqueued_at=datetime.now(UTC),
        attributes={},
    )

    result = await worker.process_task(message)

    assert result is False
    assert queue.acks == []
    assert queue.nacks == [("rh-1", 60, "ERR_REFUSAL")]
    assert queue.dead_letters == []
    assert worker.metrics.tasks_failed == 1
    assert worker.metrics.tasks_processed == 1


@pytest.mark.asyncio
async def test_process_task_logs_failing_acceptance_check_summary(monkeypatch, capsys) -> None:
    monkeypatch.setattr(worker_service, "GitRepo", SuccessfulRepo)

    config = MXConfig()
    queue = StubQueue()
    llm = StubLLM(
        "```file:ok.py\n"
        "def ok() -> int:\n"
        "    return 1\n"
        "```"
    )
    worker = Worker(config=config, worker_id="w1", queue=queue, llm=llm, vcs=CapturingVCS())

    task = TaskContract.create(
        task_type=TaskType.BUGFIX,
        goal="Do work",
        acceptance=AcceptanceCriteria(criteria=["should fail check"], checks=["false"]),
    )
    message = QueueMessage(
        id="m1",
        body=task.to_json(),
        receipt_handle="rh-1",
        receive_count=1,
        enqueued_at=datetime.now(UTC),
        attributes={},
    )

    result = await worker.process_task(message)
    output = capsys.readouterr().out

    assert result is False
    assert queue.acks == []
    assert queue.nacks == [("rh-1", 60, "ERR_CHECK_FAIL")]
    assert "Acceptance checks failed: false ->" in output


@pytest.mark.asyncio
async def test_process_task_preserves_non_empty_detail_for_blank_runtime_exception(monkeypatch, capsys) -> None:
    monkeypatch.setattr(worker_service, "GitRepo", SuccessfulRepo)

    config = MXConfig()
    queue = StubQueue()
    worker = Worker(
        config=config,
        worker_id="w1",
        queue=queue,
        llm=RaisingLLM(TimeoutError()),
        vcs=CapturingVCS(),
    )

    task = TaskContract.create(
        task_type=TaskType.BUGFIX,
        goal="Do work",
        acceptance=AcceptanceCriteria(criteria=["should fail"], checks=[]),
    )
    message = QueueMessage(
        id="m1",
        body=task.to_json(),
        receipt_handle="rh-1",
        receive_count=1,
        enqueued_at=datetime.now(UTC),
        attributes={},
    )

    result = await worker.process_task(message)
    output = capsys.readouterr().out
    redelivered_task = TaskContract.from_json(message.body)

    assert result is False
    assert queue.nacks == [("rh-1", 60, "ERR_TASK_FAILED")]
    assert "LLM chat timed out after" in output
    assert redelivered_task.previous_failure_detail is not None
    assert "LLM chat timed out after" in redelivered_task.previous_failure_detail


@pytest.mark.asyncio
async def test_process_task_llm_timeout_is_reported_and_nacked(monkeypatch) -> None:
    monkeypatch.setattr(worker_service, "GitRepo", SuccessfulRepo)

    config = MXConfig()
    config.llm.provider = "ollama"
    config.llm.model = "mistral:7b"
    queue = StubQueue()
    worker = Worker(
        config=config,
        worker_id="w1",
        queue=queue,
        llm=SlowLLM(),
        vcs=CapturingVCS(),
    )
    monkeypatch.setattr(worker, "_llm_chat_timeout_seconds", lambda _task: 0.01)

    task = TaskContract.create(
        task_type=TaskType.BUGFIX,
        goal="Do work",
        acceptance=AcceptanceCriteria(criteria=["should fail"], checks=[]),
    )
    message = QueueMessage(
        id="m1",
        body=task.to_json(),
        receipt_handle="rh-1",
        receive_count=1,
        enqueued_at=datetime.now(UTC),
        attributes={},
    )

    result = await worker.process_task(message)
    redelivered_task = TaskContract.from_json(message.body)

    assert result is False
    assert queue.nacks == [("rh-1", 60, "ERR_TASK_FAILED")]
    assert redelivered_task.previous_failure_detail is not None
    assert "LLM chat timed out after 0.01s" in redelivered_task.previous_failure_detail


@pytest.mark.asyncio
async def test_process_task_dead_letters_immediately_when_retry_budget_is_zero(monkeypatch) -> None:
    monkeypatch.setattr(worker_service, "GitRepo", SuccessfulRepo)

    config = MXConfig()
    config.worker.retry_max = 0
    queue = StubQueue()
    worker = Worker(
        config=config,
        worker_id="w1",
        queue=queue,
        llm=RaisingLLM(RuntimeError("boom")),
        vcs=CapturingVCS(),
    )

    task = TaskContract.create(
        task_type=TaskType.BUGFIX,
        goal="Do work",
        acceptance=AcceptanceCriteria(criteria=["should fail"], checks=[]),
    )
    message = QueueMessage(
        id="m1",
        body=task.to_json(),
        receipt_handle="rh-1",
        receive_count=1,
        enqueued_at=datetime.now(UTC),
        attributes={},
    )

    result = await worker.process_task(message)

    assert result is False
    assert queue.nacks == []
    assert queue.dead_letters == [("rh-1", "ERR_TASK_FAILED")]


def test_llm_max_tokens_for_strict_ollama_tasks() -> None:
    config = MXConfig()
    config.llm.provider = "ollama"
    config.llm.model = "mistral:7b"
    config.llm.max_tokens = 4096
    worker = Worker(config=config, worker_id="w1", queue=StubQueue(), llm=StubLLM(), vcs=CapturingVCS())

    single_file_task = TaskContract.create(
        task_type=TaskType.FEATURE,
        goal="Implement narrow task",
        context=TaskContext(files_hint=["tools/jsonpeek.py"]),
    )
    multi_file_task = TaskContract.create(
        task_type=TaskType.FEATURE,
        goal="Implement broader task",
        context=TaskContext(files_hint=["tools/jsonpeek.py", "tests/test_jsonpeek.py"]),
    )

    assert worker._llm_max_tokens_for_task(single_file_task) == 512
    assert worker._llm_max_tokens_for_task(multi_file_task) == 768


def test_llm_max_tokens_for_local_proof_mode_ollama_tasks(monkeypatch) -> None:
    monkeypatch.setenv("MX_LOCAL_MODEL_PROOF_MODE", "1")
    config = MXConfig()
    config.llm.provider = "ollama"
    config.llm.model = "mistral:7b"
    config.llm.max_tokens = 4096
    worker = Worker(config=config, worker_id="w1", queue=StubQueue(), llm=StubLLM(), vcs=CapturingVCS())

    single_file_task = TaskContract.create(
        task_type=TaskType.FEATURE,
        goal="Implement narrow task",
        context=TaskContext(files_hint=["tools/jsonpeek.py"]),
    )
    multi_file_task = TaskContract.create(
        task_type=TaskType.FEATURE,
        goal="Implement broader task",
        context=TaskContext(files_hint=["tools/jsonpeek.py", "tests/test_jsonpeek.py"]),
    )

    assert worker._llm_max_tokens_for_task(single_file_task) == 320
    assert worker._llm_max_tokens_for_task(multi_file_task) == 512


def test_llm_timeout_for_local_proof_mode_ollama_task(monkeypatch) -> None:
    monkeypatch.setenv("MX_LOCAL_MODEL_PROOF_MODE", "1")
    config = MXConfig()
    config.llm.provider = "ollama"
    config.llm.model = "mistral:7b"
    worker = Worker(config=config, worker_id="w1", queue=StubQueue(), llm=StubLLM(), vcs=CapturingVCS())

    single_file_task = TaskContract.create(
        task_type=TaskType.FEATURE,
        goal="Implement narrow task",
        context=TaskContext(files_hint=["tools/jsonpeek.py"]),
    )

    assert worker._llm_chat_timeout_seconds(single_file_task) == 420


@pytest.mark.asyncio
async def test_submit_to_vcs_uses_diff_fallback_and_maps_risk_confidence() -> None:
    config = MXConfig()
    queue = StubQueue()
    vcs = CapturingVCS()
    worker = Worker(config=config, worker_id="w1", queue=queue, llm=StubLLM(), vcs=vcs)

    task = TaskContract.create(
        task_type=TaskType.FEATURE,
        goal="Add endpoint",
        acceptance=AcceptanceCriteria(criteria=["works"], checks=[]),
        constraints=TaskConstraints(risk=RiskLevel.HIGH),
    )
    evidence = EvidencePack(
        task_id=task.id,
        task_goal=task.goal,
        commands_run=[],
        files_changed=["a.py"],
        verification_method="manual",
        known_risks=[],
    )

    result = await worker._submit_to_vcs(
        repo=DiffFallbackRepo(),
        task=task,
        evidence=evidence,
        changed_files=["a.py"],
        all_passed=True,
        draft=False,
    )

    assert result == {
        "number": "prop-123",
        "type": "proposal",
        "url": "http://example/prop-123",
        "status": "PENDING",
    }
    assert worker.metrics.prs_opened == 1

    assert len(vcs.calls) == 1
    call = vcs.calls[0]
    assert call["diff"].startswith("diff --git")
    assert call["confidence"] == 0.85
    assert call["risk_tier"] == 2
    assert call["draft"] is False
    assert call["source"] == "mx_swarm"
    assert call["source_run_id"] == task.trace_id
    assert call["source_task_id"] == task.id
    assert call["source_agent_id"] == "w1"
    assert call["source_attempt_id"] == task.attempt_id
    assert call["intent_type"] == "feature"


def test_submission_source_run_id_prefers_env_and_intent_type_maps_replan(monkeypatch) -> None:
    config = MXConfig()
    worker = Worker(config=config, worker_id="w1", queue=StubQueue(), llm=StubLLM(), vcs=CapturingVCS())
    task = TaskContract.create(
        task_type=TaskType.TEST,
        goal="Replan failing contract",
        stream=TaskStream.QA,
        routing_target=RoutingTarget.REPLAN,
    )

    monkeypatch.setenv("MX_RUN_ID", "run-proof-1")

    assert worker._submission_source_run_id(task) == "run-proof-1"
    assert worker._submission_intent_type(task) == "replan"


@pytest.mark.asyncio
async def test_process_task_success_evidence_includes_execution_trace(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(worker_service, "GitRepo", SuccessfulRepo)

    fixed_uuid = uuid.UUID("22222222-2222-2222-2222-222222222222")
    monkeypatch.setattr(worker_service.uuid, "uuid4", lambda: fixed_uuid)
    monkeypatch.setattr(worker_service.tempfile, "gettempdir", lambda: str(tmp_path))

    config = MXConfig()
    queue = StubQueue()
    vcs = CapturingVCS()
    llm = StubLLM(
        "```file:ok.py\n"
        "def ok() -> int:\n"
        "    return 1\n"
        "```"
    )
    worker = Worker(config=config, worker_id="w1", queue=queue, llm=llm, vcs=vcs)

    task = TaskContract.create(task_type=TaskType.BUGFIX, goal="Ship change")
    message = QueueMessage(
        id="m1",
        body=task.to_json(),
        receipt_handle="rh-1",
        receive_count=1,
        enqueued_at=datetime.now(UTC),
        attributes={},
    )

    result = await worker.process_task(message)

    assert result is True
    assert queue.acks == ["rh-1"]
    assert queue.nacks == []
    assert len(vcs.calls) == 1
    evidence = vcs.calls[0]["evidence_markdown"]
    assert "### Execution Trace" in evidence
    assert "`worker_id`" in evidence
    assert "`queue_message_id`" in evidence
    assert "`planned_settlement`" in evidence
    assert "`ACK`" in evidence


@pytest.mark.asyncio
async def test_process_task_remaps_unique_basename_to_allowed_target(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(worker_service, "GitRepo", SuccessfulRepo)
    monkeypatch.chdir(tmp_path)

    config = MXConfig()
    queue = StubQueue()
    vcs = CapturingVCS()
    llm = StubLLM(
        "```file:textstats.py\n"
        "def count_lines(text: str) -> int:\n"
        "    return len(text.splitlines())\n"
        "```"
    )
    worker = Worker(config=config, worker_id="w1", queue=queue, llm=llm, vcs=vcs)

    task = TaskContract.create(
        task_type=TaskType.FEATURE,
        goal="Implement textstats",
        context=TaskContext(files_hint=["tools/textstats.py"]),
        acceptance=AcceptanceCriteria(criteria=["creates file"], checks=[]),
    )
    message = QueueMessage(
        id="m1",
        body=task.to_json(),
        receipt_handle="rh-1",
        receive_count=1,
        enqueued_at=datetime.now(UTC),
        attributes={},
    )

    result = await worker.process_task(message)

    assert result is True
    assert queue.acks == ["rh-1"]
    assert queue.nacks == []
    assert len(vcs.calls) == 1
    assert vcs.calls[0]["affected_paths"] == ["tools/textstats.py"]


@pytest.mark.asyncio
async def test_process_task_emits_submission_events_for_success(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(worker_service, "GitRepo", SuccessfulRepo)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    config = MXConfig()
    queue = StubQueue()
    vcs = CapturingVCS()
    llm = StubLLM(
        "```file:tools/textstats.py\n"
        "def count_lines(text: str) -> int:\n"
        "    return len(text.splitlines())\n"
        "```"
    )
    worker = Worker(config=config, worker_id="w1", queue=queue, llm=llm, vcs=vcs)

    task = TaskContract.create(
        task_type=TaskType.FEATURE,
        goal="Implement textstats",
        context=TaskContext(files_hint=["tools/textstats.py"]),
        acceptance=AcceptanceCriteria(criteria=["creates file"], checks=[]),
    )
    message = QueueMessage(
        id="m1",
        body=task.to_json(),
        receipt_handle="rh-1",
        receive_count=1,
        enqueued_at=datetime.now(UTC),
        attributes={},
    )

    result = await worker.process_task(message)

    assert result is True
    submission_log = tmp_path / ".runtime" / "submission_events.jsonl"
    assert submission_log.exists()

    rows = [json.loads(line) for line in submission_log.read_text().splitlines() if line.strip()]
    assert [row["status"] for row in rows] == ["PENDING", "settled_ack"]
    assert rows[0]["branch_name"] == task.branch_name()
    assert rows[0]["submission_id"] == "prop-123"
    assert rows[0]["local_mode"] is True
    assert rows[0]["source"] == "mx_swarm"
    assert rows[0]["source_run_id"] == task.trace_id
    assert rows[0]["source_task_id"] == task.id
    assert rows[0]["source_agent_id"] == "w1"
    assert rows[0]["source_attempt_id"] == rows[0]["attempt_id"]
    assert rows[0]["intent_type"] == "feature"
    assert rows[1]["source"] == "mx_swarm"
    assert rows[1]["settled_at"]


@pytest.mark.asyncio
async def test_process_task_fails_closed_when_post_landing_verification_fails(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(worker_service, "GitRepo", SuccessfulRepo)
    monkeypatch.chdir(tmp_path)

    config = MXConfig()
    config.vcs.provider = "aether"
    queue = StubQueue()
    vcs = FailingLandingVCS()
    llm = StubLLM(
        "```file:tools/textstats.py\n"
        "def count_lines(text: str) -> int:\n"
        "    return len(text.splitlines())\n"
        "```"
    )
    worker = Worker(config=config, worker_id="w1", queue=queue, llm=llm, vcs=vcs)

    task = TaskContract.create(
        task_type=TaskType.FEATURE,
        goal="Implement textstats",
        context=TaskContext(files_hint=["tools/textstats.py"]),
        acceptance=AcceptanceCriteria(criteria=["creates file"], checks=[]),
    )
    message = QueueMessage(
        id="m1",
        body=task.to_json(),
        receipt_handle="rh-1",
        receive_count=1,
        enqueued_at=datetime.now(UTC),
        attributes={},
    )

    result = await worker.process_task(message)

    assert result is False
    assert queue.acks == []
    assert queue.nacks
    assert queue.nacks[0][2] == NackReason.ERR_POST_LANDING_VERIFY.value

    submission_log = tmp_path / ".runtime" / "submission_events.jsonl"
    rows = [json.loads(line) for line in submission_log.read_text().splitlines() if line.strip()]
    assert rows[-1]["status"] == "settled_nack"
    assert rows[-1]["landing_verification_status"] == "failed"
    assert "integrity mismatch" in rows[-1]["landing_verification_detail"]


@pytest.mark.asyncio
async def test_process_task_stages_only_generated_files(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(worker_service, "GitRepo", StagingCaptureRepo)
    monkeypatch.chdir(tmp_path)
    StagingCaptureRepo.last_add_paths = None

    config = MXConfig()
    queue = StubQueue()
    vcs = CapturingVCS()
    llm = StubLLM(
        "```file:tools/textstats.py\n"
        "def count_lines(text: str) -> int:\n"
        "    return len(text.splitlines())\n"
        "```"
    )
    worker = Worker(config=config, worker_id="w1", queue=queue, llm=llm, vcs=vcs)

    task = TaskContract.create(
        task_type=TaskType.FEATURE,
        goal="Implement textstats",
        context=TaskContext(
            files_hint=["tools/textstats.py"],
            do_not_touch=["tests/test_textstats.py"],
        ),
        acceptance=AcceptanceCriteria(criteria=["creates file"], checks=[]),
    )
    message = QueueMessage(
        id="m1",
        body=task.to_json(),
        receipt_handle="rh-1",
        receive_count=1,
        enqueued_at=datetime.now(UTC),
        attributes={},
    )

    result = await worker.process_task(message)

    assert result is True
    assert StagingCaptureRepo.last_add_paths == ["tools/textstats.py"]


@pytest.mark.asyncio
async def test_run_loop_stops_immediately_when_paused(monkeypatch) -> None:
    config = MXConfig()
    queue = StubQueue()
    worker = Worker(config=config, worker_id="w1", queue=queue, llm=StubLLM(), vcs=CapturingVCS())

    paused_cfg = MXConfig()
    paused_cfg.paused = True
    monkeypatch.setattr(worker_service.MXConfig, "load", lambda: paused_cfg)

    await asyncio.wait_for(worker.run_loop(), timeout=0.2)
    assert worker._running is False


@pytest.mark.asyncio
async def test_run_command_falls_back_to_python_module_when_tool_missing(monkeypatch) -> None:
    config = MXConfig()
    worker = Worker(config=config, worker_id="w1", queue=StubQueue(), llm=StubLLM(), vcs=CapturingVCS())
    captured: dict[str, str] = {}

    class DummyProc:
        returncode = 0

        async def communicate(self):
            return (b"ok", b"")

    async def fake_create_subprocess_shell(command: str, **kwargs):
        captured["command"] = command
        return DummyProc()

    monkeypatch.setattr(worker_service.shutil, "which", lambda tool: None if tool == "pytest" else "/usr/bin/ok")
    monkeypatch.setattr(worker_service.asyncio, "create_subprocess_shell", fake_create_subprocess_shell)

    ok, output = await worker._run_command("pytest tests/test_textstats.py::test_json_output", cwd=".")

    assert ok is True
    assert output == "ok"
    assert captured["command"] == f'"{sys.executable}" -m pytest tests/test_textstats.py::test_json_output'


@pytest.mark.asyncio
async def test_run_command_keeps_original_when_tool_exists(monkeypatch) -> None:
    config = MXConfig()
    worker = Worker(config=config, worker_id="w1", queue=StubQueue(), llm=StubLLM(), vcs=CapturingVCS())
    captured: dict[str, str] = {}

    class DummyProc:
        returncode = 0

        async def communicate(self):
            return (b"ok", b"")

    async def fake_create_subprocess_shell(command: str, **kwargs):
        captured["command"] = command
        return DummyProc()

    monkeypatch.setattr(worker_service.shutil, "which", lambda _tool: "/usr/bin/tool")
    monkeypatch.setattr(worker_service.asyncio, "create_subprocess_shell", fake_create_subprocess_shell)

    ok, output = await worker._run_command("ruff check .", cwd=".")

    assert ok is True
    assert output == "ok"
    assert captured["command"] == "ruff check ."


@pytest.mark.asyncio
async def test_run_command_falls_back_to_current_interpreter_when_python_missing(monkeypatch) -> None:
    config = MXConfig()
    worker = Worker(config=config, worker_id="w1", queue=StubQueue(), llm=StubLLM(), vcs=CapturingVCS())
    captured: dict[str, str] = {}

    class DummyProc:
        returncode = 0

        async def communicate(self):
            return (b"ok", b"")

    async def fake_create_subprocess_shell(command: str, **kwargs):
        captured["command"] = command
        return DummyProc()

    monkeypatch.setattr(worker_service.shutil, "which", lambda tool: None if tool == "python" else "/usr/bin/ok")
    monkeypatch.setattr(worker_service.asyncio, "create_subprocess_shell", fake_create_subprocess_shell)

    ok, output = await worker._run_command("python -m pytest tests/test_textstats.py -q", cwd=".")

    assert ok is True
    assert output == "ok"
    assert captured["command"] == f'"{sys.executable}" -m pytest tests/test_textstats.py -q'


@pytest.mark.asyncio
async def test_run_command_rewrites_python_module_ruff_to_binary(monkeypatch) -> None:
    config = MXConfig()
    worker = Worker(config=config, worker_id="w1", queue=StubQueue(), llm=StubLLM(), vcs=CapturingVCS())
    captured: dict[str, str] = {}

    class DummyProc:
        returncode = 0

        async def communicate(self):
            return (b"ok", b"")

    async def fake_create_subprocess_shell(command: str, **kwargs):
        captured["command"] = command
        return DummyProc()

    monkeypatch.setattr(worker_service.asyncio, "create_subprocess_shell", fake_create_subprocess_shell)

    ok, output = await worker._run_command("python -m ruff check tools/jsonpeek.py", cwd=".")

    expected_ruff = Path(sys.executable).parent / "ruff"
    assert ok is True
    assert output == "ok"
    assert captured["command"] == f'"{expected_ruff}" check tools/jsonpeek.py'


@pytest.mark.asyncio
async def test_implement_task_emits_model_response_received_only_after_chat_completion(
    monkeypatch, tmp_path
) -> None:
    spans: list[dict] = []

    def capture_span(**kwargs) -> None:
        spans.append(kwargs)

    monkeypatch.setattr(worker_service, "append_trace_span", capture_span)
    monkeypatch.setattr(worker_service, "append_llm_usage_event", lambda **kwargs: None)

    in_flight_operations: list[str] = []

    class GatedLLM:
        async def chat(self, *args, **kwargs):
            in_flight_operations.extend(span["operation"] for span in spans)
            await asyncio.sleep(0)
            resp = _LLMResponse("```python:tools/jsonpeek.py\nprint('ok')\n```\nImplemented.")
            resp.usage = {"input_tokens": 11, "output_tokens": 7}  # type: ignore[attr-defined]
            return resp

    repo = SuccessfulRepo(local_path=str(tmp_path / "repo"))
    worker = Worker(
        config=MXConfig(),
        worker_id="w-telemetry",
        queue=StubQueue(),
        llm=GatedLLM(),
        vcs=CapturingVCS(),
    )
    task = TaskContract.create(
        task_type=TaskType.FEATURE,
        goal="Implement jsonpeek",
        context=TaskContext(files_hint=["tools/jsonpeek.py"]),
    )

    changed_files, _notes = await worker._implement_task(repo, task)

    assert changed_files == ["tools/jsonpeek.py"]
    assert in_flight_operations == ["llm_chat"]
    assert "model_response_received" not in in_flight_operations

    operations = [(span["operation"], span.get("status")) for span in spans]
    assert operations == [
        ("llm_chat", "started"),
        ("model_response_received", None),
        ("llm_chat", "ok"),
    ]
    assert all(span["operation"] != "first_token_received" for span in spans)
    response_span = spans[1]
    assert response_span["attributes"]["synthetic"] is False
    assert response_span["attributes"]["response_chars"] > 0
    assert worker.metrics.total_tokens == 18


@pytest.mark.asyncio
async def test_process_task_llm_timeout_never_emits_model_response_received(monkeypatch) -> None:
    spans: list[dict] = []

    def capture_span(**kwargs) -> None:
        spans.append(kwargs)

    monkeypatch.setattr(worker_service, "GitRepo", SuccessfulRepo)
    monkeypatch.setattr(worker_service, "append_trace_span", capture_span)

    config = MXConfig()
    config.llm.provider = "ollama"
    config.llm.model = "mistral:7b"
    worker = Worker(
        config=config,
        worker_id="w-timeout",
        queue=StubQueue(),
        llm=SlowLLM(),
        vcs=CapturingVCS(),
    )
    monkeypatch.setattr(worker, "_llm_chat_timeout_seconds", lambda _task: 0.01)

    task = TaskContract.create(
        task_type=TaskType.BUGFIX,
        goal="Timeout check",
        acceptance=AcceptanceCriteria(criteria=["should timeout"], checks=[]),
    )
    message = QueueMessage(
        id="m-timeout",
        body=task.to_json(),
        receipt_handle="rh-timeout",
        receive_count=1,
        enqueued_at=datetime.now(UTC),
        attributes={},
    )

    result = await worker.process_task(message)

    assert result is False
    emitted_ops = [span["operation"] for span in spans]
    assert "model_timeout" in emitted_ops
    assert "model_response_received" not in emitted_ops
    assert "first_token_received" not in emitted_ops


@pytest.mark.asyncio
async def test_implement_task_canned_completion_marks_model_response_received_synthetic(
    monkeypatch, tmp_path
) -> None:
    spans: list[dict] = []

    def capture_span(**kwargs) -> None:
        spans.append(kwargs)

    monkeypatch.setattr(worker_service, "append_trace_span", capture_span)
    monkeypatch.setattr(worker_service, "append_llm_usage_event", lambda **kwargs: None)

    fixture_path = tmp_path / "canned.json"
    fixture_path.write_text(
        json.dumps(
            {
                "note": "synthetic worker proof",
                "files": {"tools/jsonpeek.py": "print('ok')\n"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MX_CANNED_COMPLETION_FIXTURE", str(fixture_path))

    repo = SuccessfulRepo(local_path=str(tmp_path / "repo"))
    worker = Worker(
        config=MXConfig(),
        worker_id="w-canned",
        queue=StubQueue(),
        llm=RaisingLLM(AssertionError("chat() must not be called for canned completion")),
        vcs=CapturingVCS(),
    )
    task = TaskContract.create(
        task_type=TaskType.FEATURE,
        goal="Implement jsonpeek",
        context=TaskContext(files_hint=["tools/jsonpeek.py"]),
    )

    changed_files, _notes = await worker._implement_task(repo, task)

    assert changed_files == ["tools/jsonpeek.py"]
    assert all(span["operation"] != "first_token_received" for span in spans)
    canned_spans = [span for span in spans if span["operation"] == "model_response_received"]
    assert len(canned_spans) == 1
    assert canned_spans[0]["attributes"]["synthetic"] is True

