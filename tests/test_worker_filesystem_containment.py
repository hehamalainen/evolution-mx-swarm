from __future__ import annotations

from pathlib import Path

import pytest

from shared.utils.config import MXConfig
from shared.utils.git import GitRepo
from tests.test_worker_stability import CapturingVCS, StubLLM, StubQueue
from worker.service import Worker


def _make_worker() -> Worker:
    return Worker(
        config=MXConfig(),
        worker_id="worker-containment",
        queue=StubQueue(),
        llm=StubLLM(),
        vcs=CapturingVCS(),
    )


def test_resolve_allowed_target_path_rejects_quoted_absolute_and_traversal_paths() -> None:
    worker = _make_worker()
    allowed = {"tools/jsonpeek.py", "passwd"}

    for raw_path in (
        '"/etc/passwd"',
        "'/etc/passwd'",
        "`/tmp/outside.py`",
        '"../outside.py"',
        ".git/config",
        "tools/../../outside.py",
    ):
        resolved, reason = worker._resolve_allowed_target_path(raw_path, allowed)
        assert resolved is None, f"Expected {raw_path!r} to be rejected, got {resolved!r}"
        assert reason == "unsafe_or_malformed"


@pytest.mark.asyncio
async def test_read_and_write_file_reject_symlink_and_parent_escapes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    outside_secret = outside / "secret.txt"
    outside_secret.write_text("do-not-touch", encoding="utf-8")

    symlink_file = workspace / "escaped.txt"
    symlink_file.symlink_to(outside_secret)

    symlink_dir = workspace / "linked_dir"
    symlink_dir.symlink_to(outside)

    repo = GitRepo(repo_url=str(workspace), local_path=str(workspace))
    worker = _make_worker()

    with pytest.raises(PermissionError):
        await worker._write_file(repo, "escaped.txt", "overwritten")
    assert outside_secret.read_text(encoding="utf-8") == "do-not-touch"

    with pytest.raises(PermissionError):
        await worker._write_file(repo, "linked_dir/pwned.txt", "overwritten")
    assert not (outside / "pwned.txt").exists()

    with pytest.raises(PermissionError):
        await worker._read_file(repo, "escaped.txt")

    with pytest.raises(PermissionError):
        await worker._write_file(repo, "../outside/secret.txt", "overwritten")

    with pytest.raises(PermissionError):
        await worker._write_file(repo, f'"{outside_secret}"', "overwritten")


@pytest.mark.asyncio
async def test_run_command_strips_host_credentials_from_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-anthropic-host-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "dummy-github-host-secret")
    monkeypatch.setenv("AETHER_TOKEN", "dummy-aether-host-secret")

    worker = _make_worker()
    ok, output = await worker._run_command(
        (
            "python3 -c \"import os; "
            "print('ANTHROPIC=' + str(os.environ.get('ANTHROPIC_API_KEY'))); "
            "print('GITHUB=' + str(os.environ.get('GITHUB_TOKEN'))); "
            "print('AETHER=' + str(os.environ.get('AETHER_TOKEN')))\""
        ),
        cwd=str(tmp_path),
        timeout=15,
    )
    assert ok is True
    assert "ANTHROPIC=None" in output
    assert "GITHUB=None" in output
    assert "AETHER=None" in output
    assert "dummy-anthropic-host-secret" not in output
