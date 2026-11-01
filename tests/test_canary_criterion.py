from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def get_canary():
    from scripts import run_aether_semantic_canary

    return run_aether_semantic_canary


@pytest.mark.asyncio
async def test_canary_fail_closed_on_unknown():
    """MX-1: Canary fails closed when branch/commit is unknown."""
    run_aether_semantic_canary = get_canary()
    mock_build_info = {
        "semantic_merge_enabled": True,
        "synthesis_worker_enabled": True,
        "branch": "unknown",
        "commit": "abc1234",
    }

    with patch("scripts.run_aether_semantic_canary._http_request") as mock_req:
        mock_req.return_value = mock_build_info
        args = MagicMock()
        args.aether_base_url = "http://localhost:8100"
        args.expected_aether_sha = None
        args.require_sqlite = False
        res = await run_aether_semantic_canary.verify_preflight_remediation(args)
        assert res == 1


@pytest.mark.asyncio
async def test_canary_fail_closed_on_mismatched_aether_sha():
    """Canary fails closed when running Aether build SHA differs from pinned SHA."""
    run_aether_semantic_canary = get_canary()
    pinned = "bc924b8b3b3d653ec01f2cdbcfc1332012fc29c6"
    mock_build_info = {
        "semantic_merge_enabled": True,
        "synthesis_worker_enabled": True,
        "branch": "main",
        "commit": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        "dirty": False,
        "database_profile": "sqlite",
    }

    with patch("scripts.run_aether_semantic_canary._http_request") as mock_req:
        mock_req.return_value = mock_build_info
        args = MagicMock()
        args.aether_base_url = "http://localhost:8100"
        args.expected_aether_sha = pinned
        args.require_sqlite = True
        args.structural_only = True
        assert await run_aether_semantic_canary.verify_preflight_remediation(args) == 1

        mock_build_info["commit"] = pinned
        assert await run_aether_semantic_canary.verify_preflight_remediation(args) == 0


@pytest.mark.asyncio
async def test_canary_fail_closed_on_stray_fallback():
    """MX-2: Canary fails closed when a stray fallback event is found."""
    run_aether_semantic_canary = get_canary()
    mock_events = {
        "events": [
            {
                "event_type": "semantic_fallback",
                "event_reason": "fallback_applied",
                "synthesis_engine": None,
                "proposal_id": "prop1",
            }
        ]
    }
    with patch("scripts.run_aether_semantic_canary._http_request") as mock_req:
        mock_req.return_value = mock_events
        args = MagicMock()
        res = await run_aether_semantic_canary.verify_canary_events(
            args, "repo1", "run1", ["prop1", "prop2"], True
        )
        assert res == 1


@pytest.mark.asyncio
async def test_canary_fail_closed_on_incomplete_cohort():
    """MX-3: Canary fails closed when cohort promotion is incomplete."""
    run_aether_semantic_canary = get_canary()
    mock_events = {
        "events": [
            {
                "event_type": "structural_merge",
                "event_reason": "structural_merge_applied",
                "synthesis_engine": None,
                "proposal_id": "prop1",
            }
        ]
    }
    with patch("scripts.run_aether_semantic_canary._http_request") as mock_req:
        mock_req.return_value = mock_events
        args = MagicMock()
        res = await run_aether_semantic_canary.verify_canary_events(
            args, "repo1", "run1", ["prop1", "prop2"], False
        )
        assert res == 1


@pytest.mark.asyncio
async def test_promotion_cohort_membership():
    """MX-4: Verify promotion record cohort membership matches expected proposals."""
    run_aether_semantic_canary = get_canary()
    mock_promo = {"included_proposals": '["prop1"]'}
    with patch("scripts.run_aether_semantic_canary._http_request") as mock_req:
        mock_req.return_value = mock_promo
        args = MagicMock()
        res = await run_aether_semantic_canary.verify_promotion_membership(
            args, "repo1", "promo1", ["prop1", "prop2"]
        )
        assert res == 1


@pytest.mark.asyncio
async def test_canonical_snapshot_content_markers():
    """MX-5: Assert snapshotMerged contains alpha/beta methods on CommandResult via AST and behavior."""
    run_aether_semantic_canary = get_canary()
    mock_file = {"content": "def alpha_method():\n  pass\n# missing beta"}
    with patch("scripts.run_aether_semantic_canary._http_request") as mock_req:
        mock_req.return_value = mock_file
        args = MagicMock()
        args.require_behavior_check = False
        res = await run_aether_semantic_canary.verify_snapshot_markers(
            args, "repo1", "snap1", "src/loglens/runtime.py"
        )
        assert res == 1


@pytest.mark.asyncio
async def test_snapshot_markers_ast_rejects_comments_and_strings_and_wrong_class():
    """AST + behavior check rejects comments, string literals, and methods on wrong class."""
    run_aether_semantic_canary = get_canary()

    decoy_comment_and_string = '''
# def alpha_method(self) -> str: return "alpha"
DECOY = """
class CommandResult:
    def alpha_method(self) -> str:
        return "alpha"
    def beta_method(self) -> str:
        return "beta"
"""
class CommandResult:
    pass
'''
    with patch("scripts.run_aether_semantic_canary._http_request") as mock_req:
        mock_req.return_value = {"content": decoy_comment_and_string}
        args = MagicMock()
        args.require_behavior_check = True
        assert (
            await run_aether_semantic_canary.verify_snapshot_markers(
                args, "repo1", "snap1", "src/loglens/runtime.py"
            )
            == 1
        )

    valid_merged = '''"""Shared command result and rendering helpers."""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from loglens.shared import formatting

OUTPUT_FORMATS = ("plain", "table", "json", "csv")


@dataclass(frozen=True)
class CommandResult:
    """Normalized command payload for every CLI feature."""

    plain_lines: list[str]
    columns: list[str]
    rows: list[dict[str, object]]

    def alpha_method(self) -> str:
        return "alpha"

    def beta_method(self) -> str:
        return "beta"


def add_output_format_option(parser: argparse.ArgumentParser) -> None:
    """Attach the shared output-format option to a command parser."""
    parser.add_argument(
        "--format",
        choices=OUTPUT_FORMATS,
        default="plain",
        help="Output format for the command result.",
    )


def render_result(result: CommandResult, *, output_format: str) -> str:
    """Render a command result in the requested output format."""
    if output_format == "plain":
        return formatting.join_lines(result.plain_lines)

    formatter = getattr(formatting, f"render_{output_format}", None)
    if formatter is None:
        raise NotImplementedError(f"output format '{output_format}' is not available yet")
    return formatter(result.rows, result.columns)
'''
    with patch("scripts.run_aether_semantic_canary._http_request") as mock_req:
        mock_req.return_value = {"content": valid_merged}
        args = MagicMock()
        args.require_behavior_check = True
        out: dict[str, object] = {}
        assert (
            await run_aether_semantic_canary.verify_snapshot_markers(
                args,
                "repo1",
                "snap1",
                "src/loglens/runtime.py",
                snapshot_verification_out=out,
            )
            == 0
        )
        ver = out["verification"]
        assert isinstance(ver, dict)
        assert ver["ast_verified"] is True
        assert ver["behavior_verified"] is True
        assert ver["behavior_outputs"] == {"alpha_method": "alpha", "beta_method": "beta"}


