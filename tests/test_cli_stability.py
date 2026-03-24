import asyncio
import importlib
from types import SimpleNamespace

import pytest

from shared.adapters.aether_vcs import AetherVCS
from shared.schemas import Requirement, RequirementStatus, RequirementType, Spec
from shared.utils.config import MXConfig

cli_main = importlib.import_module("cli.main")


@pytest.mark.asyncio
async def test_sync_spec_with_aether_promotes_and_resets(monkeypatch) -> None:
    config = MXConfig()
    config.vcs.provider = "aether"
    config.vcs.aether_repo_id = "repo-1"
    config.vcs.aether_agent_id = "agent-1"

    spec = Spec.create(prd_hash="hash")

    promoted_req = Requirement.create(
        req_type=RequirementType.FUNCTIONAL,
        title="Promoted",
        description="d",
        acceptance_criteria=["x"],
    )
    promoted_req.status = RequirementStatus.IN_PROGRESS
    promoted_req.assigned_tasks = ["task-promoted"]

    retry_req = Requirement.create(
        req_type=RequirementType.FUNCTIONAL,
        title="Retry",
        description="d",
        acceptance_criteria=["x"],
    )
    retry_req.status = RequirementStatus.IN_PROGRESS
    retry_req.assigned_tasks = ["task-retry"]

    spec.requirements = [promoted_req, retry_req]

    vcs = AetherVCS(base_url="http://localhost:8100", repo_id="repo-1")

    async def fake_get_agent_proposals(agent_id: str):
        assert agent_id == "agent-1"
        return [
            {"task_id": "task-promoted", "status": "PROMOTED"},
            {"task_id": "task-retry", "status": "PENDING"},
        ]

    vcs.get_agent_proposals = fake_get_agent_proposals  # type: ignore[assignment]
    monkeypatch.setattr(cli_main, "create_vcs_adapter", lambda _cfg: vcs)

    await cli_main.sync_spec_with_aether(spec, config)

    assert promoted_req.status == RequirementStatus.COMPLETED
    assert "Aether Proposal Promoted" in promoted_req.completion_evidence

    assert retry_req.status == RequirementStatus.PENDING
    assert retry_req.assigned_tasks == []


@pytest.mark.asyncio
async def test_cmd_run_exits_when_prd_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_main, "print_banner", lambda: None)
    monkeypatch.setattr(cli_main, "load_config", lambda _path: MXConfig())

    args = SimpleNamespace(config="mx.config.json", dashboard_url=None, workers=None)

    with pytest.raises(SystemExit) as exc_info:
        await cli_main.cmd_run(args)

    assert exc_info.value.code == 1


@pytest.mark.asyncio
async def test_cmd_plan_fails_fast_on_llm_preflight_error(monkeypatch) -> None:
    monkeypatch.setattr(cli_main, "load_config", lambda _path: MXConfig())

    async def fake_llm_preflight(_config: MXConfig) -> tuple[bool, str]:
        return False, "backend unreachable"

    monkeypatch.setattr(cli_main, "llm_preflight", fake_llm_preflight)

    args = SimpleNamespace(config="mx.config.json", force=False)
    with pytest.raises(RuntimeError, match="LLM preflight failed: backend unreachable"):
        await cli_main.cmd_plan(args)


