import subprocess
from pathlib import Path

from scripts.run_aether_live_proof import (
    COPY_EXCLUDES,
    COPY_INCLUDE_FILES,
    COPY_INCLUDE_ROOTS,
    _classify_live_failure,
    _copy_worktree,
    _landed_paths_source,
    _materialize_worktree,
)


def _checkpoint(reached: bool) -> dict[str, object]:
    return {
        "reached": reached,
        "timestamp": None,
        "status": None,
        "attributes": {},
    }


def test_classify_live_failure_identifies_model_execution_gap() -> None:
    checkpoints = {
        "task_selected": _checkpoint(True),
        "model_bypassed_canned": _checkpoint(False),
        "model_started": _checkpoint(True),
        "first_token_received": _checkpoint(False),
        "model_timeout": _checkpoint(False),
        "model_completed": _checkpoint(False),
        "submit_started": _checkpoint(False),
        "submit_completed": _checkpoint(False),
        "proposal_id_received": _checkpoint(False),
        "proposal_visible": _checkpoint(False),
        "terminal_status_seen": _checkpoint(False),
    }

    failure = _classify_live_failure(
        checkpoints,
        proposal_visible=False,
        terminal_status=None,
    )

    assert failure == "model_execution_failed"


def test_classify_live_failure_identifies_proposal_visibility_gap() -> None:
    checkpoints = {
        "task_selected": _checkpoint(True),
        "model_bypassed_canned": _checkpoint(False),
        "model_started": _checkpoint(True),
        "first_token_received": _checkpoint(True),
        "model_timeout": _checkpoint(False),
        "model_completed": _checkpoint(True),
        "submit_started": _checkpoint(True),
        "submit_completed": _checkpoint(True),
        "proposal_id_received": _checkpoint(True),
        "proposal_visible": _checkpoint(False),
        "terminal_status_seen": _checkpoint(False),
    }

    failure = _classify_live_failure(
        checkpoints,
        proposal_visible=False,
        terminal_status=None,
    )

    assert failure == "proposal_not_visible_after_submit"


def test_classify_live_failure_identifies_timeout_explicitly() -> None:
    checkpoints = {
        "task_selected": _checkpoint(True),
        "model_bypassed_canned": _checkpoint(False),
        "model_started": _checkpoint(True),
        "first_token_received": _checkpoint(False),
        "model_timeout": _checkpoint(True),
        "model_completed": _checkpoint(False),
        "submit_started": _checkpoint(False),
        "submit_completed": _checkpoint(False),
        "proposal_id_received": _checkpoint(False),
        "proposal_visible": _checkpoint(False),
        "terminal_status_seen": _checkpoint(False),
    }

    failure = _classify_live_failure(
        checkpoints,
        proposal_visible=False,
        terminal_status=None,
    )

    assert failure == "model_execution_failed"


def test_classify_live_failure_allows_canned_bypass() -> None:
    checkpoints = {
        "task_selected": _checkpoint(True),
        "model_bypassed_canned": _checkpoint(True),
        "model_started": _checkpoint(False),
        "first_token_received": _checkpoint(True),
        "model_timeout": _checkpoint(False),
        "model_completed": _checkpoint(False),
        "submit_started": _checkpoint(True),
        "submit_completed": _checkpoint(True),
        "proposal_id_received": _checkpoint(True),
        "proposal_visible": _checkpoint(True),
        "terminal_status_seen": _checkpoint(True),
    }

    failure = _classify_live_failure(
        checkpoints,
        proposal_visible=True,
        terminal_status="PROMOTED",
    )

    assert failure is None


def test_landed_paths_source_reports_explicit() -> None:
    assert _landed_paths_source({"affected_paths": ["tools/jsonpeek.py"]}) == "explicit"


def test_landed_paths_source_reports_derived_from_diff() -> None:
    assert _landed_paths_source({"change_content": "diff --git a/a.py b/a.py\n"}) == "derived_from_diff"


def test_landed_paths_source_reports_missing() -> None:
    assert _landed_paths_source({}) == "missing"


def test_copy_excludes_omit_non_runtime_bulk() -> None:
    for name in ("dashboard_v2", "dashboard_v3", "data", "temp_analysis", "vault", "venv"):
        assert name in COPY_EXCLUDES


def test_copy_includes_cover_proof_roots() -> None:
    for name in ("scripts", "shared", "tests", "tools", "worker", "qualification"):
        assert name in COPY_INCLUDE_ROOTS
    for name in ("mx.config.json", "pyproject.toml"):
        assert name in COPY_INCLUDE_FILES


def test_copy_worktree_creates_lightweight_snapshot_repo(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "mx.config.json").write_text("{}", encoding="utf-8")
    (source / "notes.txt").write_text("hello\n", encoding="utf-8")
    scripts_dir = source / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "mini.py").write_text("print('ok')\n", encoding="utf-8")
    excluded = source / "vault"
    excluded.mkdir()
    (excluded / "secret.txt").write_text("nope\n", encoding="utf-8")

    target = tmp_path / "target"
    _copy_worktree(source, target)

    assert (target / "mx.config.json").exists()
    assert (target / "scripts" / "mini.py").exists()
    assert not (target / "notes.txt").exists()
    assert not (target / "vault").exists()
    assert (target / ".git").exists()
    assert (target / ".venv" / "bin" / "python").exists()

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(target),
        check=True,
        capture_output=True,
        text=True,
    )
    assert head.stdout.strip()
    branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=str(target),
        check=True,
        capture_output=True,
        text=True,
    )
    assert branch.stdout.strip() == "main"


def test_materialize_worktree_can_clone_prepared_snapshot_repo(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "mx.config.json").write_text("{}", encoding="utf-8")
    (source / "scripts").mkdir()
    (source / "scripts" / "mini.py").write_text("print('ok')\n", encoding="utf-8")

    template_repo = tmp_path / "template"
    _copy_worktree(source, template_repo)

    target = tmp_path / "target"
    _materialize_worktree(template_repo, target, source_is_template_repo=True)

    assert (target / "mx.config.json").exists()
    assert (target / "scripts" / "mini.py").exists()
    assert (target / ".git").exists()
    assert (target / ".venv" / "bin" / "python").exists()

    branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=str(target),
        check=True,
        capture_output=True,
        text=True,
    )
    assert branch.stdout.strip() == "main"
