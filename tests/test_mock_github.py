from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from shared.utils.mock_github import MockGitHubAPI


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        check=False,
        cwd=repo,
        text=True,
    )


def test_mock_github_auto_merges_local_branch_into_base(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    assert _git(repo, "init", "-b", "main").returncode == 0
    assert _git(repo, "config", "user.name", "MX Test").returncode == 0
    assert _git(repo, "config", "user.email", "mx@example.com").returncode == 0

    tracked = repo / "tracked.txt"
    tracked.write_text("base\n", encoding="utf-8")
    assert _git(repo, "add", "tracked.txt").returncode == 0
    assert _git(repo, "commit", "-m", "base").returncode == 0

    assert _git(repo, "checkout", "-b", "feature/task").returncode == 0
    tracked.write_text("merged\n", encoding="utf-8")
    assert _git(repo, "commit", "-am", "feature").returncode == 0
    assert _git(repo, "checkout", "main").returncode == 0

    api = MockGitHubAPI(owner="local", repo="swarm", repo_path=str(repo))
    pr = asyncio.run(
        api.create_pull_request(
            title="Test PR",
            body="Body",
            head="feature/task",
            base="main",
            draft=False,
        )
    )

    assert pr["state"] == "closed"
    assert pr["merged"] is True
    assert pr["merged_at"]
    assert tracked.read_text(encoding="utf-8") == "merged\n"