@pytest.mark.asyncio
async def test_cmd_run_exits_when_judge_finishes_without_signal(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "PRD.md").write_text("# PRD\n")

    monkeypatch.setattr(cli_main, "print_banner", lambda: None)

    async def fake_llm_preflight(_config: MXConfig) -> tuple[bool, str]:
        return True, ""

    monkeypatch.setattr(cli_main, "llm_preflight", fake_llm_preflight)

    class StubPlanner:
        def __init__(self, config: MXConfig, repo_path: str = "."):
            self.config = config
            self.repo_path = repo_path

        async def initialize(self) -> None:
            return None

        async def shutdown(self) -> None:
            return None

        async def generate_spec(self):
            return SimpleNamespace(requirements=[])

        async def generate_tasks(self, _spec):
            return []

        async def publish_tasks(self, _tasks):
            return 0

    class StubWorker:
        instances: list["StubWorker"] = []

        def __init__(self, config: MXConfig, worker_id: str = "worker-0"):
            self.config = config
            self.worker_id = worker_id
            self.metrics = SimpleNamespace(tasks_succeeded=0, tasks_failed=0, tasks_processed=0)
            self._current_task = None
            self.shutdown_called = False
            StubWorker.instances.append(self)

        async def initialize(self) -> None:
            return None

        async def run_loop(self) -> None:
            await asyncio.sleep(3600)

        async def shutdown(self) -> None:
            self.shutdown_called = True

    class StubJudge:
        def __init__(self, config: MXConfig, repo_path: str = "."):
            self.config = config
            self.repo_path = repo_path

        async def initialize(self) -> None:
            return None

        async def run_loop(self) -> None:
            cfg = MXConfig.load("mx.config.json")
            cfg.paused = True
            cfg.save("mx.config.json")

        async def shutdown(self) -> None:
            return None

    monkeypatch.setattr(cli_main, "Planner", StubPlanner)
    monkeypatch.setattr(cli_main, "Worker", StubWorker)
    monkeypatch.setattr(cli_main, "Judge", StubJudge)

    args = SimpleNamespace(config="mx.config.json", dashboard_url=None, workers=1)

    await asyncio.wait_for(cli_main.cmd_run(args), timeout=2)

    assert StubWorker.instances
    assert all(w.shutdown_called for w in StubWorker.instances)


def test_model_safe_worker_cap_for_ollama_7b() -> None:
    config = MXConfig()
    config.worker.max_workers = 10
    config.llm.provider = "ollama"
    config.llm.model = "mistral:7b"

    assert cli_main._model_safe_worker_cap(config) == 1


def test_resolve_effective_workers_clamps_to_task_supply_and_model_cap() -> None:
    effective = cli_main._resolve_effective_workers(
        requested_workers=10,
        model_cap=1,
        published_tasks=2,
        queue_depth=8,
    )
    assert effective == 1


def test_reset_stale_in_progress_requirements() -> None:
    spec = Spec.create(prd_hash="hash")
    stale = Requirement.create(
        req_type=RequirementType.FUNCTIONAL,
        title="Stale",
        description="d",
        acceptance_criteria=["x"],
    )
    stale.status = RequirementStatus.IN_PROGRESS
    stale.assigned_tasks = ["task-1"]

    clean = Requirement.create(
        req_type=RequirementType.FUNCTIONAL,
        title="Clean",
        description="d",
        acceptance_criteria=["x"],
    )
    clean.status = RequirementStatus.PENDING

    spec.requirements = [stale, clean]
    reset_count = cli_main._reset_stale_in_progress_requirements(spec)

    assert reset_count == 1
    assert stale.status == RequirementStatus.PENDING
    assert stale.assigned_tasks == []
    assert any("queue empty at startup" in item for item in stale.completion_evidence)
    assert clean.status == RequirementStatus.PENDING


@pytest.mark.asyncio
async def test_cmd_qualify_delegates_to_runner(monkeypatch) -> None:
    calls: list[tuple[str, list[str]]] = []

    async def fake_run_python_tool(script_name: str, extra_args: list[str]) -> None:
        calls.append((script_name, extra_args))

    monkeypatch.setattr(cli_main, "_run_python_tool", fake_run_python_tool)

    args = SimpleNamespace(
        corpus="qualification/corpora/gate2_local_open_weight.json",
        runs=1,
        workers=5,
        timeout_seconds=600,
        model_label="ollama/mistral:7b",
    )

    await cli_main.cmd_qualify(args)

    assert calls == [
        (
            "run_model_qualification.py",
            [
                "--model-label",
                "ollama/mistral:7b",
                "--corpus",
                "qualification/corpora/gate2_local_open_weight.json",
                "--runs",
                "1",
                "--workers",
                "5",
                "--timeout-seconds",
                "600",
            ],
        )
    ]


@pytest.mark.asyncio
async def test_cmd_proof_delegates_to_runner(monkeypatch) -> None:
    calls: list[tuple[str, list[str]]] = []

    async def fake_run_python_tool(script_name: str, extra_args: list[str]) -> None:
        calls.append((script_name, extra_args))

    monkeypatch.setattr(cli_main, "_run_python_tool", fake_run_python_tool)

    args = SimpleNamespace(
        slice="jsonpeek",
        corpus="qualification/corpora/gate2_local_open_weight.json",
        workers=1,
        timeout_seconds=900,
    )

    await cli_main.cmd_proof(args)

    assert calls == [
        (
            "run_delivery_proof.py",
            [
                "--slice",
                "jsonpeek",
                "--workers",
                "1",
                "--timeout-seconds",
                "900",
                "--retry-max",
                "0",
                "--corpus",
                "qualification/corpora/gate2_local_open_weight.json",
            ],
        )
    ]


