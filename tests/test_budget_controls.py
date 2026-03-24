from shared.utils.config import BudgetConfig
from shared.utils.metrics import (
    append_budget_event,
    append_llm_usage_event,
    get_budget_snapshot,
)


def test_budget_snapshot_computes_frontier_spend_and_active_claims(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    append_llm_usage_event(
        trace_id="trace-1",
        task_id="task-1",
        attempt_id="attempt-1",
        actor_id="worker-1",
        provider="gemini",
        model="gemini-2.5-flash",
        model_tier="frontier",
        routing_target="frontier",
        estimated_cost_bucket="high",
        input_tokens=1000,
        output_tokens=500,
        estimated_cost_usd=0.01,
    )
    append_llm_usage_event(
        trace_id="trace-2",
        task_id="task-2",
        attempt_id="attempt-1",
        actor_id="worker-2",
        provider="ollama",
        model="mistral:7b",
        model_tier="local_open_weight",
        routing_target="local",
        estimated_cost_bucket="low",
        input_tokens=1000,
        output_tokens=500,
        estimated_cost_usd=0.0,
    )
    append_budget_event(
        event_type="frontier_claim_started",
        trace_id="trace-1",
        task_id="task-1",
        attempt_id="attempt-1",
        actor_id="worker-1",
        lane="frontier",
        detail="started",
    )

    snapshot = get_budget_snapshot(
        BudgetConfig(
            daily_usd_cap=25.0,
            frontier_daily_usd_cap=10.0,
            max_frontier_escalations_per_hour=5,
            max_frontier_concurrency=2,
            cost_per_success_ceiling_usd=5.0,
        )
    )

    assert snapshot["frontier_spend_today_usd"] == 0.01
    assert snapshot["total_spend_today_usd"] == 0.01
    assert snapshot["active_frontier_claims"] == 1
    assert snapshot["frontier_claims_last_hour"] == 1
    assert snapshot["frontier_spend_ratio"] == 1.0
