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


@pytest.mark.asyncio
async def test_authenticated_git_operations_use_ephemeral_auth_and_clean_up_on_success_and_failure(
    monkeypatch,
    tmp_path,
) -> None:
    from pathlib import Path

    destination = tmp_path / "dest"
    destination.mkdir()
    git_dir = destination / ".git"
    git_dir.mkdir()
    synthetic_token = "ghp_SYNTHETIC_GIT_TOKEN_ABCDEF123456"
    (git_dir / "config").write_text(
        f'[remote "origin"]\n\turl = https://x-access-token:{synthetic_token}@github.com/example/repo.git\n',
        encoding="utf-8",
    )

    captured_calls: list[tuple[list[str], dict[str, str] | None, bool]] = []
    observed_askpass_paths: list[Path] = []

    async def fake_run_git(args, cwd=None, env=None):
        askpass = Path(env["GIT_ASKPASS"]) if env and "GIT_ASKPASS" in env else None
        if askpass is not None:
            observed_askpass_paths.append(askpass)
        captured_calls.append((list(args), dict(env) if env else None, bool(askpass and askpass.is_file())))
        if args[0] == "fetch":
            return GitResult(
                False,
                "",
                f"fatal: repository 'https://x-access-token:{synthetic_token}@github.com/example/repo.git/' not found",
                128,
            )
        return GitResult(True, "", "", 0)

    monkeypatch.setattr(git_utils, "run_git", fake_run_git)

    # Even if initialized with an embedded-credential URL, repo_url and authenticated_url strip credentials
    repo = GitRepo(
        repo_url=f"https://x-access-token:{synthetic_token}@github.com/example/repo.git",
        local_path=str(destination),
    )
    assert repo.repo_url == "https://github.com/example/repo.git"
    assert repo.authenticated_url == "https://github.com/example/repo.git"
    assert synthetic_token not in repo.repo_url
    assert synthetic_token not in repo.authenticated_url

    clone_res = await repo.clone(branch="main", depth=1)
    assert clone_res.success is True

    fetch_res = await repo.fetch("main")
    assert fetch_res.success is False
    assert synthetic_token not in fetch_res.stderr
    assert "<REDACTED_CREDENTIALS>" in fetch_res.stderr

    pull_res = await repo.pull("main")
    assert pull_res.success is True

    push_res = await repo.push("feat/test")
    assert push_res.success is True

    assert len(captured_calls) == 4
    for args, env, askpass_existed_during_call in captured_calls:
        # Command arguments and raw environment variables never contain the plaintext token
        assert all(synthetic_token not in arg for arg in args)
        assert env is not None
        assert "MX_GIT_EPHEMERAL_TOKEN" not in env
        assert all(synthetic_token not in val for val in env.values())
        assert askpass_existed_during_call is True
        # Hardened config overrides disable workspace hooks and external helpers
        cfg_pairs = {
            env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
            for i in range(int(env["GIT_CONFIG_COUNT"]))
            if f"GIT_CONFIG_KEY_{i}" in env
        }
        assert cfg_pairs.get("core.fsmonitor") == "false"
        assert cfg_pairs.get("core.sshCommand") == "false"
        assert cfg_pairs.get("protocol.ext.allow") == "never"
        assert "core.hooksPath" in cfg_pairs

    # Every temporary askpass directory was removed immediately after each git invocation
    assert observed_askpass_paths
    for askpass_path in observed_askpass_paths:
        assert not askpass_path.exists()
        assert not askpass_path.parent.exists()

    # Workspace .git/config was scrubbed of embedded credentials
    scrubbed_config = (git_dir / "config").read_text(encoding="utf-8")
    assert synthetic_token not in scrubbed_config
    assert "url = https://github.com/example/repo.git" in scrubbed_config

    # Verify cleanup even when run_git raises an unexpected exception
    async def raising_run_git(args, cwd=None, env=None):
        askpass = Path(env["GIT_ASKPASS"])
        observed_askpass_paths.append(askpass)
        raise RuntimeError("simulated subprocess failure")

    monkeypatch.setattr(git_utils, "run_git", raising_run_git)
    with pytest.raises(RuntimeError, match="simulated subprocess failure"):
        await repo.push("feat/test")
    assert not observed_askpass_paths[-1].parent.exists()
