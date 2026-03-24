from boss.governor import Governor, TelemetrySnapshot, TriggerType
from shared.schemas.teamplan import TeamPlan


def _make_governor() -> Governor:
    return Governor(teamplan=TeamPlan.default(), telemetry_provider=lambda: TelemetrySnapshot())


def test_detect_stall_false_when_no_outstanding_work() -> None:
    governor = _make_governor()
    telemetry = TelemetrySnapshot(
        workers_active=2,
        tasks_completed_1h=0,
        tasks_in_progress=0,
        queue_depth=0,
    )

    assert governor._detect_stall(telemetry) is False


def test_detect_stall_true_when_queue_has_work_and_nothing_in_progress() -> None:
    governor = _make_governor()
    telemetry = TelemetrySnapshot(
        workers_active=0,
        tasks_completed_1h=0,
        tasks_in_progress=0,
        queue_depth=3,
    )

    assert governor._detect_stall(telemetry) is True


def test_budget_alert_trigger_uses_teamplan_budget_policy() -> None:
    teamplan = TeamPlan.default()
    teamplan.budget_policy.usd_per_day = 10.0
    governor = Governor(teamplan=teamplan, telemetry_provider=lambda: TelemetrySnapshot())

    triggers = governor._check_triggers(
        TelemetrySnapshot(
            spend_today_usd=9.0,
        )
    )

    assert TriggerType.BUDGET_ALERT in triggers
