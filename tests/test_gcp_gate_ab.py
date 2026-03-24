from pathlib import Path

from scripts.run_gcp_gate_ab import build_gate_ab_plan


def test_build_gate_ab_plan_uses_gate_profile(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    config_path = tmp_path / "mx.config.cloud.json"
    config_path.write_text(
        """
{
  "frontier_llm": {
    "provider": "gemini",
    "model": "gemini-2.5-flash",
    "api_key": "secret:GEMINI_API_KEY"
  },
  "budget": {
    "daily_usd_cap": 25.0,
    "frontier_daily_usd_cap": 10.0,
    "freeze_mode": "frontier_only",
    "max_frontier_concurrency": 2
  }
}
""".strip()
    )

    plan = build_gate_ab_plan(
        gate="A",
        project_id="mx-staging",
        corpus_path=Path("qualification/corpora/gate2_local_open_weight.json"),
        config_path=config_path,
        topic_prefix="mx-stage",
        subscription_prefix="mx-stage",
    )

    assert plan.gate == "A"
    assert plan.worker_count == 100
    assert plan.duration_minutes == 60
    assert plan.topic_id.startswith("mx-stage-a-")
    assert plan.subscription_id.startswith("mx-stage-a-")
    assert plan.validation.project_id_present is True
    assert plan.validation.frontier_llm_configured is True
    assert plan.validation.frontier_llm_is_gemini is True
    assert plan.validation.frontier_budget_configured is True
    assert plan.validation.frontier_concurrency_configured is True
    assert plan.validation.freeze_mode_valid is True
