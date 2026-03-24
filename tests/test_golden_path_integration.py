import json
import os
import uuid

import pytest

import worker.service as worker_service
from planner import Planner
from shared.adapters.queue import InMemoryQueue
from shared.adapters.vcs_adapter import LandingVerificationResult, SubmitResult
from shared.schemas import RequirementStatus, Spec, TaskType
from shared.utils.config import MXConfig
from shared.utils.git import GitResult
from shared.utils.task_registry import load_task_snapshot
from worker.service import Worker


class _LLMResponse:
    def __init__(self, content: str):
        self.content = content


class PlannerLLM:
    async def chat(self, *args, **kwargs):
        messages = kwargs.get("messages") or []
        prompt = messages[0].content if messages else ""

        if "Extract requirements from this PRD as JSON." in prompt:
            return _LLMResponse(
                """
[
  {
    "type": "functional",
    "title": "Golden Path Requirement",
    "description": "Produce one deterministic implementation task.",
    "acceptance_criteria": ["Worker can produce a code artifact"],
    "priority": 6
  }
]
""".strip()
            )

        if "Create implementation tasks for this requirement." in prompt:
            return _LLMResponse(
                """
[
  {
    "type": "feature",
    "goal": "Create deterministic worker artifact",
    "files_hint": ["app/feature.py"],
    "do_not_touch": [],
    "checks": [],
    "criteria": ["Feature module exists"],
    "timebox_minutes": 30,
    "risk": "low",
    "merge_policy": "manual"
  }
]
""".strip()
            )

        raise AssertionError(f"Unexpected planner prompt: {prompt[:120]}")


class PlannerUnknownTaskTypeLLM(PlannerLLM):
    async def chat(self, *args, **kwargs):
        messages = kwargs.get("messages") or []
        prompt = messages[0].content if messages else ""

        if "Create implementation tasks for this requirement." in prompt:
            return _LLMResponse(
                """
[
  {
    "type": "task",
    "goal": "Create deterministic worker artifact",
    "files_hint": ["app/feature.py"],
    "do_not_touch": [],
    "checks": [],
    "criteria": ["Feature module exists"],
    "timebox_minutes": 30,
    "risk": "med",
    "merge_policy": "auto"
  }
]
""".strip()
            )

        return await super().chat(*args, **kwargs)


class PlannerInvalidChecksLLM(PlannerLLM):
    async def chat(self, *args, **kwargs):
        messages = kwargs.get("messages") or []
        prompt = messages[0].content if messages else ""

        if "Create implementation tasks for this requirement." in prompt:
            return _LLMResponse(
                """
[
  {
    "type": "feature",
    "goal": "Create deterministic worker artifact",
    "files_hint": ["cli/textstats.py"],
    "do_not_touch": [],
    "checks": [
      "pytest tests/test_missing.py::test_unknown",
      "pytest tests/test_textstats.py::test_json_output",
      "ruff check .",
      "echo this should be ignored"
    ],
    "criteria": ["Feature module exists"],
    "timebox_minutes": 30,
    "risk": "low",
    "merge_policy": "manual"
  }
]
""".strip()
            )

        return await super().chat(*args, **kwargs)


class PlannerDependencyLLM(PlannerLLM):
    async def chat(self, *args, **kwargs):
        messages = kwargs.get("messages") or []
        prompt = messages[0].content if messages else ""

        if "Extract requirements from this PRD as JSON." in prompt:
            return _LLMResponse(
                """
[
  {
    "type": "functional",
    "title": "Base Requirement",
    "description": "Produce the base artifact first.",
    "acceptance_criteria": ["Base artifact exists"],
    "priority": 7
  },
  {
    "type": "functional",
    "title": "Dependent Requirement",
    "description": "Only run after base requirement finishes.",
    "acceptance_criteria": ["Dependent artifact exists"],
    "priority": 6
  }
]
""".strip()
            )

        if "Create implementation tasks for this requirement." in prompt:
            if "Base Requirement" in prompt:
                return _LLMResponse(
                    """
[
  {
    "type": "feature",
    "goal": "Create base artifact",
    "files_hint": ["app/base.py"],
    "do_not_touch": [],
    "checks": [],
    "criteria": ["Base module exists"],
    "timebox_minutes": 30,
    "risk": "low",
    "merge_policy": "manual"
  }
]
""".strip()
                )
            return _LLMResponse(
                """
[
  {
    "type": "feature",
    "goal": "Create dependent artifact",
    "files_hint": ["app/dependent.py"],
    "do_not_touch": [],
    "checks": [],
    "criteria": ["Dependent module exists"],
    "timebox_minutes": 30,
    "risk": "low",
    "merge_policy": "manual"
  }
]
""".strip()
            )

        raise AssertionError(f"Unexpected planner prompt: {prompt[:120]}")


