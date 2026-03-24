import pytest

import shared.adapters.aether_vcs as aether_module
from shared.adapters.aether_vcs import AetherVCS
from shared.adapters.vcs_adapter import SubmitResult


@pytest.mark.asyncio
async def test_submit_retries_on_unhealthy() -> None:
    vcs = AetherVCS(base_url="http://localhost:8100", repo_id="repo_test", max_retries=2, base_backoff_s=0)

    calls = {"health": 0, "submit": 0}

    async def fake_health() -> bool:
        calls["health"] += 1
        return False

    async def fake_submit(_url: str, _data: dict) -> SubmitResult:
        calls["submit"] += 1
        return SubmitResult(
            success=True,
            submission_type="proposal",
            submission_id="prop_ok",
            status="PENDING",
            message="ok",
        )

    aether_module.aiohttp = object()
    vcs.health_check = fake_health  # type: ignore[assignment]
    vcs._submit_aiohttp = fake_submit  # type: ignore[assignment]

    result = await vcs.submit_change(
        task_id="task",
        goal="goal",
        branch_name="branch",
        diff="diff",
        affected_paths=[],
        evidence_markdown="evidence",
    )

    assert not result.success
    assert calls["health"] == 2
    assert calls["submit"] == 0


@pytest.mark.asyncio
async def test_submit_retries_on_exception_then_succeeds() -> None:
    vcs = AetherVCS(base_url="http://localhost:8100", repo_id="repo_test", max_retries=3, base_backoff_s=0)

    calls = {"health": 0, "submit": 0}

    async def fake_health() -> bool:
        calls["health"] += 1
        return True

    async def fake_submit(_url: str, _data: dict) -> SubmitResult:
        calls["submit"] += 1
        if calls["submit"] == 1:
            raise RuntimeError("temporary network")
        return SubmitResult(
            success=True,
            submission_type="proposal",
            submission_id="prop_ok",
            status="PENDING",
            message="ok",
        )

    aether_module.aiohttp = object()
    vcs.health_check = fake_health  # type: ignore[assignment]
    vcs._submit_aiohttp = fake_submit  # type: ignore[assignment]

    result = await vcs.submit_change(
        task_id="task",
        goal="goal",
        branch_name="branch",
        diff="diff",
        affected_paths=[],
        evidence_markdown="evidence",
    )

    assert result.success
    assert calls["health"] == 2
    assert calls["submit"] == 2


@pytest.mark.asyncio
async def test_submit_change_forwards_provenance_fields() -> None:
    vcs = AetherVCS(base_url="http://localhost:8100", repo_id="repo_test", max_retries=1, base_backoff_s=0)

    captured: dict[str, object] = {}

    async def fake_health() -> bool:
        return True

    async def fake_submit(_url: str, data: dict) -> SubmitResult:
        captured.update(data)
        return SubmitResult(
            success=True,
            submission_type="proposal",
            submission_id="prop_ok",
            status="PENDING",
            message="ok",
        )

    aether_module.aiohttp = object()
    vcs.health_check = fake_health  # type: ignore[assignment]
    vcs._submit_aiohttp = fake_submit  # type: ignore[assignment]

    result = await vcs.submit_change(
        task_id="task-123",
        goal="goal",
        branch_name="branch",
        diff="diff",
        affected_paths=["tools/jsonpeek.py"],
        evidence_markdown="evidence",
        source="mx_swarm",
        source_run_id="run-1",
        source_task_id="task-123",
        source_agent_id="worker-1",
        source_attempt_id="attempt-2",
        intent_type="feature",
    )

    assert result.success is True
    assert captured["source"] == "mx_swarm"
    assert captured["source_run_id"] == "run-1"
    assert captured["source_task_id"] == "task-123"
    assert captured["source_agent_id"] == "worker-1"
    assert captured["source_attempt_id"] == "attempt-2"
    assert captured["intent_type"] == "feature"