def test_canary_fail_closed_on_zero_or_skipped_checks():
    """Canary fails closed if zero checks execute, if any check is skipped, or if required check is missing."""
    run_aether_semantic_canary = get_canary()

    ok, reason = run_aether_semantic_canary.verify_checks_executed([])
    assert ok is False
    assert "Zero checks" in reason

    skipped_ledger = [
        {"name": name, "status": "PASS", "passed": True}
        for name in run_aether_semantic_canary.REQUIRED_CANARY_CHECKS
    ]
    skipped_ledger[0]["status"] = "SKIP"
    ok, reason = run_aether_semantic_canary.verify_checks_executed(skipped_ledger)
    assert ok is False
    assert "skipped" in reason.lower()

    valid_ledger = [
        {"name": name, "status": "PASS", "passed": True}
        for name in run_aether_semantic_canary.REQUIRED_CANARY_CHECKS
    ]
    skipped_promo = [{"id": "prom_1", "evidence": json.dumps({"build": "PASS", "unit_tests": "SKIP"})}]
    ok, reason = run_aether_semantic_canary.verify_checks_executed(
        valid_ledger, promotion_records=skipped_promo
    )
    assert ok is False
    assert "skipped gate" in reason.lower()

    passing_promo = [
        {
            "id": "prom_1",
            "evidence": json.dumps(
                {"syntax": "PASS", "build": "PASS", "unit_tests": "PASS", "lint": "PASS"}
            ),
        }
    ]
    ok, _ = run_aether_semantic_canary.verify_checks_executed(
        valid_ledger, promotion_records=passing_promo
    )
    assert ok is True


@pytest.mark.asyncio
async def test_telemetry_endpoint_response_archival():
    """MX-6: Canary archives raw telemetry queries to runner log files."""
    run_aether_semantic_canary = get_canary()
    with tempfile.TemporaryDirectory() as tmpdir:
        log_file = Path(tmpdir) / "canary_run.log"
        args = MagicMock()
        res = run_aether_semantic_canary.archive_telemetry_raw(args, {"test": "data"}, log_file)
        assert res is True
        assert log_file.exists()
        assert "test" in log_file.read_text()


@pytest.mark.asyncio
async def test_canary_contradictory_events_rejected():
    """MX-7: Reject if both structural and synthesis events are found for a proposal."""
    run_aether_semantic_canary = get_canary()
    mock_events = {
        "events": [
            {
                "event_type": "structural_merge",
                "event_reason": "structural_merge_applied",
                "proposal_id": "prop1",
            },
            {
                "event_type": "synthesis_merge",
                "event_reason": "synthesis_merge_applied",
                "proposal_id": "prop1",
            },
        ]
    }
    with patch("scripts.run_aether_semantic_canary._http_request") as mock_req:
        mock_req.return_value = mock_events
        args = MagicMock()
        res = await run_aether_semantic_canary.verify_canary_events(
            args, "repo1", "run1", ["prop1"], False
        )
        assert res == 1


@pytest.mark.asyncio
async def test_canary_synthesis_engine_validation():
    """MX-8: Verify engine metadata: synthesis must have deterministic_simulation, structural null."""
    run_aether_semantic_canary = get_canary()
    mock_events = {
        "events": [
            {
                "event_type": "structural_merge",
                "event_reason": "structural_merge_applied",
                "synthesis_engine": "deterministic_simulation",
                "proposal_id": "prop1",
            }
        ]
    }
    with patch("scripts.run_aether_semantic_canary._http_request") as mock_req:
        mock_req.return_value = mock_events
        args = MagicMock()
        res = await run_aether_semantic_canary.verify_canary_events(
            args, "repo1", "run1", ["prop1"], False
        )
        assert res == 1


@pytest.mark.asyncio
async def test_canary_positive_path_structural():
    """MX-9: Valid structural merge with synthesis_engine=null passes canary verification."""
    run_aether_semantic_canary = get_canary()
    mock_events = {
        "events": [
            {
                "event_type": "structural_merge",
                "event_reason": "structural_merge_applied",
                "synthesis_engine": None,
                "proposal_id": "prop1",
            },
            {
                "event_type": "structural_merge",
                "event_reason": "structural_merge_applied",
                "synthesis_engine": None,
                "proposal_id": "prop2",
            },
        ]
    }
    with patch("scripts.run_aether_semantic_canary._http_request") as mock_req:
        mock_req.return_value = mock_events
        args = MagicMock()
        res = await run_aether_semantic_canary.verify_canary_events(
            args, "repo1", "run1", ["prop1", "prop2"], True
        )
        assert res == 0


@pytest.mark.asyncio
async def test_canary_positive_path_synthesis():
    """MX-10: Valid deterministic synthesis merge with engine='deterministic_simulation' passes."""
    run_aether_semantic_canary = get_canary()
    mock_events = {
        "events": [
            {
                "event_type": "synthesis_merge",
                "event_reason": "synthesis_merge_applied",
                "synthesis_engine": "deterministic_simulation",
                "proposal_id": "prop1",
            },
            {
                "event_type": "synthesis_merge",
                "event_reason": "synthesis_merge_applied",
                "synthesis_engine": "deterministic_simulation",
                "proposal_id": "prop2",
            },
        ]
    }
    with patch("scripts.run_aether_semantic_canary._http_request") as mock_req:
        mock_req.return_value = mock_events
        args = MagicMock()
        res = await run_aether_semantic_canary.verify_canary_events(
            args, "repo1", "run1", ["prop1", "prop2"], False
        )
        assert res == 0


def create_mock_worker(vcs=None):
    from shared import MXConfig
    from worker.service import Worker

    config = MXConfig()
    config.vcs.provider = "mock"

    mock_secrets = MagicMock()
    mock_queue = MagicMock()
    mock_llm = MagicMock()
    mock_vcs = vcs or MagicMock()

    return Worker(
        config=config,
        worker_id="worker-test",
        queue=mock_queue,
        llm=mock_llm,
        vcs=mock_vcs,
        secrets=mock_secrets,
    )


@pytest.mark.asyncio
async def test_worker_canned_llm_bypass():
    """MX-11: Canned completions bypass LLM client construction and llm_chat tracing, but execute file application."""
    from shared.schemas.task_contract import TaskContext, TaskContract, TaskType

    canned_data = {
        "task-a": {
            "files": {"src/loglens/parsing.py": "def test_edited():\n    pass\n"},
            "note": "Canned change note",
        }
    }
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
        json.dump(canned_data, f)
        temp_fixture = f.name

    try:
        with patch.dict(os.environ, {"MX_CANNED_COMPLETION_FIXTURE": temp_fixture}):
            task = TaskContract.create(
                task_type=TaskType.FEATURE,
                goal="Implement control change",
                context=TaskContext(files_hint=["src/loglens/parsing.py"]),
            )
            task.id = "task-a"

            worker = create_mock_worker(vcs=MagicMock())
            worker._get_codebase_context = AsyncMock(
                side_effect=AssertionError("should not read codebase context")
            )
            worker._llm_client_for_task = MagicMock(
                side_effect=AssertionError("should not select LLM client")
            )
            worker.apply_changes = AsyncMock(return_value=(["src/loglens/parsing.py"], []))

            res = await worker._implement_task(MagicMock(), task)
            assert "src/loglens/parsing.py" in res[0]
    finally:
        if os.path.exists(temp_fixture):
            os.unlink(temp_fixture)


