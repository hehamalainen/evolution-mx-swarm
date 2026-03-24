import time
from types import MappingProxyType

from shared.utils.capability_registry import (
    TerminalOutcomeClass,
    TerminalSettlementEvent,
    capability_registry_db_path,
    classify_terminal_event,
    get_capability_registry,
    shutdown_capability_registry,
)


def test_capability_registry_records_success_and_refreshes_snapshot(tmp_path) -> None:
    registry = get_capability_registry(repo_path=str(tmp_path), refresh_interval_s=60.0)
    try:
        recorded = registry.record_settlement_event(
            TerminalSettlementEvent(
                task_id="task-1",
                attempt_id="attempt-1",
                trace_id="trace-1",
                lane_key="rig:exec:shard:local:ollama/mistral:7b",
                rig_id="rig",
                stream_id="exec",
                shard_id="shard",
                model_tier="local_open_weight",
                outcome="success",
                terminal_state="settled_ack",
                reason_code=None,
                task_class="feature",
                toolchain="python",
                cost_usd=0.12,
                latency_s=3.5,
                timestamp=time.time(),
            )
        )

        snapshot = registry.get_routing_snapshot()
        capability = snapshot["rig:exec:shard:local:ollama/mistral:7b"]

        assert recorded is True
        assert isinstance(snapshot, MappingProxyType)
        assert capability.pass_rate > 0.5
        assert capability.task_class_strengths["feature"] > 0.5
        assert capability.toolchain_strengths["python"] > 0.5
        assert capability.cost_per_success_usd > 0.0
        assert capability.settlement_latency_ema_s > 0.0
    finally:
        shutdown_capability_registry(repo_path=str(tmp_path))


def test_capability_registry_dedupes_terminal_attempts(tmp_path) -> None:
    registry = get_capability_registry(repo_path=str(tmp_path), refresh_interval_s=60.0)
    try:
        event = TerminalSettlementEvent(
            task_id="task-1",
            attempt_id="attempt-1",
            trace_id="trace-1",
            lane_key="rig:exec:shard:local:ollama/mistral:7b",
            rig_id="rig",
            stream_id="exec",
            shard_id="shard",
            model_tier="local_open_weight",
            outcome="failed",
            terminal_state="settled_nack",
            reason_code="ERR_NO_DIFF",
            task_class="feature",
            toolchain="python",
            cost_usd=0.02,
            latency_s=1.0,
        )

        assert registry.record_settlement_event(event) is True
        assert registry.record_settlement_event(event) is False
    finally:
        shutdown_capability_registry(repo_path=str(tmp_path))


def test_capability_registry_applies_false_success_penalty(tmp_path) -> None:
    db_path = capability_registry_db_path(repo_path=str(tmp_path))
    registry = get_capability_registry(db_path=db_path, refresh_interval_s=60.0)
    try:
        registry.record_settlement_event(
            TerminalSettlementEvent(
                task_id="task-1",
                attempt_id="attempt-1",
                trace_id="trace-1",
                lane_key="rig:exec:shard:frontier:gemini/pro",
                rig_id="rig",
                stream_id="exec",
                shard_id="shard",
                model_tier="frontier",
                outcome="success",
                terminal_state="settled_ack",
                false_success=True,
            )
        )

        capability = registry.get_routing_snapshot()["rig:exec:shard:frontier:gemini/pro"]
        assert capability.false_success_penalty_multiplier == 0.1
    finally:
        shutdown_capability_registry(db_path=db_path)


def test_capability_registry_ignores_transient_system_failures_for_pass_rate(tmp_path) -> None:
    registry = get_capability_registry(repo_path=str(tmp_path), refresh_interval_s=60.0)
    try:
        event = TerminalSettlementEvent(
            task_id="task-2",
            attempt_id="attempt-1",
            trace_id="trace-2",
            lane_key="rig:exec:shard:local:ollama/mistral:7b",
            rig_id="rig",
            stream_id="exec",
            shard_id="shard",
            model_tier="local_open_weight",
            outcome="failed",
            terminal_state="settled_nack",
            reason_code="ERR_TASK_FAILED",
            detail="All connection attempts failed",
            task_class="feature",
            toolchain="python",
            cost_usd=0.0,
            latency_s=2.5,
        )

        assert registry.record_settlement_event(event) is True
        capability = registry.get_routing_snapshot()["rig:exec:shard:local:ollama/mistral:7b"]

        assert event.outcome_class == TerminalOutcomeClass.TRANSIENT_SYSTEM_FAILURE.value
        assert event.learning_weight == 0.0
        assert capability.pass_rate == 0.5
        assert capability.transient_failure_rate > 0.0
        assert capability.task_class_strengths == {}
        assert capability.toolchain_strengths == {}
    finally:
        shutdown_capability_registry(repo_path=str(tmp_path))


def test_classify_terminal_event_marks_landing_and_capability_failures() -> None:
    landing = TerminalSettlementEvent(
        task_id="task-landing",
        attempt_id="attempt-1",
        trace_id="trace-landing",
        lane_key="lane",
        rig_id="rig",
        stream_id="exec",
        shard_id="shard",
        model_tier="frontier",
        outcome="failed",
        terminal_state="settled_nack",
        reason_code="ERR_POST_LANDING_VERIFY",
    )
    capability = TerminalSettlementEvent(
        task_id="task-cap",
        attempt_id="attempt-1",
        trace_id="trace-cap",
        lane_key="lane",
        rig_id="rig",
        stream_id="exec",
        shard_id="shard",
        model_tier="local_open_weight",
        outcome="failed",
        terminal_state="dead_letter",
        reason_code="ERR_CHECK_FAIL",
    )

    assert classify_terminal_event(landing) == (TerminalOutcomeClass.LANDING_FAILURE.value, 1.0)
    assert classify_terminal_event(capability) == (TerminalOutcomeClass.CAPABILITY_FAILURE.value, 1.0)
