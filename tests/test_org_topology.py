from shared.schemas.org_topology import (
    CapabilityVector,
    LaneStatus,
    RigPlan,
    RoutingHoldReason,
    ShardPlan,
    StreamPlan,
    WorkerLane,
    build_lane_key,
)


def test_build_lane_key_uses_stable_composite_identity() -> None:
    lane_key = build_lane_key(
        rig_id="rig-auth",
        stream_id="execution",
        shard_id="backend.feature.local_open_weight",
        model_tier="local_open_weight",
        provider_model="ollama/mistral:7b",
    )

    assert lane_key == "rig-auth:execution:backend.feature.local_open_weight:local_open_weight:ollama/mistral:7b"


def test_worker_lane_keeps_capability_evidence_separate_from_status() -> None:
    lane = WorkerLane(
        lane_key="rig:exec:shard:local:model",
        model_tier="local_open_weight",
        target_task_classes={"feature"},
        supported_toolchains={"python"},
        max_mutable_files=1,
        capabilities=CapabilityVector(),
    )

    assert lane.status == LaneStatus.ACTIVE
    assert lane.capabilities.false_success_penalty_multiplier == 1.0
    assert RoutingHoldReason.LOW_UTILITY_HOLD.value == "LOW_UTILITY_HOLD"


def test_rig_plan_boundary_contract_parses_defaults() -> None:
    rig = RigPlan(
        rig_id="rig-cli",
        goal_cluster_node_ids=["node-1"],
        frontier_budget_cap_usd=2.5,
        governance_owner_id="governor-1",
        streams=[
            StreamPlan(
                stream_id="exec",
                stream_type="execution",
                shards=[
                    ShardPlan(
                        shard_id="cli.feature.local",
                        target_task_classes=["feature"],
                        supported_toolchains=["python"],
                        max_mutable_files=1,
                        concurrency_cap=4,
                        freeze_condition_threshold=3,
                        allowed_model_tiers=["local_open_weight"],
                    )
                ],
            )
        ],
    )

    assert rig.streams[0].stream_id == "exec"
    assert rig.streams[0].shards[0].allowed_model_tiers == ["local_open_weight"]
