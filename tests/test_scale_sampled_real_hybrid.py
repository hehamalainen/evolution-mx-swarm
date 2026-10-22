from scripts.run_scale_sampled_real_hybrid import _build_payload


def _control_plane_report(*, structural_pass: bool = True, loss_budget_pass: bool = True) -> dict:
    return {
        "manifest": {"run_id": "control-run"},
        "verdict": {
            "passed": structural_pass and loss_budget_pass,
            "structural_pass": structural_pass,
            "loss_budget_pass": loss_budget_pass,
        },
        "metrics": {
            "verified_landings": 1686,
            "total_terminal_tasks": 2000,
            "dead_letter_ratio": 0.1235,
            "dead_letter_ratio_limit": 0.14,
            "dlq_volume": 247,
        },
        "hold_reason_distribution": {"NO_CAPACITY": 895},
        "dead_letter_reason_distribution": {"ERR_PREREQUISITE_FAILED": 160},
    }


def _sampled_real_report(*, passed: bool = True, dlq_volume: int = 0, p95: float = 6.9) -> dict:
    return {
        "manifest": {"run_id": "sampled-run"},
        "verdict": {"passed": passed},
        "metrics": {
            "verified_landings": 100,
            "total_terminal_tasks": 100,
            "dlq_volume": dlq_volume,
            "dead_letter_ratio": dlq_volume / 100.0,
            "sink_settlement_latency_p95_s": p95,
            "batch_wall_clock_seconds": 227.0,
            "throughput_tasks_per_minute": 26.4,
            "sink_truth_divergence_count": 0,
        },
    }


def test_build_payload_passes_when_bounded_real_batch_is_clean() -> None:
    payload = _build_payload(
        logical_lanes=2000,
        seed=23,
        sampled_real_workers=100,
        active_concurrency=4,
        slice_key="jsonpeek",
        timeout_seconds=1200,
        sink_mode="aether",
        sampled_real_settlement_p95_limit=10.0,
        control_plane_report=_control_plane_report(),
        sampled_real_report=_sampled_real_report(),
    )

    assert payload["overall_verdict"]["passed"] is True
    assert payload["overall_verdict"]["reasons"] == []
    assert payload["manifest"]["sampled_real_share"] == 0.05


def test_build_payload_fails_when_sampled_real_batch_breaks_guardrails() -> None:
    payload = _build_payload(
        logical_lanes=2500,
        seed=23,
        sampled_real_workers=150,
        active_concurrency=4,
        slice_key="jsonpeek",
        timeout_seconds=1200,
        sink_mode="aether",
        sampled_real_settlement_p95_limit=10.0,
        control_plane_report=_control_plane_report(loss_budget_pass=False),
        sampled_real_report=_sampled_real_report(passed=False, dlq_volume=2, p95=12.0),
    )

    assert payload["overall_verdict"]["passed"] is False
    assert "control_plane_loss_budget_fail" in payload["overall_verdict"]["reasons"]
    assert "sampled_real_worker_batch_fail" in payload["overall_verdict"]["reasons"]
    assert "sampled_real_dlq_detected:2" in payload["overall_verdict"]["reasons"]
    assert any(
        reason.startswith("sampled_real_settlement_latency_exceeded")
        for reason in payload["overall_verdict"]["reasons"]
    )
