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


@pytest.mark.asyncio
async def test_acceptance_workspace_cannot_recover_github_token_from_git_config_or_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import base64
    import subprocess
    from datetime import UTC, datetime

    from shared.adapters.queue import QueueMessage
    from shared.schemas import AcceptanceCriteria, TaskContext, TaskContract, TaskType

    # Create a real local bare remote fixture and seed it with an initial commit on 'main'
    bare_remote = tmp_path / "remote_origin.git"
    seed_repo = tmp_path / "seed_repo"
    bare_remote.mkdir()
    seed_repo.mkdir()
    git_env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
        "GIT_AUTHOR_NAME": "Seed Author",
        "GIT_AUTHOR_EMAIL": "seed@example.com",
        "GIT_COMMITTER_NAME": "Seed Author",
        "GIT_COMMITTER_EMAIL": "seed@example.com",
    }
    subprocess.run(["git", "init", "-q", "--bare", str(bare_remote)], check=True, env=git_env)
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed_repo)], check=True, env=git_env)
    (seed_repo / "app_mod.py").write_text("def value() -> int:\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(seed_repo), "add", "app_mod.py"], check=True, env=git_env)
    subprocess.run(
        ["git", "-C", str(seed_repo), "commit", "-q", "-m", "initial commit"],
        check=True,
        env=git_env,
    )
    subprocess.run(
        ["git", "-C", str(seed_repo), "remote", "add", "origin", str(bare_remote)],
        check=True,
        env=git_env,
    )
    subprocess.run(["git", "-C", str(seed_repo), "push", "-q", "origin", "main"], check=True, env=git_env)

    # Redirect https://github.com/example/repo.git to the local bare fixture via GIT_CONFIG_*
    synthetic_token = "ghp_SYNTHETIC_ACCEPTANCE_SECRET_9876543210"
    basic_secret = base64.b64encode(f"x-access-token:{synthetic_token}".encode()).decode("ascii")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{bare_remote}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/example/repo.git")

    config = MXConfig()
    config.vcs.provider = "github"
    config.github.token = synthetic_token
    config.github.repo_owner = "example"
    config.github.repo_name = "repo"
    config.github.default_branch = "main"

    queue = StubQueue()
    llm = StubLLM("```file:app_mod.py\ndef value() -> int:\n    return 2\n```\nUpdated value.")
    vcs = CapturingVCS()
    worker = Worker(config=config, worker_id="w-cred-isolation", queue=queue, llm=llm, vcs=vcs)

    # Acceptance check attempts to recover the synthetic token or its base64 form from:
    # 1. os.environ
    # 2. .git/config and all files under the workspace (including .git/)
    # 3. git remote -v and git config --list --local
    probe_cmd = (
        "python3 -c \""
        "import os, pathlib, subprocess, sys; "
        f"targets = ['{synthetic_token}', '{basic_secret}']; "
        "env_dump = repr(dict(os.environ)); "
        "git_cfg = pathlib.Path('.git/config').read_text(encoding='utf-8'); "
        "remotes = subprocess.check_output(['git', 'remote', '-v'], text=True); "
        "local_cfg = subprocess.check_output(['git', 'config', '--list', '--local'], text=True); "
        "all_git_files = '\\n'.join("
        "p.read_text(encoding='utf-8', errors='ignore') "
        "for p in pathlib.Path('.git').rglob('*') if p.is_file()"
        "); "
        "combined = '\\n'.join([env_dump, git_cfg, remotes, local_cfg, all_git_files]); "
        "leaked = [t for t in targets if t in combined]; "
        "sys.exit(1 if leaked else 0)"
        "\""
    )

    task = TaskContract.create(
        task_type=TaskType.BUGFIX,
        goal="Update app_mod value safely without exposing GitHub credentials",
        context=TaskContext(files_hint=["app_mod.py"]),
        acceptance=AcceptanceCriteria(
            criteria=["No GitHub token recoverable from acceptance workspace"],
            checks=[probe_cmd],
        ),
    )
    msg = QueueMessage(
        id="msg-cred-probe",
        body=task.to_json(),
        receipt_handle="rh-cred-probe",
        receive_count=1,
        enqueued_at=datetime.now(UTC),
        attributes={},
    )

    ok = await worker.process_task(msg)
    assert ok is True
    assert queue.acks == ["rh-cred-probe"]
    assert queue.nacks == []


@pytest.mark.asyncio
async def test_acceptance_installed_hooks_and_git_config_tampering_cannot_capture_push_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import base64
    import shlex
    import subprocess
    import sys
    from datetime import UTC, datetime

    from shared.adapters.queue import QueueMessage
    from shared.schemas import AcceptanceCriteria, TaskContext, TaskContract, TaskType

    bare_remote = tmp_path / "remote_origin.git"
    seed_repo = tmp_path / "seed_repo"
    bare_remote.mkdir()
    seed_repo.mkdir()
    git_env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
        "GIT_AUTHOR_NAME": "Seed Author",
        "GIT_AUTHOR_EMAIL": "seed@example.com",
        "GIT_COMMITTER_NAME": "Seed Author",
        "GIT_COMMITTER_EMAIL": "seed@example.com",
    }
    subprocess.run(["git", "init", "-q", "--bare", str(bare_remote)], check=True, env=git_env)
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed_repo)], check=True, env=git_env)
    (seed_repo / "app_mod.py").write_text("def value() -> int:\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(seed_repo), "add", "app_mod.py"], check=True, env=git_env)
    subprocess.run(
        ["git", "-C", str(seed_repo), "commit", "-q", "-m", "initial commit"],
        check=True,
        env=git_env,
    )
    subprocess.run(
        ["git", "-C", str(seed_repo), "remote", "add", "origin", str(bare_remote)],
        check=True,
        env=git_env,
    )
    subprocess.run(["git", "-C", str(seed_repo), "push", "-q", "origin", "main"], check=True, env=git_env)

    synthetic_token = "ghp_SYNTHETIC_HOOK_CAPTURE_SECRET_9876543210"
    basic_secret = base64.b64encode(f"x-access-token:{synthetic_token}".encode()).decode("ascii")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{bare_remote}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/example/repo.git")

    config = MXConfig()
    config.vcs.provider = "github"
    config.github.token = synthetic_token
    config.github.repo_owner = "example"
    config.github.repo_name = "repo"
    config.github.default_branch = "main"

    queue = StubQueue()
    llm = StubLLM("```file:app_mod.py\ndef value() -> int:\n    return 2\n```\nUpdated value.")
    vcs = CapturingVCS()
    worker = Worker(config=config, worker_id="w-hook-isolation", queue=queue, llm=llm, vcs=vcs)

    capture_file = tmp_path / "synthetic-hook-capture.txt"
    evil_hooks_dir = tmp_path / "evil_external_hooks"
    evil_hooks_dir.mkdir()
    capture_script = tmp_path / "evil_capture.sh"
    capture_script.write_text(
        "#!/bin/sh\n"
        f"env >> {shlex.quote(str(capture_file))}\n"
        'if [ -n "$GIT_ASKPASS" ] && [ -x "$GIT_ASKPASS" ]; then\n'
        f'  "$GIT_ASKPASS" "Password" >> {shlex.quote(str(capture_file))} 2>&1 || true\n'
        "fi\n",
        encoding="utf-8",
    )
    capture_script.chmod(0o700)

    tamper_script = tmp_path / "tamper_workspace_git.py"
    tamper_script.write_text(
        "from pathlib import Path\n"
        f"capture_sh = Path({str(capture_script)!r}).read_text(encoding='utf-8')\n"
        "hooks_dir = Path('.git/hooks')\n"
        "hooks_dir.mkdir(parents=True, exist_ok=True)\n"
        "for hook_name in ('pre-push', 'pre-commit', 'commit-msg', 'post-commit', 'reference-transaction'):\n"
        "    hp = hooks_dir / hook_name\n"
        "    hp.write_text(capture_sh, encoding='utf-8')\n"
        "    hp.chmod(0o700)\n"
        f"ext_hooks = Path({str(evil_hooks_dir)!r})\n"
        "for hook_name in ('pre-push', 'pre-commit', 'commit-msg', 'post-commit'):\n"
        "    ehp = ext_hooks / hook_name\n"
        "    ehp.write_text(capture_sh, encoding='utf-8')\n"
        "    ehp.chmod(0o700)\n"
        "cfg_path = Path('.git/config')\n"
        "cfg_text = cfg_path.read_text(encoding='utf-8')\n"
        f"cmd = {str(capture_script)!r}\n"
        "tampered_cfg = cfg_text + (\n"
        f"    '\\n[core]\\n\\thooksPath = {evil_hooks_dir}\\n'\n"
        "    f'\\tfsmonitor = {cmd}\\n'\n"
        "    f'\\tsshCommand = {cmd}\\n'\n"
        "    f'\\taskPass = {cmd}\\n'\n"
        "    f'\\tpager = {cmd}\\n'\n"
        "    f'[credential]\\n\\thelper = !{cmd}\\n'\n"
        "    f'[filter \"evil\"]\\n\\tclean = {cmd}\\n\\tsmudge = {cmd}\\n'\n"
        ")\n"
        "cfg_path.write_text(tampered_cfg, encoding='utf-8')\n"
        "cfg_path.chmod(0o400)\n"
        "info_dir = Path('.git/info')\n"
        "info_dir.mkdir(parents=True, exist_ok=True)\n"
        "(info_dir / 'attributes').write_text('* filter=evil\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )

    tamper_cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(tamper_script))}"

    task = TaskContract.create(
        task_type=TaskType.BUGFIX,
        goal="Update app_mod value while resisting acceptance hook and config tampering",
        context=TaskContext(files_hint=["app_mod.py"]),
        acceptance=AcceptanceCriteria(
            criteria=["Acceptance hook and config tampering cannot capture push credentials"],
            checks=[tamper_cmd],
        ),
    )
    msg = QueueMessage(
        id="msg-hook-tamper",
        body=task.to_json(),
        receipt_handle="rh-hook-tamper",
        receive_count=1,
        enqueued_at=datetime.now(UTC),
        attributes={},
    )

    ok = await worker.process_task(msg)
    assert ok is True
    assert queue.acks == ["rh-hook-tamper"]
    assert queue.nacks == []

    # Verify neither the pre-push/pre-commit hooks nor any tampered config helpers executed or captured secrets
    captured_text = capture_file.read_text(encoding="utf-8") if capture_file.exists() else ""
    assert captured_text == ""
    assert synthetic_token not in captured_text
    assert basic_secret not in captured_text

    # Verify the task branch was genuinely pushed to the bare remote fixture
    remote_branches = subprocess.check_output(
        ["git", "-C", str(bare_remote), "branch", "--list"],
        text=True,
        env=git_env,
    )
    assert task.branch_name() in remote_branches
