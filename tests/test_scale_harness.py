import json
from datetime import UTC
from pathlib import Path

import pytest

from shared.utils.routing import build_shadow_lanes
from simulation.scale_harness import (
    _parse_timestamp,
    build_scale_rig_plans,
    run_hybrid_control_plane_simulation,
    run_isolated_worker_load,
)


def test_build_scale_rig_plans_supports_requested_lane_count() -> None:
    rig_plans = build_scale_rig_plans(logical_lanes=60, rig_count=6)
    lanes = build_shadow_lanes(rig_plans, {})

    assert len(lanes) >= 60
    assert any(":feature:" in lane.lane_key for lane in lanes)
    assert any(":qa:" in lane.lane_key for lane in lanes)


def test_hybrid_control_plane_simulation_emits_stable_report() -> None:
    report = run_hybrid_control_plane_simulation(
        logical_lanes=80,
        logical_tasks=120,
        seed=11,
    )

    assert report.manifest.phase == "control_plane_hybrid"
    assert report.metrics.total_terminal_tasks == 120
    assert report.metrics.false_success_rate == 0.0
    assert report.metrics.duplicate_terminal_settlements == 0
    assert report.metrics.zombie_blocked_tasks == 0
    assert report.metrics.batch_wall_clock_seconds > 0
    assert report.metrics.throughput_tasks_per_minute > 0
    assert report.verdict.passed is True
    assert report.hold_reason_distribution


def test_parse_timestamp_normalizes_naive_and_utc_values() -> None:
    aware = _parse_timestamp("2026-03-25T12:00:00+00:00")
    naive = _parse_timestamp("2026-03-25T12:00:00")

    assert aware is not None
    assert naive is not None
    assert aware.tzinfo == UTC
    assert naive.tzinfo == UTC


@pytest.mark.asyncio
async def test_run_isolated_worker_load_uses_mock_sink_artifact(monkeypatch, tmp_path) -> None:
    class Proc:
        returncode = 0

        async def communicate(self):
            return (b"", b"")

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        assert str(cmd[1]).endswith("run_mock_sink_live_proof.py")
        assert "--source-is-template-repo" in cmd
        report_root = None
        for index, item in enumerate(cmd):
            if item == "--report-root":
                report_root = cmd[index + 1]
                break
        assert report_root is not None
        latest_dir = Path(report_root) / "20260328T000000Z"
        latest_dir.mkdir(parents=True, exist_ok=True)
        (latest_dir / "mock_sink_live_proof.json").write_text(
            json.dumps(
                {
                    "post_landing_verify": "pass",
                    "stage_checkpoints": {
                        "task_selected": {"timestamp": "2026-03-28T00:00:00+00:00"},
                        "terminal_status_seen": {"timestamp": "2026-03-28T00:00:03+00:00"},
                    },
                }
            ),
            encoding="utf-8",
        )
        return Proc()

    monkeypatch.setattr("simulation.scale_harness.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    report = await run_isolated_worker_load(
        logical_workers=1,
        active_concurrency=1,
        slice_key="jsonpeek",
        timeout_seconds=60,
        repo_root=tmp_path,
        source_root=tmp_path,
        sink_mode="mock_github",
    )

    assert report.manifest.sink_mode == "mock_github"
    assert report.metrics.verified_landings == 1
    assert report.metrics.batch_wall_clock_seconds > 0
    assert report.metrics.throughput_tasks_per_minute > 0
    assert report.sink_summary["mode"] == "mock_github"