@pytest.mark.asyncio
async def test_cmd_aether_adapter_proof_delegates_to_runner(monkeypatch) -> None:
    calls: list[tuple[str, list[str]]] = []

    async def fake_run_python_tool(script_name: str, extra_args: list[str]) -> None:
        calls.append((script_name, extra_args))

    monkeypatch.setattr(cli_main, "_run_python_tool", fake_run_python_tool)

    args = SimpleNamespace(
        aether_base_url="http://127.0.0.1:8100",
        repo_name="proof-repo",
        run_id="mx-run-1",
        poll_attempts=30,
        poll_interval_seconds=0.5,
    )

    await cli_main.cmd_aether_adapter_proof(args)

    assert calls == [
        (
            "run_aether_adapter_proof.py",
            [
                "--aether-base-url",
                "http://127.0.0.1:8100",
                "--repo-name",
                "proof-repo",
                "--run-id",
                "mx-run-1",
                "--poll-attempts",
                "30",
                "--poll-interval-seconds",
                "0.5",
            ],
        )
    ]


@pytest.mark.asyncio
async def test_cmd_aether_worker_proof_delegates_to_runner(monkeypatch) -> None:
    calls: list[tuple[str, list[str]]] = []

    async def fake_run_python_tool(script_name: str, extra_args: list[str]) -> None:
        calls.append((script_name, extra_args))

    monkeypatch.setattr(cli_main, "_run_python_tool", fake_run_python_tool)

    args = SimpleNamespace(
        slice="jsonpeek",
        timeout_seconds=900,
        aether_base_url="http://127.0.0.1:8100",
        repo_name="worker-proof-repo",
        run_id="worker-run-1",
        fixture="proof_fixtures/jsonpeek_canned_completion.json",
    )

    await cli_main.cmd_aether_worker_proof(args)

    assert calls == [
        (
            "run_aether_worker_proof.py",
            [
                "--slice",
                "jsonpeek",
                "--timeout-seconds",
                "900",
                "--aether-base-url",
                "http://127.0.0.1:8100",
                "--repo-name",
                "worker-proof-repo",
                "--run-id",
                "worker-run-1",
                "--fixture",
                "proof_fixtures/jsonpeek_canned_completion.json",
            ],
        )
    ]


@pytest.mark.asyncio
async def test_cmd_aether_live_proof_delegates_to_runner(monkeypatch) -> None:
    calls: list[tuple[str, list[str]]] = []

    async def fake_run_python_tool(script_name: str, extra_args: list[str]) -> None:
        calls.append((script_name, extra_args))

    monkeypatch.setattr(cli_main, "_run_python_tool", fake_run_python_tool)

    args = SimpleNamespace(
        slice="jsonpeek",
        corpus="qualification/corpora/gate2_local_open_weight.json",
        timeout_seconds=1200,
        aether_base_url="http://127.0.0.1:8100",
        repo_name="live-proof-repo",
        run_id="live-run-1",
        report_root="reports/aether_live_proof",
        llm_provider="ollama",
        llm_model="mistral:7b",
        llm_base_url="http://127.0.0.1:11434",
        llm_api_key_env=None,
        canned_completion_file=None,
    )

    await cli_main.cmd_aether_live_proof(args)

    assert calls == [
        (
            "run_aether_live_proof.py",
            [
                "--slice",
                "jsonpeek",
                "--timeout-seconds",
                "1200",
                "--corpus",
                "qualification/corpora/gate2_local_open_weight.json",
                "--aether-base-url",
                "http://127.0.0.1:8100",
                "--repo-name",
                "live-proof-repo",
                "--run-id",
                "live-run-1",
                "--report-root",
                "reports/aether_live_proof",
                "--llm-provider",
                "ollama",
                "--llm-model",
                "mistral:7b",
                "--llm-base-url",
                "http://127.0.0.1:11434",
            ],
        )
    ]