@pytest.mark.asyncio
async def test_worker_canned_missing_fail_closed():
    """MX-12: If fixture is active but missing a task, worker raises WorkerFastFailError immediately."""
    from shared.schemas.task_contract import TaskContract, TaskType
    from worker.service import WorkerFastFailError

    canned_data = {
        "other-task": {
            "files": {"other-file.py": "print(1)"},
            "note": "Not task-a",
        }
    }
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
        json.dump(canned_data, f)
        temp_fixture = f.name

    try:
        with patch.dict(os.environ, {"MX_CANNED_COMPLETION_FIXTURE": temp_fixture}):
            task = TaskContract.create(task_type=TaskType.FEATURE, goal="Implement control change")
            task.id = "task-a"
            worker = create_mock_worker()
            worker._get_codebase_context = AsyncMock(
                side_effect=AssertionError("should not read codebase")
            )

            with pytest.raises(WorkerFastFailError):
                await worker._implement_task(MagicMock(), task)
    finally:
        if os.path.exists(temp_fixture):
            os.unlink(temp_fixture)


@pytest.mark.asyncio
async def test_worker_canned_malformed_fail_closed():
    """MX-13: Malformed/unreadable fixture files raise WorkerFastFailError."""
    from shared.schemas.task_contract import TaskContract, TaskType
    from worker.service import WorkerFastFailError

    with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
        f.write("malformed { json")
        temp_fixture = f.name

    try:
        with patch.dict(os.environ, {"MX_CANNED_COMPLETION_FIXTURE": temp_fixture}):
            task = TaskContract.create(task_type=TaskType.FEATURE, goal="Implement")
            task.id = "task-a"
            worker = create_mock_worker()
            worker._get_codebase_context = AsyncMock(
                side_effect=AssertionError("should not read codebase")
            )

            with pytest.raises(WorkerFastFailError):
                await worker._implement_task(MagicMock(), task)
    finally:
        if os.path.exists(temp_fixture):
            os.unlink(temp_fixture)


@pytest.mark.asyncio
async def test_worker_secure_tar_extraction():
    """MX-14: Tar extraction rejects symlinks, hardlinks, traversals, oversized, unmanifested files, and mismatches."""
    from worker.service import WorkerFastFailError

    worker = create_mock_worker()

    def create_tar_with_file(
        name,
        content,
        link_target=None,
        is_symlink=False,
        is_hardlink=False,
        add_manifest=True,
        manifest_override=None,
    ):
        tar_stream = io.BytesIO()
        with tarfile.open(fileobj=tar_stream, mode="w:gz") as tar:
            if is_symlink:
                tarinfo = tarfile.TarInfo(name=name)
                tarinfo.type = tarfile.SYMTYPE
                tarinfo.linkname = link_target
                tar.addfile(tarinfo)
            elif is_hardlink:
                tarinfo = tarfile.TarInfo(name=name)
                tarinfo.type = tarfile.LNKTYPE
                tarinfo.linkname = link_target
                tar.addfile(tarinfo)
            else:
                tarinfo = tarfile.TarInfo(name=name)
                tarinfo.size = len(content)
                tar.addfile(tarinfo, io.BytesIO(content))

            if add_manifest:
                if manifest_override is not None:
                    man_data = dict(manifest_override)
                else:
                    man_data = {
                        "snapshot_id": "snap_1",
                        "repo_id": "repo_1",
                        "tree_hash": "correct_hash",
                        "files": {},
                    }
                    if name != ".aether_manifest.json" and not is_symlink and not is_hardlink:
                        man_data["files"][name] = hashlib.sha256(content).hexdigest()
                man_bytes = json.dumps(man_data).encode("utf-8")
                man_info = tarfile.TarInfo(name=".aether_manifest.json")
                man_info.size = len(man_bytes)
                tar.addfile(man_info, io.BytesIO(man_bytes))

        return tar_stream.getvalue()

    with pytest.raises(WorkerFastFailError):
        await worker.extract_snapshot_securely(
            create_tar_with_file("../evil.py", b"print(1)"),
            "/tmp/extract",
            "repo_1",
            "snap_1",
            "correct_hash",
        )

    with pytest.raises(WorkerFastFailError):
        await worker.extract_snapshot_securely(
            create_tar_with_file("/tmp/evil.py", b"print(1)"),
            "/tmp/extract",
            "repo_1",
            "snap_1",
            "correct_hash",
        )

    with pytest.raises(WorkerFastFailError):
        await worker.extract_snapshot_securely(
            create_tar_with_file("link.py", b"", link_target="/etc/passwd", is_symlink=True),
            "/tmp/extract",
            "repo_1",
            "snap_1",
            "correct_hash",
        )

    with pytest.raises(WorkerFastFailError):
        await worker.extract_snapshot_securely(
            create_tar_with_file("link.py", b"", link_target="/etc/passwd", is_hardlink=True),
            "/tmp/extract",
            "repo_1",
            "snap_1",
            "correct_hash",
        )

    with pytest.raises(WorkerFastFailError):
        await worker.extract_snapshot_securely(
            create_tar_with_file(
                "extra.py",
                b"print(1)",
                add_manifest=True,
                manifest_override={
                    "snapshot_id": "snap_1",
                    "repo_id": "repo_1",
                    "tree_hash": "correct_hash",
                    "files": {},
                },
            ),
            "/tmp/extract",
            "repo_1",
            "snap_1",
            "correct_hash",
        )

    with pytest.raises(WorkerFastFailError):
        await worker.extract_snapshot_securely(
            create_tar_with_file(
                "a.py",
                b"print(1)",
                add_manifest=True,
                manifest_override={
                    "snapshot_id": "snap_1",
                    "repo_id": "repo_1",
                    "tree_hash": "correct_hash",
                    "files": {"a.py": "incorrect_sha256"},
                },
            ),
            "/tmp/extract",
            "repo_1",
            "snap_1",
            "correct_hash",
        )

    with pytest.raises(WorkerFastFailError):
        await worker.extract_snapshot_securely(
            create_tar_with_file(
                "a.py",
                b"print(1)",
                add_manifest=True,
                manifest_override={
                    "snapshot_id": "snap_1",
                    "repo_id": "different_repo",
                    "tree_hash": "correct_hash",
                    "files": {"a.py": hashlib.sha256(b"print(1)").hexdigest()},
                },
            ),
            "/tmp/extract",
            "repo_1",
            "snap_1",
            "correct_hash",
        )


@pytest.mark.asyncio
async def test_worker_synthetic_baseline_commit():
    """MX-15: Baseline commit configures git identity, excludes hooks bypass, commits overlays, and verifies clean status."""
    from worker.service import WorkerFastFailError

    with tempfile.TemporaryDirectory() as tmpdir:
        subprocess.run(["git", "init"], cwd=tmpdir, capture_output=True, check=True)
        Path(tmpdir, "AGENTS.md").write_text("agents overlay", encoding="utf-8")
        Path(tmpdir, "PRD.md").write_text("prd overlay", encoding="utf-8")
        Path(tmpdir, "src.py").write_text("print(1)", encoding="utf-8")

        worker = create_mock_worker()
        original_run = subprocess.run
        hook_bypass_verified = False
        identity_set_verified = False

        def mock_run(args, **kwargs):
            nonlocal hook_bypass_verified, identity_set_verified
            if "config" in args and "core.hooksPath" in args and "/dev/null" in args:
                hook_bypass_verified = True
            if "config" in args and ("user.name" in args or "user.email" in args):
                identity_set_verified = True
            return original_run(args, **kwargs)

        with patch("subprocess.run", side_effect=mock_run):
            await worker.commit_synthetic_baseline(tmpdir)

        assert hook_bypass_verified is True
        assert identity_set_verified is True

        res = subprocess.run(
            ["git", "status", "--porcelain"], cwd=tmpdir, capture_output=True, text=True
        )
        assert res.stdout.strip() == ""

        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "git")):
            with pytest.raises(WorkerFastFailError):
                await worker.commit_synthetic_baseline(tmpdir)


