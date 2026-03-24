from scripts.run_aether_live_proof import _classify_live_failure, _landed_paths_source


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