class FailingPlannerLLM:
    async def chat(self, *args, **kwargs):
        raise AssertionError("Constrained local planning should not call the LLM")


class WorkerLLM:
    async def chat(self, *args, **kwargs):
        return _LLMResponse(
            "```file:app/feature.py\n"
            "def generated_feature() -> str:\n"
            "    return \"mx-golden-path\"\n"
            "```\n\n"
            "Implemented deterministic golden-path artifact."
        )


class SyntaxErrorWorkerLLM:
    async def chat(self, *args, **kwargs):
        return _LLMResponse(
            "```file:app/feature.py\n"
            "def broken(:\n"
            "    return 1\n"
            "```\n\n"
            "Attempted implementation."
        )


class CapturingVCS:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def submit_change(self, **kwargs):
        self.calls.append(kwargs)
        return SubmitResult(
            success=True,
            submission_type="proposal",
            submission_id="prop-golden",
            status="PENDING",
            message="ok",
            url="http://example/proposal/prop-golden",
        )

    async def get_status(self, submission_id: str):
        return {"status": "accepted", "submission_id": submission_id}

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


class GoldenPathRepo:
    def __init__(self, *args, **kwargs) -> None:
        self.local_path = kwargs.get("local_path") or "."

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
        return GitResult(
            True,
            "diff --git a/app/feature.py b/app/feature.py\n+def generated_feature() -> str:\n+    return \"mx-golden-path\"\n",
            "",
            0,
        )

    async def get_diff(self, staged: bool = False) -> GitResult:
        return await self.diff("HEAD~1", "HEAD")

    def cleanup(self) -> None:
        return None