@pytest.mark.asyncio
async def test_worker_live_snapshot_download_blocks_git_commondir_filter_exploit():
    """Live Aether base-head materialization rejects .git/commondir, .gitattributes, and DOS 8.3 aliases."""
    from shared.utils.git import GitRepo
    from worker.service import WorkerFastFailError

    worker = create_mock_worker()
    with tempfile.TemporaryDirectory() as tmpdir:
        sentinel_file = Path(tmpdir) / "git-filter-pwned"
        workspace_root = str(Path(tmpdir) / "workspace")
        os.makedirs(workspace_root, exist_ok=True)
        subprocess.run(["git", "init"], cwd=workspace_root, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@mx.local"],
            cwd=workspace_root,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=workspace_root,
            capture_output=True,
            check=True,
        )
        Path(workspace_root, "README.md").write_text("seed\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=workspace_root, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "seed"],
            cwd=workspace_root,
            capture_output=True,
            check=True,
        )

        # Build malicious tarball with .git/commondir + filter config + .gitattributes
        malicious_tar = io.BytesIO()
        with tarfile.open(fileobj=malicious_tar, mode="w:gz") as tar:
            entries = {
                ".git/commondir": b"common\n",
                ".git/common/config": (
                    f'[filter "pwn"]\n\tclean = touch {sentinel_file}\n'.encode()
                ),
                ".gitattributes": b"*.py filter=pwn\n",
                "src/loglens/runtime.py": b"print('hi')\n",
            }
            for arc_name, payload in entries.items():
                info = tarfile.TarInfo(name=arc_name)
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))

        repo = GitRepo(repo_url=workspace_root, local_path=workspace_root, token=None)
        with pytest.raises(WorkerFastFailError, match="Disallowed VCS metadata segment"):
            await worker.materialize_aether_base_head(
                repo=repo,
                actual_head="snap_malicious",
                workspace_root=workspace_root,
                tar_bytes_override=malicious_tar.getvalue(),
            )
        assert not sentinel_file.exists()
        assert not (Path(workspace_root) / ".git" / "commondir").exists()

        # Also test DOS 8.3 shortname alias GIT~1/config
        alias_tar = io.BytesIO()
        with tarfile.open(fileobj=alias_tar, mode="w:gz") as tar:
            info = tarfile.TarInfo(name="GIT~1/config")
            payload = b"[core]\n"
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))

        with pytest.raises(WorkerFastFailError, match="Disallowed VCS metadata segment"):
            await worker.materialize_aether_base_head(
                repo=repo,
                actual_head="snap_alias",
                workspace_root=workspace_root,
                tar_bytes_override=alias_tar.getvalue(),
            )


@pytest.mark.asyncio
async def test_snapshot_verifier_does_not_execute_in_parent_or_leak_env():
    """Verifier rejects top-level side effects, never executes in parent, and strips env in isolated child."""
    run_aether_semantic_canary = get_canary()

    with tempfile.TemporaryDirectory() as tmpdir:
        sentinel_path = Path(tmpdir) / "verifier-exec-pwned"
        malicious_top_level = f'''
open({str(sentinel_path)!r}, "w").write("pwned")

class CommandResult:
    def alpha_method(self) -> str:
        return "alpha"
    def beta_method(self) -> str:
        return "beta"
'''
        with patch("scripts.run_aether_semantic_canary._http_request") as mock_req:
            mock_req.return_value = {"content": malicious_top_level}
            args = MagicMock()
            args.require_behavior_check = True
            rc = await run_aether_semantic_canary.verify_snapshot_markers(
                args, "repo1", "snap1", "src/loglens/runtime.py"
            )
            assert rc == 1
        assert not sentinel_path.exists()

        # Also test a module that tries to read environment secrets inside a method
        malicious_env_leak = '''
import os

class CommandResult:
    def alpha_method(self) -> str:
        return os.environ.get("MX_SUPER_SECRET_SENTINEL", "alpha")
    def beta_method(self) -> str:
        return "beta"
'''
        with patch.dict(os.environ, {"MX_SUPER_SECRET_SENTINEL": "leaked_secret"}):
            ok, details = run_aether_semantic_canary.inspect_merged_runtime_ast_and_behavior(
                malicious_env_leak,
                require_behavior=True,
            )
            assert ok is False
            assert "Forbidden import" in str(details.get("error", ""))


def test_canary_fail_closed_on_missing_required_gates_or_failed_promotion_fetch():
    """verify_checks_executed fails closed when required gates or expected promotions are missing."""
    run_aether_semantic_canary = get_canary()
    valid_ledger = [
        {"name": name, "status": "PASS", "passed": True}
        for name in run_aether_semantic_canary.REQUIRED_CANARY_CHECKS
    ]

    # Missing build, unit_tests, lint (only syntax: PASS)
    partial_promo = [{"id": "prom_1", "evidence": json.dumps({"syntax": "PASS"})}]
    ok, reason = run_aether_semantic_canary.verify_checks_executed(
        valid_ledger, promotion_records=partial_promo
    )
    assert ok is False
    assert "missing required gates" in reason.lower()

    # Missing expected promotion ID
    full_promo = [
        {
            "id": "prom_1",
            "snapshot_id": "snap_1",
            "evidence": json.dumps(
                {"syntax": "PASS", "build": "PASS", "unit_tests": "PASS", "lint": "PASS"}
            ),
        }
    ]
    ok, reason = run_aether_semantic_canary.verify_checks_executed(
        valid_ledger,
        promotion_records=full_promo,
        expected_promotion_ids=["prom_1", "prom_2", "prom_3"],
    )
    assert ok is False
    assert "missing expected promotion records" in reason.lower()

    # Failed promotion detail fetch included as error record
    failed_fetch_promo = [
        *full_promo,
        {"id": "prom_2", "error": "HTTP 500"},
    ]
    ok, reason = run_aether_semantic_canary.verify_checks_executed(
        valid_ledger,
        promotion_records=failed_fetch_promo,
        expected_promotion_ids=["prom_1", "prom_2"],
    )
    assert ok is False
    assert "failed or invalid promotion detail record" in reason.lower()

    # unit_tests gate row with tests_passed=None fails closed
    ok, reason = run_aether_semantic_canary.verify_checks_executed(
        valid_ledger,
        promotion_records=full_promo,
        expected_promotion_ids=["prom_1"],
        gate_results_rows=[
            {
                "id": "gate_1",
                "snapshot_id": "snap_1",
                "gate_name": "unit_tests",
                "status": "PASS",
                "tests_passed": None,
            }
        ],
    )
    assert ok is False
    assert "tests_passed" in reason