@pytest.mark.asyncio
async def test_cmd_top_renders_control_plane_snapshot(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli_main, "load_config", lambda _path: MXConfig())
    monkeypatch.setattr(
        cli_main,
        "build_control_plane_snapshot",
        lambda **kwargs: {"macro": {}, "rigs": [], "hold_distribution": [], "recent_governor": []},
    )
    monkeypatch.setattr(cli_main, "render_mxtop", lambda snapshot: "MX TOP TEST")

    args = SimpleNamespace(config="mx.config.json", limit_rigs=5)
    await cli_main.cmd_top(args)

    captured = capsys.readouterr()
    assert "MX TOP TEST" in captured.out


@pytest.mark.asyncio
async def test_cmd_gcp_stage_delegates_to_runner(monkeypatch) -> None:
    calls: list[tuple[str, list[str]]] = []

    async def fake_run_python_tool(script_name: str, extra_args: list[str]) -> None:
        calls.append((script_name, extra_args))

    monkeypatch.setattr(cli_main, "_run_python_tool", fake_run_python_tool)

    args = SimpleNamespace(
        gate="A",
        project_id="mx-staging",
        corpus="qualification/corpora/gate2_local_open_weight.json",
        topic_prefix="mx-gate",
        subscription_prefix="mx-gate",
        config="mx.config.cloud.json",
    )

    await cli_main.cmd_gcp_stage(args)

    assert calls == [
        (
            "run_gcp_gate_ab.py",
            [
                "--gate",
                "A",
                "--project-id",
                "mx-staging",
                "--corpus",
                "qualification/corpora/gate2_local_open_weight.json",
                "--topic-prefix",
                "mx-gate",
                "--subscription-prefix",
                "mx-gate",
                "--config",
                "mx.config.cloud.json",
            ],
        )
    ]


@pytest.mark.asyncio
async def test_cmd_budget_prints_snapshot_and_persists_caps(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    config = MXConfig()
    config.save("mx.config.json")
    monkeypatch.setattr(cli_main, "load_config", lambda _path: MXConfig.load("mx.config.json"))
    monkeypatch.setattr(
        cli_main,
        "get_budget_snapshot",
        lambda _budget: {
            "total_spend_today_usd": 1.25,
            "frontier_spend_today_usd": 0.75,
            "control_plane_spend_today_usd": 0.1,
            "frontier_claims_last_hour": 1,
            "active_frontier_claims": 1,
            "cost_per_success_usd": 1.25,
            "frontier_frozen": False,
            "total_frozen": False,
        },
    )

    args = SimpleNamespace(
        config="mx.config.json",
        daily=25.0,
        frontier_daily=10.0,
        control_plane_daily=5.0,
        bulk_lane_daily=0.0,
        frontier_hourly=5,
        frontier_concurrency=2,
        frontier_attempts=2,
        cost_per_success=5.0,
        freeze_mode="frontier_only",
    )

    await cli_main.cmd_budget(args)

    saved = MXConfig.load("mx.config.json")
    assert saved.budget.daily_usd_cap == 25.0
    assert saved.budget.frontier_daily_usd_cap == 10.0
    assert saved.budget.max_frontier_concurrency == 2
    assert saved.budget.freeze_mode == "frontier_only"


@pytest.mark.asyncio
async def test_cmd_frontier_smoke_delegates_to_runner(monkeypatch) -> None:
    calls: list[tuple[str, list[str]]] = []

    async def fake_run_python_tool(script_name: str, extra_args: list[str]) -> None:
        calls.append((script_name, extra_args))

    monkeypatch.setattr(cli_main, "_run_python_tool", fake_run_python_tool)

    args = SimpleNamespace(
        slice="jsonpeek",
        corpus="qualification/corpora/gate2_local_open_weight.json",
        provider="ollama",
        model="mistral:7b",
        base_url="http://localhost:11434",
        timeout_seconds=900,
    )

    await cli_main.cmd_frontier_smoke(args)

    assert calls == [
        (
            "run_frontier_smoke.py",
            [
                "--slice",
                "jsonpeek",
                "--timeout-seconds",
                "900",
                "--corpus",
                "qualification/corpora/gate2_local_open_weight.json",
                "--provider",
                "ollama",
                "--model",
                "mistral:7b",
                "--base-url",
                "http://localhost:11434",
            ],
        )
    ]
