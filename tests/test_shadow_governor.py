from shared.utils.shadow_governor import evaluate_shadow_governor


def test_shadow_governor_recommends_dependency_drain_and_qa_surge() -> None:
    recommendations = evaluate_shadow_governor(
        [
            {
                "rig_id": "rig-backend",
                "task_count": 12,
                "width": 3,
                "pass_rate": 0.7,
                "blocked_dependency_ratio": 0.75,
                "acceptance_failure_rate": 0.3,
                "false_success_rate": 0.0,
                "frontier_spend_usd": 1.0,
                "frontier_budget_cap_usd": 10.0,
            }
        ],
        {"frontier_frozen": False},
    )

    types = {item.recommendation_type for item in recommendations}
    assert "DEPENDENCY_DRAIN" in types
    assert "QA_DENSITY_SURGE" in types


def test_shadow_governor_recommends_economic_freeze_and_queue_widen() -> None:
    recommendations = evaluate_shadow_governor(
        [
            {
                "rig_id": "rig-fast",
                "task_count": 20,
                "width": 4,
                "pass_rate": 0.95,
                "blocked_dependency_ratio": 0.0,
                "acceptance_failure_rate": 0.0,
                "false_success_rate": 0.0,
                "frontier_spend_usd": 5.0,
                "frontier_budget_cap_usd": 5.0,
            }
        ],
        {"frontier_frozen": False},
    )

    types = {item.recommendation_type for item in recommendations}
    assert "ECONOMIC_FREEZE" in types
    assert "QUEUE_WIDEN" in types