def test_bundle_hygiene_scanner_rejects_unlisted_files_and_local_paths():
    """Bundle hygiene scanner rejects unlisted files, unrelated repos/tables, and local paths."""
    import sqlite3

    from scripts.check_release_hygiene import scan_evidence_bundle_dir

    run_aether_semantic_canary = get_canary()
    user_root = "/" + "Users/alice"
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_db = Path(tmpdir) / "raw_aether.db"
        conn = sqlite3.connect(str(raw_db))
        conn.execute("CREATE TABLE aether_repositories (id TEXT, name TEXT)")
        conn.execute("INSERT INTO aether_repositories VALUES ('repo_canary', 'canary-repo')")
        conn.execute("INSERT INTO aether_repositories VALUES ('other_repo', 'private-repo')")
        conn.execute(
            "CREATE TABLE aether_proposals (id TEXT, repo_id TEXT, source_run_id TEXT, rationale TEXT)"
        )
        conn.execute(
            "INSERT INTO aether_proposals VALUES ('prop_1', 'repo_canary', 'run_canary', 'ok')"
        )
        conn.execute(
            "INSERT INTO aether_proposals VALUES ('prop_secret', 'other_repo', 'other_run', 'PRIVATE_REPO_MARKER')"
        )
        conn.execute(
            "CREATE TABLE aether_snapshots (id TEXT, repo_id TEXT, storage_path TEXT)"
        )
        conn.execute(
            "INSERT INTO aether_snapshots VALUES (?, ?, ?)",
            ("snap_1", "repo_canary", f"{user_root}/projects/storage/snap_1"),
        )
        conn.execute(
            "INSERT INTO aether_snapshots VALUES (?, ?, ?)",
            ("snap_secret", "other_repo", "PRIVATE_REPO_MARKER"),
        )
        conn.execute(
            "CREATE TABLE aether_gate_results (id TEXT, repo_id TEXT, snapshot_id TEXT, gate_command TEXT)"
        )
        conn.execute(
            "INSERT INTO aether_gate_results VALUES (?, ?, ?, ?)",
            ("gate_1", "repo_canary", "snap_1", "/private/var/folders/ab/cd/T/run.sh"),
        )
        conn.execute("CREATE TABLE unrelated_custom_table (secret TEXT)")
        conn.execute("INSERT INTO unrelated_custom_table VALUES ('PRIVATE_TABLE_MARKER')")
        conn.commit()
        conn.close()

        bundle_dir = Path(tmpdir) / "bundle"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        run_aether_semantic_canary._sanitize_and_export_sqlite_db(
            raw_db,
            bundle_dir / "aether.db",
            repo_id="repo_canary",
            run_id="run_canary",
        )
        (bundle_dir / "merged_runtime.py").write_text("class CommandResult:\n    pass\n", encoding="utf-8")
        (bundle_dir / "final_canary_evidence.json").write_text(
            json.dumps({"repo_id": "repo_canary", "run_id": "run_canary"}) + "\n",
            encoding="utf-8",
        )
        (bundle_dir / "manifest.json").write_text(
            json.dumps({"repo_id": "repo_canary", "run_id": "run_canary"}) + "\n",
            encoding="utf-8",
        )
        (bundle_dir / "telemetry.log").write_text("{}\n", encoding="utf-8")
        (bundle_dir / "SHA256SUMS").write_text("abc  merged_runtime.py\n", encoding="utf-8")

        findings = scan_evidence_bundle_dir(bundle_dir)
        assert findings == []
        exported_bytes = (bundle_dir / "aether.db").read_bytes()
        assert user_root.encode("utf-8") not in exported_bytes
        assert b"/var/folders/" not in exported_bytes
        assert b"PRIVATE_REPO_MARKER" not in exported_bytes
        assert b"PRIVATE_TABLE_MARKER" not in exported_bytes

        # Injecting an unrelated repo row directly into exported aether.db must be flagged by hygiene scan
        conn_exp = sqlite3.connect(str(bundle_dir / "aether.db"))
        conn_exp.execute("INSERT INTO aether_repositories VALUES ('other_repo', 'leaked')")
        conn_exp.commit()
        conn_exp.close()
        findings_unrelated_repo = scan_evidence_bundle_dir(bundle_dir)
        assert any(f.get("kind") == "sqlite_unrelated_repository" for f in findings_unrelated_repo)

        # Clean up the injected row and test unlisted file rejection
        conn_exp = sqlite3.connect(str(bundle_dir / "aether.db"))
        conn_exp.execute("DELETE FROM aether_repositories WHERE id = 'other_repo'")
        conn_exp.commit()
        conn_exp.close()

        (bundle_dir / "aether_server.log").write_text(f"Started from {user_root}/venv\n", encoding="utf-8")
        findings_with_unlisted = scan_evidence_bundle_dir(bundle_dir)
        kinds = {str(f["kind"]) for f in findings_with_unlisted}
        assert "unlisted_bundle_file" in kinds
        assert "bundle_private_path" in kinds


def test_p1_verifier_blocks_io_fileio_escape_probe():
    """[P1] Snapshot code using _io.FileIO inside alpha_method cannot write outside the verifier."""
    run_aether_semantic_canary = get_canary()

    with tempfile.TemporaryDirectory() as tmpdir:
        escape_marker = Path(tmpdir) / "fileio-escape-marker.txt"
        probe_source = f'''from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class CommandResult:
    plain_lines: list[str]
    rows: list[dict[str, object]]
    columns: tuple[str, ...]

    def alpha_method(self) -> str:
        import _io
        try:
            with _io.FileIO({str(escape_marker)!r}, "w") as fh:
                fh.write(b"escaped")
        except BaseException:
            pass
        return "alpha"

    def beta_method(self) -> str:
        return "beta"
'''
        # 1. Full AST + behavior check blocks _io import at AST level
        ok_ast, _details_ast = run_aether_semantic_canary.inspect_merged_runtime_ast_and_behavior(
            probe_source,
            require_behavior=True,
            snapshot_files={"src/loglens/__init__.py": "", "src/loglens/runtime.py": probe_source},
        )
        assert ok_ast is False
        assert not escape_marker.exists()

        # 2. Direct child behavior verifier blocks _io.FileIO via OS Seatbelt + C audit hook even if AST check is bypassed
        ok_beh, _outputs_beh, _err_beh = run_aether_semantic_canary._run_isolated_snapshot_behavior_check(
            probe_source,
            snapshot_files={"src/loglens/__init__.py": "", "src/loglens/runtime.py": probe_source},
        )
        assert ok_beh is False
        assert not escape_marker.exists()


def test_p1_behavior_verifier_uses_only_materialized_snapshot_files():
    """[P1] Behavior verifier fails if loglens.shared.formatting is missing from snapshot_files and passes when included."""
    run_aether_semantic_canary = get_canary()

    runtime_with_shared_import = '''"""Shared command result and rendering helpers."""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from loglens.shared import formatting

OUTPUT_FORMATS = ("plain", "table", "json", "csv")


@dataclass(frozen=True)
class CommandResult:
    """Normalized command payload for every CLI feature."""

    plain_lines: list[str]
    columns: list[str]
    rows: list[dict[str, object]]

    def alpha_method(self) -> str:
        return "alpha"

    def beta_method(self) -> str:
        return "beta"


def add_output_format_option(parser: argparse.ArgumentParser) -> None:
    """Attach the shared output-format option to a command parser."""
    parser.add_argument(
        "--format",
        choices=OUTPUT_FORMATS,
        default="plain",
        help="Output format for the command result.",
    )


def render_result(result: CommandResult, *, output_format: str) -> str:
    """Render a command result in the requested output format."""
    if output_format == "plain":
        return formatting.join_lines(result.plain_lines)

    formatter = getattr(formatting, f"render_{output_format}", None)
    if formatter is None:
        raise NotImplementedError(f"output format '{output_format}' is not available yet")
    return formatter(result.rows, result.columns)
'''
    seed_formatting = (
        '"""Shared output formatting surface for plain, table, JSON, and CSV renderers."""\n\n\n'
        'def join_lines(lines: list[str]) -> str:\n'
        '    """Render plain-text command output from preformatted lines."""\n'
        '    return "\\n".join(lines)\n'
    )
    # Without src/loglens/shared/formatting.py in snapshot_files, import must fail (no fallback to MX checkout)
    ok_missing, details_missing = run_aether_semantic_canary.inspect_merged_runtime_ast_and_behavior(
        runtime_with_shared_import,
        require_behavior=True,
        snapshot_files={
            "src/loglens/__init__.py": "",
            "src/loglens/runtime.py": runtime_with_shared_import,
        },
    )
    assert ok_missing is False
    assert "No module named 'loglens.shared'" in str(details_missing.get("error", ""))

    # With complete package files in snapshot_files, isolated behavior check succeeds
    ok_complete, details_complete = run_aether_semantic_canary.inspect_merged_runtime_ast_and_behavior(
        runtime_with_shared_import,
        require_behavior=True,
        snapshot_files={
            "src/loglens/__init__.py": "",
            "src/loglens/shared/__init__.py": "",
            "src/loglens/shared/formatting.py": seed_formatting,
            "src/loglens/runtime.py": runtime_with_shared_import,
        },
    )
    assert ok_complete is True
    assert details_complete["behavior_outputs"]["alpha_method"] == "alpha"
    assert details_complete["behavior_outputs"]["beta_method"] == "beta"


