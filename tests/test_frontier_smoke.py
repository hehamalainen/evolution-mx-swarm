import json
from pathlib import Path

import pytest

from scripts import run_gate2_local_parallel
from shared.schemas import (
    AcceptanceCriteria,
    ModelTier,
    RoutingTarget,
    TaskContext,
    TaskContract,
    TaskType,
)


def test_prepare_run_config_writes_frontier_override(tmp_path: Path) -> None:
    config_path = tmp_path / "mx.config.json"
    config_path.write_text(json.dumps({"worker": {}, "github": {}, "secrets": {"env": {}}}))

    run_gate2_local_parallel._prepare_run_config(
        tmp_path,
        default_branch="main",
        worker_count=1,
        frontier_llm={"provider": "openai", "model": "gpt-4o-mini"},
    )

    payload = json.loads(config_path.read_text())
    assert payload["frontier_llm"] == {"provider": "openai", "model": "gpt-4o-mini"}


class _FakePlanner:
    created_tasks: list[TaskContract] = []

    def __init__(self, config, queue, repo_path, prd_path):
        self.repo_path = repo_path
        self.prd_path = prd_path

    def _read_prd(self):
        return "# prd"

    def _build_constrained_local_requirement(self, prd):
        return prd

    def _build_constrained_local_task(self, requirement, prd):
        task = TaskContract.create(
            task_type=TaskType.FEATURE,
            goal="Implement smoke slice",
            context=TaskContext(files_hint=["tools/jsonpeek.py"]),
            acceptance=AcceptanceCriteria(criteria=["works"], checks=[]),
        )
        self.created_tasks.append(task)
        return task

    async def publish_tasks(self, tasks):
        return len(tasks)


@pytest.mark.asyncio
async def test_publish_constrained_tasks_can_promote_to_frontier(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(run_gate2_local_parallel, "Planner", _FakePlanner)
    _FakePlanner.created_tasks.clear()

    task_map = await run_gate2_local_parallel._publish_constrained_tasks(
        run_dir=tmp_path,
        config=None,
        queue=None,
        slices=[
            run_gate2_local_parallel.SliceSpec(
                key="jsonpeek",
                prd_path="prds/golden_path_002_jsonpeek.md",
                implementation_path="tools/jsonpeek.py",
                test_paths=["tests/test_jsonpeek.py"],
            )
        ],
        model_tier=ModelTier.FRONTIER,
        routing_target=RoutingTarget.FRONTIER,
        routing_reason="frontier_smoke",
    )

    assert len(task_map) == 1
    assert _FakePlanner.created_tasks
    assert _FakePlanner.created_tasks[0].model_tier == ModelTier.FRONTIER
    assert _FakePlanner.created_tasks[0].routing_target == RoutingTarget.FRONTIER
    assert _FakePlanner.created_tasks[0].routing_reason == "frontier_smoke"