@pytest.mark.asyncio
async def test_prd_to_worker_submission_golden_path(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(worker_service, "GitRepo", GoldenPathRepo)

    (tmp_path / "PRD.md").write_text(
        "# PRD\n\n"
        "Build a deterministic golden-path run for the swarm.\n"
    )
    (tmp_path / "AGENTS.md").write_text(
        "# Agent Guidelines\n- Keep changes minimal and testable.\n"
    )

    config = MXConfig()
    config.vcs.provider = "aether"

    queue = InMemoryQueue(f"mx-golden-{uuid.uuid4().hex}")
    await queue.purge()

    planner = Planner(config=config, queue=queue, llm=PlannerLLM(), repo_path=str(tmp_path))
    planning = await planner.run(force_replan=True)

    assert planning["success"] is True
    assert planning["tasks_generated"] == 1
    assert planning["tasks_published"] == 1
    assert await queue.get_queue_depth() == 1

    spec = Spec.load(str(tmp_path / "SPEC.json"))
    assert len(spec.requirements) == 1
    assert spec.requirements[0].status == RequirementStatus.IN_PROGRESS
    assert len(spec.requirements[0].assigned_tasks) == 1

    vcs = CapturingVCS()
    worker = Worker(
        config=config,
        worker_id="worker-golden",
        queue=queue,
        llm=WorkerLLM(),
        vcs=vcs,
    )

    await worker.initialize()
    try:
        processed = await worker.run_once()
    finally:
        await worker.shutdown()

    assert processed is True
    assert worker.metrics.tasks_succeeded == 1
    assert worker.metrics.tasks_failed == 0
    assert worker.metrics.tasks_processed == 1
    assert await queue.get_queue_depth() == 0
    assert await queue.get_dead_letter_depth() == 0

    assert len(vcs.calls) == 1
    submission = vcs.calls[0]
    assert submission["affected_paths"] == ["app/feature.py"]
    assert "mx-golden-path" in submission["diff"]
    assert "### Execution Trace" in submission["evidence_markdown"]
    assert "`planned_settlement`" in submission["evidence_markdown"]


@pytest.mark.asyncio
async def test_failure_lane_retries_to_dlq_without_ack(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(worker_service, "GitRepo", GoldenPathRepo)

    (tmp_path / "PRD.md").write_text(
        "# PRD\n\n"
        "Build a deterministic failure-lane run for retry settlement.\n"
    )
    (tmp_path / "AGENTS.md").write_text(
        "# Agent Guidelines\n- Keep changes minimal and testable.\n"
    )

    config = MXConfig()
    config.vcs.provider = "aether"

    queue = InMemoryQueue(f"mx-failure-{uuid.uuid4().hex}")
    await queue.purge()

    planner = Planner(config=config, queue=queue, llm=PlannerLLM(), repo_path=str(tmp_path))
    planning = await planner.run(force_replan=True)
    assert planning["tasks_published"] == 1
    assert await queue.get_queue_depth() == 1

    ack_calls: list[str] = []
    nack_calls: list[tuple[str, int, str | None]] = []
    dlq_calls: list[tuple[str, str]] = []

    orig_ack = queue.ack
    orig_nack = queue.nack
    orig_dead_letter = queue.dead_letter

    async def _record_ack(receipt_handle: str) -> None:
        ack_calls.append(receipt_handle)
        await orig_ack(receipt_handle)

    async def _record_nack(
        receipt_handle: str,
        reason: str,
        delay: int = 0,
    ) -> None:
        nack_calls.append((receipt_handle, delay, reason))
        await orig_nack(receipt_handle, reason=reason, delay=delay)

    async def _record_dead_letter(receipt_handle: str, reason: str) -> None:
        dlq_calls.append((receipt_handle, reason))
        await orig_dead_letter(receipt_handle, reason=reason)

    monkeypatch.setattr(queue, "ack", _record_ack)
    monkeypatch.setattr(queue, "nack", _record_nack)
    monkeypatch.setattr(queue, "dead_letter", _record_dead_letter)

    vcs = CapturingVCS()
    worker = Worker(
        config=config,
        worker_id="worker-failure",
        queue=queue,
        llm=SyntaxErrorWorkerLLM(),
        vcs=vcs,
    )

    await worker.initialize()
    try:
        attempts = 0
        while await queue.get_queue_depth() == 1:
            attempts += 1
            assert await queue.get_queue_depth() == 1
            processed = await worker.run_once()
            assert processed is False
    finally:
        await worker.shutdown()

    assert await queue.get_dead_letter_depth() == 1
    assert ack_calls == []
    assert attempts >= 4
    assert len(nack_calls) == attempts - 1
    assert all(reason == "ERR_SYNTAX" for _, _, reason in nack_calls)
    assert len(dlq_calls) == 1
    assert dlq_calls[0][1] == "ERR_SYNTAX"
    assert worker.metrics.tasks_succeeded == 0
    assert worker.metrics.tasks_failed == attempts
    assert worker.metrics.tasks_processed == attempts
    assert len(vcs.calls) == 0


@pytest.mark.asyncio
async def test_planner_normalizes_unknown_task_type(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "PRD.md").write_text("# PRD\n\nGenerate one implementation task.\n")
    (tmp_path / "AGENTS.md").write_text("# Agent Guidelines\n- Keep changes minimal.\n")

    config = MXConfig()
    queue = InMemoryQueue(f"mx-normalize-{uuid.uuid4().hex}")
    await queue.purge()

    planner = Planner(
        config=config,
        queue=queue,
        llm=PlannerUnknownTaskTypeLLM(),
        repo_path=str(tmp_path),
    )
    planning = await planner.run(force_replan=True)

    assert planning["success"] is True
    assert planning["tasks_published"] == 1
    messages = await queue.receive(max_messages=1)
    assert len(messages) == 1
    payload = json.loads(messages[0].body)
    assert payload["type"] == TaskType.FEATURE.value


@pytest.mark.asyncio
async def test_planner_sanitizes_checks_to_existing_targets(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "PRD.md").write_text("# PRD\n\nGenerate one implementation task.\n")
    (tmp_path / "AGENTS.md").write_text("# Agent Guidelines\n- Keep changes minimal.\n")
    (tmp_path / "cli").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cli" / "textstats.py").write_text("def main():\n    return 0\n")
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "test_textstats.py").write_text("def test_json_output():\n    assert True\n")

    config = MXConfig()
    queue = InMemoryQueue(f"mx-checks-{uuid.uuid4().hex}")
    await queue.purge()

    planner = Planner(
        config=config,
        queue=queue,
        llm=PlannerInvalidChecksLLM(),
        repo_path=str(tmp_path),
    )
    planning = await planner.run(force_replan=True)

    assert planning["success"] is True
    messages = await queue.receive(max_messages=1)
    payload = json.loads(messages[0].body)
    checks = payload["acceptance"]["checks"]
    assert checks == [
        "python -m pytest tests/test_textstats.py::test_json_output -q",
        "python -m ruff check cli/textstats.py",
    ]


@pytest.mark.asyncio
async def test_planner_uses_constrained_local_mode_for_strict_targets(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "PRD.md").write_text(
        "# Textstats CLI\n\n"
        "Build a small CLI tool called `textstats`.\n\n"
        "## Strict File Targets\n\n"
        "- tools/textstats.py\n"
        "- tests/test_textstats.py\n\n"
        "## Acceptance Criteria\n\n"
        "1. pytest -q tests/test_textstats.py passes.\n"
        "2. Missing file exits non-zero and prints an error message.\n"
    )
    (tmp_path / "AGENTS.md").write_text("# Agent Guidelines\n- Keep changes minimal.\n")

    config = MXConfig()
    config.llm.provider = "ollama"
    config.llm.model = "mistral:7b"

    queue = InMemoryQueue(f"mx-constrained-{uuid.uuid4().hex}")
    await queue.purge()

    planner = Planner(
        config=config,
        queue=queue,
        llm=FailingPlannerLLM(),
        repo_path=str(tmp_path),
    )
    planning = await planner.run(force_replan=True)

    assert planning["success"] is True
    assert planning["spec_requirements"] == 1
    assert planning["tasks_generated"] == 1
    assert planning["tasks_published"] == 1

    spec = Spec.load(str(tmp_path / "SPEC.json"))
    assert len(spec.requirements) == 1
    assert spec.requirements[0].title == "Textstats CLI"

    messages = await queue.receive(max_messages=1)
    assert len(messages) == 1
    payload = json.loads(messages[0].body)
    assert payload["context"]["files_hint"] == ["tools/textstats.py", "tests/test_textstats.py"]
    assert payload["context"]["do_not_touch"] == []
    assert payload["acceptance"]["checks"] == [
        "python -m pytest tests/test_textstats.py -q",
        "python -m ruff check tools/textstats.py tests/test_textstats.py",
    ]


@pytest.mark.asyncio
async def test_planner_strips_markdown_wrapped_strict_targets(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "PRD.md").write_text(
        "# Textstats CLI\n\n"
        "Build a small CLI tool called `textstats`.\n\n"
        "## Strict File Targets\n\n"
        "- `tools/textstats.py`\n"
        "- `tests/test_textstats.py`\n\n"
        "## Acceptance Criteria\n\n"
        "1. `pytest -q tests/test_textstats.py` passes.\n"
    )
    (tmp_path / "AGENTS.md").write_text("# Agent Guidelines\n- Keep changes minimal.\n")

    config = MXConfig()
    config.llm.provider = "ollama"
    config.llm.model = "mistral:7b"

    queue = InMemoryQueue(f"mx-constrained-md-{uuid.uuid4().hex}")
    await queue.purge()

    planner = Planner(
        config=config,
        queue=queue,
        llm=FailingPlannerLLM(),
        repo_path=str(tmp_path),
    )
    planning = await planner.run(force_replan=True)

    assert planning["success"] is True
    messages = await queue.receive(max_messages=1)
    payload = json.loads(messages[0].body)
    assert payload["context"]["files_hint"] == ["tools/textstats.py", "tests/test_textstats.py"]
    assert payload["context"]["do_not_touch"] == []


@pytest.mark.asyncio
async def test_planner_locks_existing_test_targets_in_constrained_local_mode(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "PRD.md").write_text(
        "# Textstats CLI\n\n"
        "Build a small CLI tool called `textstats`.\n\n"
        "## Strict File Targets\n\n"
        "- tools/textstats.py\n"
        "- tests/test_textstats.py\n\n"
        "## Acceptance Criteria\n\n"
        "1. `pytest -q tests/test_textstats.py` passes.\n"
    )
    (tmp_path / "AGENTS.md").write_text("# Agent Guidelines\n- Keep changes minimal.\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_textstats.py").write_text(
        "def test_placeholder() -> None:\n"
        "    assert True\n"
    )

    config = MXConfig()
    config.llm.provider = "ollama"
    config.llm.model = "mistral:7b"

    queue = InMemoryQueue(f"mx-constrained-existing-tests-{uuid.uuid4().hex}")
    await queue.purge()

    planner = Planner(
        config=config,
        queue=queue,
        llm=FailingPlannerLLM(),
        repo_path=str(tmp_path),
    )
    planning = await planner.run(force_replan=True)

    assert planning["success"] is True
    messages = await queue.receive(max_messages=1)
    payload = json.loads(messages[0].body)
    assert payload["context"]["files_hint"] == ["tools/textstats.py"]
    assert payload["context"]["do_not_touch"] == ["tests/test_textstats.py"]
    assert payload["acceptance"]["checks"] == [
        "python -m pytest tests/test_textstats.py -q",
        "python -m ruff check tools/textstats.py tests/test_textstats.py",
    ]


@pytest.mark.asyncio
async def test_planner_reads_explicit_prd_path_for_constrained_local_mode(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "PRD.md").write_text("# Root PRD\n\nThis should be ignored.\n")
    (tmp_path / "prds").mkdir()
    (tmp_path / "prds" / "jsonpeek.md").write_text(
        "# Jsonpeek CLI\n\n"
        "Inspect top-level keys from a JSON object.\n\n"
        "## Strict File Targets\n\n"
        "- tools/jsonpeek.py\n"
        "- tests/test_jsonpeek.py\n\n"
        "## Acceptance Criteria\n\n"
        "1. `pytest -q tests/test_jsonpeek.py` passes.\n"
    )
    (tmp_path / "AGENTS.md").write_text("# Agent Guidelines\n- Keep changes minimal.\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_jsonpeek.py").write_text(
        "def test_placeholder() -> None:\n"
        "    assert True\n"
    )

    config = MXConfig()
    config.llm.provider = "ollama"
    config.llm.model = "mistral:7b"

    queue = InMemoryQueue(f"mx-constrained-prd-path-{uuid.uuid4().hex}")
    await queue.purge()

    planner = Planner(
        config=config,
        queue=queue,
        llm=FailingPlannerLLM(),
        repo_path=str(tmp_path),
        prd_path="prds/jsonpeek.md",
    )
    planning = await planner.run(force_replan=True)

    assert planning["success"] is True
    spec = Spec.load(str(tmp_path / "SPEC.json"))
    assert spec.requirements[0].title == "Jsonpeek CLI"

    messages = await queue.receive(max_messages=1)
    payload = json.loads(messages[0].body)
    assert payload["context"]["files_hint"] == ["tools/jsonpeek.py"]
    assert payload["context"]["do_not_touch"] == ["tests/test_jsonpeek.py"]


@pytest.mark.asyncio
async def test_planner_assigns_prerequisite_task_ids_from_requirement_dependencies(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "PRD.md").write_text("# PRD\n\nBuild dependent features.\n")
    (tmp_path / "AGENTS.md").write_text("# Agent Guidelines\n- Keep changes minimal.\n")

    config = MXConfig()
    queue = InMemoryQueue(f"mx-dependencies-{uuid.uuid4().hex}", repo_path=str(tmp_path))
    await queue.purge()

    planner = Planner(
        config=config,
        queue=queue,
        llm=PlannerDependencyLLM(),
        repo_path=str(tmp_path),
    )

    async def _inject_dependencies(requirements):
        requirements[1].dependencies = [requirements[0].id]
        return requirements

    monkeypatch.setattr(planner, "_identify_dependencies", _inject_dependencies)

    planning = await planner.run(force_replan=True)

    assert planning["success"] is True
    assert planning["tasks_generated"] == 1
    assert planning["tasks_published"] == 1

    spec = Spec.load(str(tmp_path / "SPEC.json"))
    base_req, dependent_req = spec.requirements
    assert base_req.status == RequirementStatus.IN_PROGRESS
    assert dependent_req.status == RequirementStatus.PENDING
    assert len(base_req.assigned_tasks) == 1

    base_task = load_task_snapshot(base_req.assigned_tasks[0], repo_path=str(tmp_path))
    assert base_task is not None
    assert base_task.prerequisite_requirement_ids == []
    assert base_task.prerequisite_task_ids == []

    dependent_tasks = await planner.generate_tasks(spec, requirements_filter=[dependent_req.id])
    assert len(dependent_tasks) == 1
    assert dependent_tasks[0].prerequisite_requirement_ids == [base_req.id]
    assert dependent_tasks[0].prerequisite_task_ids == base_req.assigned_tasks