def test_p1_output_dir_validation_never_deletes_existing_files():
    """[P1] _validate_output_dir rejects non-empty output directories without deleting existing files."""
    run_aether_semantic_canary = get_canary()

    with tempfile.TemporaryDirectory() as tmpdir:
        non_empty_dir = Path(tmpdir) / "user_data"
        non_empty_dir.mkdir()
        keep_file = non_empty_dir / "keep.txt"
        keep_subdir = non_empty_dir / "subdir"
        keep_subdir.mkdir()
        keep_file.write_text("precious data", encoding="utf-8")
        (keep_subdir / "nested.txt").write_text("nested precious data", encoding="utf-8")

        ok, err = run_aether_semantic_canary._validate_output_dir(non_empty_dir)
        assert ok is False
        assert "Refusing non-empty output directory" in err
        assert keep_file.exists()
        assert (keep_subdir / "nested.txt").exists()

        # Empty or not-yet-created directory is accepted
        fresh_dir = Path(tmpdir) / "fresh_bundle"
        ok_fresh, err_fresh = run_aether_semantic_canary._validate_output_dir(fresh_dir)
        assert ok_fresh is True
        assert err_fresh == "OK"


@pytest.mark.asyncio
async def test_p2_materialize_aether_base_head_removes_obsolete_files():
    """[P2] Materializing a 1-file Aether snapshot over a clone containing obsolete.py removes obsolete.py from worktree and HEAD."""
    from shared.utils.git import GitRepo

    worker = create_mock_worker()
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_root = str(Path(tmpdir) / "workspace")
        os.makedirs(workspace_root, exist_ok=True)
        subprocess.run(["git", "init"], cwd=workspace_root, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@mx.local"],
            cwd=workspace_root,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=workspace_root,
            capture_output=True,
            check=True,
        )
        Path(workspace_root, "obsolete.py").write_text("stale = True\n", encoding="utf-8")
        Path(workspace_root, "src").mkdir(parents=True, exist_ok=True)
        Path(workspace_root, "src", "old_module.py").write_text("old = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=workspace_root, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "initial clone with obsolete files"],
            cwd=workspace_root,
            capture_output=True,
            check=True,
        )

        # Build a 1-file snapshot tarball containing only src/only_file.py
        snap_tar = io.BytesIO()
        with tarfile.open(fileobj=snap_tar, mode="w:gz") as tar:
            payload = b"only_file = True\n"
            info = tarfile.TarInfo(name="src/only_file.py")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))

        repo = GitRepo(repo_url=workspace_root, local_path=workspace_root, token=None)
        await worker.materialize_aether_base_head(
            repo=repo,
            actual_head="snap_one_file",
            workspace_root=workspace_root,
            tar_bytes_override=snap_tar.getvalue(),
        )

        assert not Path(workspace_root, "obsolete.py").exists()
        assert not Path(workspace_root, "src", "old_module.py").exists()
        assert Path(workspace_root, "src", "only_file.py").exists()
        ls_res = await repo._run_local_git(["ls-files"])
        tracked = {line.strip() for line in ls_res.stdout.splitlines() if line.strip()}
        assert tracked == {"src/only_file.py"}


@pytest.mark.asyncio
async def test_p2_git_repo_add_preserves_tracked_gitattributes_and_gitmodules():
    """[P2] Routine GitRepo.add() preserves tracked .gitattributes and .gitmodules while restoring them around untrusted hooks."""
    from shared.utils.git import GitRepo

    with tempfile.TemporaryDirectory() as tmpdir:
        subprocess.run(["git", "init"], cwd=tmpdir, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@mx.local"],
            cwd=tmpdir,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=tmpdir,
            capture_output=True,
            check=True,
        )
        gitattr = Path(tmpdir) / ".gitattributes"
        gitmod = Path(tmpdir) / ".gitmodules"
        gitattr.write_text("* text=auto\n", encoding="utf-8")
        gitmod.write_text("# submodules\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=tmpdir, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "track gitattributes and gitmodules"],
            cwd=tmpdir,
            capture_output=True,
            check=True,
        )

        repo = GitRepo(repo_url=tmpdir, local_path=tmpdir, token=None)
        Path(tmpdir, "app.py").write_text("x = 1\n", encoding="utf-8")
        await repo.add(["app.py"])

        assert gitattr.exists()
        assert gitmod.exists()
        status = await repo.get_status()
        assert ".gitattributes" not in status.stdout
        assert ".gitmodules" not in status.stdout


def test_p2_verify_checks_executed_requires_unit_tests_row_per_promotion_snapshot():
    """[P2] verify_checks_executed fails when 3 promotions share only 1 unit_tests gate row."""
    run_aether_semantic_canary = get_canary()
    valid_ledger = [
        {"name": name, "status": "PASS", "passed": True}
        for name in run_aether_semantic_canary.REQUIRED_CANARY_CHECKS
    ]
    all_gates_evidence = json.dumps(
        {"syntax": "PASS", "build": "PASS", "unit_tests": "PASS", "lint": "PASS"}
    )
    three_promos = [
        {"id": "prom_1", "snapshot_id": "snap_1", "evidence": all_gates_evidence},
        {"id": "prom_2", "snapshot_id": "snap_2", "evidence": all_gates_evidence},
        {"id": "prom_3", "snapshot_id": "snap_3", "evidence": all_gates_evidence},
    ]

    # Only 1 unit_tests row in gate_results_rows (missing snap_2 and snap_3)
    one_row_only = [
        {
            "id": "gate_1",
            "snapshot_id": "snap_1",
            "gate_name": "unit_tests",
            "status": "PASS",
            "tests_passed": 1,
        }
    ]
    ok_one, reason_one = run_aether_semantic_canary.verify_checks_executed(
        valid_ledger,
        promotion_records=three_promos,
        expected_promotion_ids=["prom_1", "prom_2", "prom_3"],
        gate_results_rows=one_row_only,
        require_gate_results_rows=True,
    )
    assert ok_one is False
    assert "missing a passing unit_tests row" in reason_one

    # All 3 promotions have matching passing unit_tests rows with tests_passed > 0
    three_rows = [
        {
            "id": f"gate_{i}",
            "snapshot_id": f"snap_{i}",
            "gate_name": "unit_tests",
            "status": "PASS",
            "tests_passed": 1,
        }
        for i in (1, 2, 3)
    ]
    ok_all, reason_all = run_aether_semantic_canary.verify_checks_executed(
        valid_ledger,
        promotion_records=three_promos,
        expected_promotion_ids=["prom_1", "prom_2", "prom_3"],
        gate_results_rows=three_rows,
        require_gate_results_rows=True,
    )
    assert ok_all is True
    assert "All 10 executed checks and promotion gates passed" in reason_all