@pytest.mark.asyncio
async def test_submit_sync_returns_canonical_v1_proposal_url(monkeypatch) -> None:
    vcs = AetherVCS(base_url="http://localhost:8100", repo_id="repo_test", max_retries=1, base_backoff_s=0)

    class _Response:
        def read(self) -> bytes:
            return b'{"proposal_id":"prop_ok","status":"PROMOTED"}'

        def getcode(self) -> int:
            return 201

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=30: _Response(),
    )

    result = await vcs._submit_sync("http://localhost:8100/v1/repos/repo_test/proposals", {"change": "x"})

    assert result.success is True
    assert result.url == "http://localhost:8100/v1/repos/repo_test/proposals/prop_ok"


def test_build_proposal_payload_sets_expected_tuple_and_idempotency() -> None:
    vcs = AetherVCS(base_url="http://localhost:8100", repo_id="repo_test")

    payload = vcs._build_proposal_payload(
        task_id="task-123",
        goal="goal",
        branch_name="task/task-123-goal",
        diff="diff",
        affected_paths=["proofs/task-123.txt"],
        evidence_markdown="evidence",
        confidence=1.0,
        model_id="deterministic-proof",
        risk_tier=0,
        source="mx_swarm",
        source_run_id="run-1",
        source_task_id="task-123",
        source_agent_id="proof-worker-0",
        source_attempt_id="attempt-1",
        intent_type="feature",
    )

    assert payload["source"] == "mx_swarm"
    assert payload["source_run_id"] == "run-1"
    assert payload["source_task_id"] == "task-123"
    assert payload["source_agent_id"] == "proof-worker-0"
    assert payload["source_attempt_id"] == "attempt-1"
    assert payload["intent_type"] == "feature"
    assert payload["idempotency_key"] == "mx-task-123-task/task-123-goal"


@pytest.mark.asyncio
async def test_verify_landed_change_requires_path_evidence() -> None:
    vcs = AetherVCS(base_url="http://localhost:8100", repo_id="repo_test")

    async def fake_status(_submission_id: str) -> dict[str, object]:
        return {"status": "accepted"}

    vcs.get_status = fake_status  # type: ignore[assignment]

    result = await vcs.verify_landed_change(
        "prop-1",
        expected_paths=["tools/jsonpeek.py"],
    )

    assert result.supported is True
    assert result.verified is False
    assert result.canonical_status == "accepted"
    assert "did not return landed path evidence" in result.detail


@pytest.mark.asyncio
async def test_verify_landed_change_accepts_matching_paths() -> None:
    vcs = AetherVCS(base_url="http://localhost:8100", repo_id="repo_test")

    async def fake_status(_submission_id: str) -> dict[str, object]:
        return {
            "status": "accepted",
            "affected_paths": ["tools/jsonpeek.py", "tests/test_jsonpeek.py"],
            "change_content": "diff",
        }

    vcs.get_status = fake_status  # type: ignore[assignment]

    result = await vcs.verify_landed_change(
        "prop-1",
        expected_paths=["tools/jsonpeek.py"],
        expected_diff="diff",
    )

    assert result.supported is True
    assert result.verified is True
    assert result.verified_paths == ["tools/jsonpeek.py", "tests/test_jsonpeek.py"]


@pytest.mark.asyncio
async def test_verify_landed_change_accepts_paths_derived_from_diff() -> None:
    vcs = AetherVCS(base_url="http://localhost:8100", repo_id="repo_test")

    async def fake_status(_submission_id: str) -> dict[str, object]:
        return {
            "status": "PROMOTED",
            "change_content": (
                "diff --git a/tools/jsonpeek.py b/tools/jsonpeek.py\n"
                "new file mode 100644\n"
                "index 0000000..1111111\n"
                "--- /dev/null\n"
                "+++ b/tools/jsonpeek.py\n"
                "@@ -0,0 +1 @@\n"
                "+print('ok')\n"
            ),
        }

    vcs.get_status = fake_status  # type: ignore[assignment]

    result = await vcs.verify_landed_change(
        "prop-1",
        expected_paths=["tools/jsonpeek.py"],
    )

    assert result.supported is True
    assert result.verified is True
    assert result.canonical_status == "promoted"
    assert result.verified_paths == ["tools/jsonpeek.py"]
