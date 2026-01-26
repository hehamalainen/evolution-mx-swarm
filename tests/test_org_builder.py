"""Tests for Org Builder and TEAMPLAN functionality."""

import os
import sys

import pytest

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.schemas.task_contract import RiskTier, TaskContract, TaskStream, TaskType
from shared.schemas.teamplan import (
    BudgetPolicy,
    CapsConfig,
    MergePolicy,
    ReorgPolicy,
    ResetTriggers,
    RiskMergePolicy,
    StreamConfig,
    TeamPlan,
    Topology,
)


class TestTeamPlanSchema:
    """Test TEAMPLAN schema."""

    def test_default_teamplan(self):
        """Test creating default TEAMPLAN."""
        teamplan = TeamPlan.default()

        assert teamplan.version == "0.1"
        assert teamplan.topology == Topology.STREAMS
        assert len(teamplan.streams) == 5  # feature, qa, ops, security, docs
        assert teamplan.total_target_workers() == 95  # 50+25+10+5+5

    def test_stream_config(self):
        """Test StreamConfig."""
        stream = StreamConfig(
            name="feature",
            role="feature_engineer",
            target_workers=50,
            max_workers=70,
            tools=["git", "tests"],
            priority=2
        )

        d = stream.to_dict()
        assert d["name"] == "feature"
        assert d["target_workers"] == 50

        restored = StreamConfig.from_dict(d)
        assert restored.name == stream.name
        assert restored.max_workers == stream.max_workers

    def test_caps_config(self):
        """Test CapsConfig."""
        caps = CapsConfig(
            pr_cap_per_hour=10,
            open_pr_cap=20,
            max_parallel_ci=5,
            max_active_tasks=50
        )

        d = caps.to_dict()
        assert d["pr_cap_per_hour"] == 10
        assert d["open_pr_cap"] == 20

    def test_risk_merge_policy(self):
        """Test RiskMergePolicy."""
        policy = RiskMergePolicy(
            tier0=MergePolicy.AUTO_ON_GREEN,
            tier1=MergePolicy.AUTO_ON_GREEN_FEATURE_FLAG,
            tier2=MergePolicy.MANUAL_REVIEW_REQUIRED
        )

        assert policy.get_policy(0) == MergePolicy.AUTO_ON_GREEN
        assert policy.get_policy(1) == MergePolicy.AUTO_ON_GREEN_FEATURE_FLAG
        assert policy.get_policy(2) == MergePolicy.MANUAL_REVIEW_REQUIRED

    def test_teamplan_serialization(self):
        """Test TEAMPLAN JSON serialization."""
        teamplan = TeamPlan.default()
        teamplan.complexity_score = 0.5
        teamplan.coupling_score = 0.3
        teamplan.risk_score = 0.4

        # Serialize
        json_str = teamplan.to_json()
        assert '"version": "0.1"' in json_str
        assert '"topology": "streams"' in json_str

        # Deserialize
        d = teamplan.to_dict()
        restored = TeamPlan.from_dict(d)
        assert restored.version == teamplan.version
        assert restored.complexity_score == 0.5
        assert len(restored.streams) == len(teamplan.streams)

    def test_teamplan_role_mix(self):
        """Test role mix calculation."""
        teamplan = TeamPlan.default()
        mix = teamplan.get_role_mix()

        assert "feature" in mix
        assert "qa" in mix
        assert sum(mix.values()) == pytest.approx(100, rel=0.01)


class TestTaskContractExtensions:
    """Test TaskContract stream/tier/spec_ref fields."""

    def test_task_with_stream_and_tier(self):
        """Test creating task with routing fields."""
        task = TaskContract.create(
            task_type=TaskType.FEATURE,
            goal="Implement user login",
            stream=TaskStream.FEATURE,
            tier=RiskTier.TIER_1,
            spec_ref="RG-001"
        )

        assert task.stream == TaskStream.FEATURE
        assert task.tier == RiskTier.TIER_1
        assert task.spec_ref == "RG-001"

    def test_task_serialization_with_new_fields(self):
        """Test task serialization includes new fields."""
        task = TaskContract.create(
            task_type=TaskType.SECURITY,
            goal="Add input validation",
            stream=TaskStream.SECURITY,
            tier=RiskTier.TIER_2,
            spec_ref="SEC-042"
        )

        d = task.to_dict()
        assert d["stream"] == "security"
        assert d["tier"] == 2
        assert d["spec_ref"] == "SEC-042"

        # Restore
        restored = TaskContract.from_dict(d)
        assert restored.stream == TaskStream.SECURITY
        assert restored.tier == RiskTier.TIER_2
        assert restored.spec_ref == "SEC-042"

    def test_backward_compatibility(self):
        """Test tasks without new fields still work."""
        old_task_data = {
            "id": "abc123",
            "type": "feature",
            "goal": "Old task",
            "context": {},
            "acceptance": {},
            "constraints": {},
        }

        task = TaskContract.from_dict(old_task_data)
        assert task.stream == TaskStream.FEATURE  # Default
        assert task.tier == RiskTier.TIER_1  # Default
        assert task.spec_ref is None


class TestResetTriggers:
    """Test reset trigger thresholds."""

    def test_default_triggers(self):
        """Test default trigger values."""
        triggers = ResetTriggers()
        assert triggers.ci_health_lt == 85
        assert triggers.nack_rate_gt == 25
        assert triggers.stall_minutes_gt == 60

    def test_custom_triggers(self):
        """Test custom trigger thresholds."""
        triggers = ResetTriggers(
            ci_health_lt=90,
            nack_rate_gt=15,
            conflict_rate_gt=20
        )
        assert triggers.ci_health_lt == 90
        assert triggers.nack_rate_gt == 15


class TestBudgetPolicy:
    """Test budget policy."""

    def test_default_budget(self):
        """Test default budget policy."""
        budget = BudgetPolicy()
        assert budget.usd_per_day == 50.0
        assert budget.hard_stop is True

    def test_custom_budget(self):
        """Test custom budget policy."""
        budget = BudgetPolicy(
            usd_per_day=200,
            usd_per_hour_soft=25,
            hard_stop=False,
            burn_alert_thresholds=[60, 80, 95]
        )
        assert budget.usd_per_day == 200
        assert budget.usd_per_hour_soft == 25
        assert budget.hard_stop is False


class TestReorgPolicy:
    """Test reorg policy."""

    def test_default_reorg(self):
        """Test default reorg policy."""
        reorg = ReorgPolicy()
        assert reorg.interval_minutes == 60
        assert reorg.hysteresis_minutes == 30
        assert reorg.prefer_caps_before_reorg is True

    def test_reorg_serialization(self):
        """Test reorg policy serialization."""
        reorg = ReorgPolicy(
            interval_minutes=45,
            max_worker_delta_per_interval=10
        )
        d = reorg.to_dict()
        assert d["interval_minutes"] == 45
        assert d["max_worker_delta_per_interval"] == 10

        restored = ReorgPolicy.from_dict(d)
        assert restored.interval_minutes == 45