def test_p1_verifier_blocks_external_sqlite_read_and_non_darwin_fails_closed():
    """[P1] Snapshot dependency cannot read an external SQLite database, and non-Darwin platform fails closed."""
    import sqlite3

    run_aether_semantic_canary = get_canary()
    with tempfile.TemporaryDirectory() as tmpdir:
        ext_db = Path(tmpdir) / "external_secret.db"
        conn = sqlite3.connect(str(ext_db))
        conn.execute("CREATE TABLE secrets (val TEXT)")
        conn.execute("INSERT INTO secrets VALUES ('alpha')")
        conn.commit()
        conn.close()

        malicious_formatting = f"""import sqlite3
conn = sqlite3.connect({str(ext_db)!r})
LEAKED = conn.execute("SELECT val FROM secrets").fetchone()[0]

def join_lines(lines: list[str]) -> str:
    return "\\n".join(lines)
"""
        runtime_using_formatting = """from __future__ import annotations
from dataclasses import dataclass
from loglens.shared import formatting

@dataclass(frozen=True)
class CommandResult:
    plain_lines: list[str]
    columns: list[str]
    rows: list[dict[str, object]]

    def alpha_method(self) -> str:
        return "alpha"

    def beta_method(self) -> str:
        return "beta"
"""
        snap_files = {
            "src/loglens/__init__.py": "",
            "src/loglens/shared/__init__.py": "",
            "src/loglens/shared/formatting.py": malicious_formatting,
            "src/loglens/runtime.py": runtime_using_formatting,
        }
        # 1. Parent AST + file verification rejects sqlite3 in snapshot dependency
        ok_ast, details_ast = run_aether_semantic_canary.inspect_merged_runtime_ast_and_behavior(
            runtime_using_formatting,
            require_behavior=True,
            snapshot_files=snap_files,
        )
        assert ok_ast is False
        assert "sqlite3" in str(details_ast.get("error", ""))

        # 2. Direct child subprocess rejects external SQLite read via Seatbelt + C audit hook
        ok_child, _out_child, err_child = run_aether_semantic_canary._run_isolated_snapshot_behavior_check(
            runtime_using_formatting,
            snapshot_files=snap_files,
        )
        assert ok_child is False
        assert err_child is not None

        # 3. Non-Darwin platform fails closed without executing unsandboxed child
        with patch.object(run_aether_semantic_canary.sys, "platform", "linux"):
            ok_linux, _out_linux, err_linux = run_aether_semantic_canary._run_isolated_snapshot_behavior_check(
                runtime_using_formatting,
                snapshot_files={
                    "src/loglens/__init__.py": "",
                    "src/loglens/shared/__init__.py": "",
                    "src/loglens/shared/formatting.py": "def join_lines(lines):\n    return '\\n'.join(lines)\n",
                    "src/loglens/runtime.py": runtime_using_formatting,
                },
            )
            assert ok_linux is False
            assert "sandbox-exec is unavailable" in str(err_linux)


def test_p1_verifier_blocks_forged_behavioral_pass_via_init_stdout_and_exit():
    """[P1] Snapshot cannot forge a behavioral PASS by emitting JSON from __init__.py while alpha_method raises."""
    run_aether_semantic_canary = get_canary()

    forged_init = (
        'import json, sys\n'
        'sys.stdout.write(json.dumps({"ok": True, "outputs": {"alpha_method": "alpha", "beta_method": "beta"}}))\n'
        'sys.stdout.flush()\n'
        'sys.exit(0)\n'
    )
    broken_runtime = """from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class CommandResult:
    plain_lines: list[str]
    columns: list[str]
    rows: list[dict[str, object]]

    def alpha_method(self) -> str:
        raise RuntimeError("alpha_method is broken")
        return "alpha"

    def beta_method(self) -> str:
        return "beta"
"""
    snap_files = {
        "src/loglens/__init__.py": forged_init,
        "src/loglens/runtime.py": broken_runtime,
    }
    ok_ast, details_ast = run_aether_semantic_canary.inspect_merged_runtime_ast_and_behavior(
        broken_runtime,
        require_behavior=True,
        snapshot_files=snap_files,
    )
    assert ok_ast is False
    assert details_ast["behavior_verified"] is False

    # Direct child call also fails because stdout is redirected to /dev/null and out_fd requires per-run nonce
    ok_child, _out_child, _err_child = run_aether_semantic_canary._run_isolated_snapshot_behavior_check(
        broken_runtime,
        snapshot_files=snap_files,
    )
    assert ok_child is False


def test_p1_sqlite_export_and_hygiene_omit_shared_agent_metrics_table():
    """[P1] Public SQLite export drops aether_agent_metrics so unrelated scopes for shared agent_id are never retained."""
    import sqlite3

    from scripts.check_release_hygiene import scan_evidence_bundle_dir

    run_aether_semantic_canary = get_canary()
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_db = Path(tmpdir) / "raw_aether.db"
        conn = sqlite3.connect(str(raw_db))
        conn.execute("CREATE TABLE aether_repositories (id TEXT, name TEXT)")
        conn.execute("INSERT INTO aether_repositories VALUES ('repo_canary', 'canary-repo')")
        conn.execute(
            "CREATE TABLE aether_proposals (id TEXT, repo_id TEXT, source_run_id TEXT, agent_id TEXT)"
        )
        conn.execute(
            "INSERT INTO aether_proposals VALUES ('prop_1', 'repo_canary', 'run_canary', 'worker-1')"
        )
        conn.execute(
            "CREATE TABLE aether_agent_metrics (agent_id TEXT, scope TEXT, proposals_submitted INTEGER)"
        )
        conn.execute(
            "INSERT INTO aether_agent_metrics VALUES ('worker-1', 'repo_canary', 1)"
        )
        conn.execute(
            "INSERT INTO aether_agent_metrics VALUES ('worker-1', 'PRIVATE_UNRELATED_SCOPE', 99)"
        )
        conn.commit()
        conn.close()

        bundle_dir = Path(tmpdir) / "bundle"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        exported_db = bundle_dir / "aether.db"
        run_aether_semantic_canary._sanitize_and_export_sqlite_db(
            raw_db,
            exported_db,
            repo_id="repo_canary",
            run_id="run_canary",
        )
        assert b"PRIVATE_UNRELATED_SCOPE" not in exported_db.read_bytes()

        conn_check = sqlite3.connect(str(exported_db))
        tables = {
            r[0]
            for r in conn_check.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn_check.close()
        assert "aether_agent_metrics" not in tables

        (bundle_dir / "merged_runtime.py").write_text("class CommandResult:\n    pass\n", encoding="utf-8")
        (bundle_dir / "final_canary_evidence.json").write_text(
            json.dumps({"repo_id": "repo_canary", "run_id": "run_canary"}) + "\n",
            encoding="utf-8",
        )
        (bundle_dir / "manifest.json").write_text(
            json.dumps({"repo_id": "repo_canary", "run_id": "run_canary"}) + "\n",
            encoding="utf-8",
        )
        (bundle_dir / "telemetry.log").write_text("{}\n", encoding="utf-8")
        (bundle_dir / "SHA256SUMS").write_text("abc  merged_runtime.py\n", encoding="utf-8")
        assert scan_evidence_bundle_dir(bundle_dir) == []

        # Re-injecting aether_agent_metrics into exported DB must be flagged by hygiene scan
        conn_inj = sqlite3.connect(str(exported_db))
        conn_inj.execute("CREATE TABLE aether_agent_metrics (agent_id TEXT, scope TEXT)")
        conn_inj.execute("INSERT INTO aether_agent_metrics VALUES ('worker-1', 'other_scope')")
        conn_inj.commit()
        conn_inj.close()
        findings = scan_evidence_bundle_dir(bundle_dir)
        assert any(f.get("kind") == "sqlite_unlisted_table" for f in findings)


@pytest.mark.asyncio
async def test_p2_extract_snapshot_securely_rejects_symlink_root_and_unowned_cleanup():
    """[P2] extract_snapshot_securely rejects symlinked extract_dir before resolve() and refuses unowned clean_destination=True."""
    from worker.service import WorkerFastFailError

    worker = create_mock_worker()
    snap_tar = io.BytesIO()
    with tarfile.open(fileobj=snap_tar, mode="w:gz") as tar:
        payload = b"x = 1\n"
        info = tarfile.TarInfo(name="src/mod.py")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))

    with tempfile.TemporaryDirectory() as tmpdir:
        victim_dir = Path(tmpdir) / "victim_dir"
        victim_dir.mkdir()
        precious_marker = victim_dir / "precious.txt"
        precious_marker.write_text("do not delete", encoding="utf-8")

        symlink_dest = Path(tmpdir) / "symlink_extract"
        symlink_dest.symlink_to(victim_dir)

        with pytest.raises(WorkerFastFailError, match="symlink"):
            await worker.extract_snapshot_securely(
                snap_tar.getvalue(),
                symlink_dest,
                repo_id="repo1",
                snapshot_id="snap1",
                require_manifest=False,
            )
        assert precious_marker.exists()

        # Calling clean_destination=True on an unowned non-git directory is also refused
        with pytest.raises(WorkerFastFailError, match="Refusing destructive snapshot cleanup"):
            await worker.extract_snapshot_securely(
                snap_tar.getvalue(),
                victim_dir,
                repo_id="repo1",
                snapshot_id="snap1",
                require_manifest=False,
                clean_destination=True,
            )
        assert precious_marker.exists()


