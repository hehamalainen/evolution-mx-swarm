import pytest

from shared.utils import git as git_utils
from shared.utils.git import GitRepo, GitResult


@pytest.mark.asyncio
async def test_clone_local_repo_ignores_branch_and_depth(monkeypatch, tmp_path) -> None:
    source_repo = tmp_path / "source"
    source_repo.mkdir()
    destination = tmp_path / "dest"
    captured: list[list[str]] = []

    async def fake_run_git(args, cwd=None, env=None):
        captured.append(args)
        return GitResult(True, "", "", 0)

    monkeypatch.setattr(git_utils, "run_git", fake_run_git)

    repo = GitRepo(repo_url=str(source_repo), local_path=str(destination), token=None)
    result = await repo.clone(branch="feat/agent-marketplace", depth=1)

    assert result.success is True
    assert captured == [["clone", str(source_repo), str(destination)]]


@pytest.mark.asyncio
async def test_clone_remote_repo_keeps_branch_and_depth(monkeypatch, tmp_path) -> None:
    destination = tmp_path / "dest"
    captured: list[list[str]] = []

    async def fake_run_git(args, cwd=None, env=None):
        captured.append(args)
        return GitResult(True, "", "", 0)

    monkeypatch.setattr(git_utils, "run_git", fake_run_git)

    repo = GitRepo(
        repo_url="https://github.com/example/repo.git",
        local_path=str(destination),
        token=None,
    )
    result = await repo.clone(branch="main", depth=3)

    assert result.success is True
    assert captured == [[
        "clone",
        "--depth",
        "3",
        "--branch",
        "main",
        "https://github.com/example/repo.git",
        str(destination),
    ]]
