import json

from shared.schemas import (
    AcceptanceCriteria,
    ModelTier,
    TaskConstraints,
    TaskContext,
    TaskContract,
    TaskStream,
    TaskType,
)
from shared.schemas.teamplan import TeamPlan
from shared.utils.config import MXConfig
from shared.utils.control_plane import build_control_plane_snapshot, render_mxtop


def test_build_control_plane_snapshot_uses_shadow_rig_mapping(tmp_path) -> None:
    teamplan = TeamPlan(
        rig_plans=[
            {
                "rig_id": "rig-backend",
                "goal_cluster_node_ids": ["pkg-1"],
                "frontier_budget_cap_usd": 5.0,
                "governance_owner_id": "governor:backend",
                "streams": [
                    {
                        "stream_id": "backend-execution",
                        "stream_type": "execution",
                        "qa_density_ratio": 0.2,
                        "shards": [
                            {
                                "shard_id": "backend.execution",
                                "target_task_classes": ["feature"],
                                "supported_toolchains": ["python"],
                                "max_mutable_files": 2,
                                "concurrency_cap": 2,
                                "freeze_condition_threshold": 3,
                                "allowed_model_tiers": ["local_open_weight"],
                            }
                        ],
                    }
                ],
            }
        ]
    )
    teamplan.save(str(tmp_path / "TEAMPLAN.json"))

    task = TaskContract.create(
        task_type=TaskType.FEATURE,
        goal="Implement textstats",
        context=TaskContext(files_hint=["tools/textstats.py"]),
        acceptance=AcceptanceCriteria(checks=["pytest tests/test_textstats.py -q"]),
        constraints=TaskConstraints(),
        stream=TaskStream.FEATURE,
        model_tier=ModelTier.LOCAL_OPEN_WEIGHT,
    )
    snapshot_dir = tmp_path / ".runtime" / "task_snapshots"
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / f"{task.id}.json").write_text(
        json.dumps({"task": task.to_dict()}),
    )

    runtime_dir = tmp_path / ".runtime"
    (runtime_dir / "task_state_events.jsonl").write_text(
        json.dumps(
            {
                "task_id": task.id,
                "attempt_id": "a1",
                "state_to": "executing",
                "reason": "",
                "timestamp": "2026-03-12T20:35:00+00:00",
            }
        )
        + "\n"
    )
    (runtime_dir / "routing_decisions.jsonl").write_text(
        json.dumps(
            {
                "actor_id": "shadow_scheduler",
                "task_id": task.id,
                "recommended_lane_key": "rig-backend:feature:backend.execution:local_open_weight:shadow",
                "recommended_stream": "feature",
                "timestamp": "2026-03-12T20:35:01+00:00",
            }
        )
        + "\n"
    )
    (runtime_dir / "governor_events.jsonl").write_text(
        json.dumps(
            {
                "actor_id": "shadow_governor",
                "recommendation_type": "QA_DENSITY_SURGE",
                "scope_type": "rig",
                "scope_id": "rig-backend",
                "status": "THROTTLED",
                "reason": "test",
                "timestamp": "2026-03-12T20:35:02+00:00",
            }
        )
        + "\n"
    )

    snapshot = build_control_plane_snapshot(
        repo_path=str(tmp_path),
        budget_config=MXConfig().budget,
    )

    assert snapshot["rigs"]
    assert snapshot["rigs"][0]["rig_id"] == "rig-backend"
    assert snapshot["rigs"][0]["task_count"] == 1
    assert snapshot["rigs"][0]["status"] == "QA_ELEVATED"

    rendered = render_mxtop(snapshot)
    assert "MX SWARM CONTROL PLANE" in rendered
    assert "rig-backend" in rendered