@pytest.mark.asyncio
async def test_p2_materialize_aether_base_head_supports_unicode_filenames():
    """[P2] materialize_aether_base_head succeeds with UTF-8 filenames like café.py using git ls-files -z."""
    from shared.utils.git import GitRepo

    worker = create_mock_worker()
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_root = str(Path(tmpdir) / "workspace")
        os.makedirs(workspace_root, exist_ok=True)
        subprocess.run(["git", "init"], cwd=workspace_root, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@mx.local"],
            cwd=workspace_root,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=workspace_root,
            capture_output=True,
            check=True,
        )
        Path(workspace_root, "init.py").write_text("x = 0\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=workspace_root, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=workspace_root, capture_output=True, check=True)

        snap_tar = io.BytesIO()
        with tarfile.open(fileobj=snap_tar, mode="w:gz") as tar:
            payload = "café = True\n".encode()
            info = tarfile.TarInfo(name="src/café.py")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))

        repo = GitRepo(repo_url=workspace_root, local_path=workspace_root, token=None)
        await worker.materialize_aether_base_head(
            repo=repo,
            actual_head="snap_unicode",
            workspace_root=workspace_root,
            tar_bytes_override=snap_tar.getvalue(),
        )
        assert Path(workspace_root, "src", "café.py").exists()
        assert not Path(workspace_root, "init.py").exists()


@pytest.mark.asyncio
async def test_p2_verify_promotion_membership_rejects_unexpected_excluded_proposals():
    """[P2] verify_promotion_membership fails closed if excluded_proposals is non-empty."""
    run_aether_semantic_canary = get_canary()
    with patch("scripts.run_aether_semantic_canary._http_request") as mock_req:
        mock_req.return_value = {
            "included_proposals": '["prop1", "prop2"]',
            "excluded_proposals": '["prop_excluded"]',
        }
        args = MagicMock()
        rc = await run_aether_semantic_canary.verify_promotion_membership(
            args, "repo1", "promo1", ["prop1", "prop2"]
        )
        assert rc == 1

        mock_req.return_value = {
            "included_proposals": '["prop1", "prop2"]',
            "excluded_proposals": "[]",
        }
        rc_ok = await run_aether_semantic_canary.verify_promotion_membership(
            args, "repo1", "promo1", ["prop1", "prop2"]
        )
        assert rc_ok == 0


def test_p1_verifier_rejects_executable_runtime_forgery_and_reassignment_probe():
    """[P1] Parent rejects runtime.py payloads that reassign CommandResult or access child driver state via getattr."""
    run_aether_semantic_canary = get_canary()

    forgery_probe = '''
import argparse

class CommandResult:
    def alpha_method(self):
        return "alpha"
    def beta_method(self):
        return "beta"

CommandResult = None
system = getattr(argparse, "_sys")
driver = system.modules["__main__"]
state = getattr(driver, "__dict__")
serializer = state["json"]
operating = state["os"]
result = {"ok": True, "nonce": state["payload"]["nonce"], "outputs": {"alpha_method": "alpha", "beta_method": "beta"}}
sent = getattr(operating, "write")(state["out_fd"], serializer.dumps(result).encode("utf-8"))
ended = getattr(operating, "_exit")(0)
'''
    parent_err = run_aether_semantic_canary._verify_snapshot_files_and_methods(forgery_probe, None)
    assert parent_err is not None

    with patch("scripts.run_aether_semantic_canary.subprocess.run") as mock_subproc:
        ok, details = run_aether_semantic_canary.inspect_merged_runtime_ast_and_behavior(
            forgery_probe,
            require_behavior=True,
        )
        assert ok is False
        assert details["behavior_verified"] is False
        mock_subproc.assert_not_called()

        ok_child, _out_child, err_child = run_aether_semantic_canary._run_isolated_snapshot_behavior_check(
            forgery_probe,
        )
        assert ok_child is False
        assert err_child is not None
        mock_subproc.assert_not_called()

    # Also test a valid merged runtime.py with an extra top-level reassignment `CommandResult = None`
    seed_runtime = (
        Path(run_aether_semantic_canary.project_root)
        / "qualification"
        / "seed_repos"
        / "loglens"
        / "src"
        / "loglens"
        / "runtime.py"
    ).read_text(encoding="utf-8")
    reassigned_runtime = seed_runtime.replace(
        "    rows: list[dict[str, object]]\n",
        "    rows: list[dict[str, object]]\n\n    def alpha_method(self) -> str:\n        return 'alpha'\n\n    def beta_method(self) -> str:\n        return 'beta'\n",
    ) + "\nCommandResult = None\n"

    with patch("scripts.run_aether_semantic_canary.subprocess.run") as mock_subproc2:
        ok_reassign, details_reassign = run_aether_semantic_canary.inspect_merged_runtime_ast_and_behavior(
            reassigned_runtime,
            require_behavior=True,
        )
        assert ok_reassign is False
        assert details_reassign["behavior_verified"] is False
        assert "differs from trusted loglens seed" in str(details_reassign.get("error", ""))
        mock_subproc2.assert_not_called()




