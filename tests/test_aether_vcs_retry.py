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
