"""Tests for MX Swarm schemas."""

import pytest
import json

from shared.schemas import (
    TaskContract,
    TaskType,
    TaskContext,
    AcceptanceCriteria,
    TaskConstraints,
    MergePolicy,
    RiskLevel,
    Spec,
    Requirement,
    RequirementType,
    RequirementStatus,
)


class TestTaskContract:
    """Tests for TaskContract schema."""

    def test_create_task(self):
        """Test creating a basic task."""
        task = TaskContract.create(
            task_type=TaskType.FEATURE,
            goal="Implement user authentication",
            priority=8,
        )

        assert task.id is not None
        assert len(task.id) == 8
        assert task.type == TaskType.FEATURE
        assert task.goal == "Implement user authentication"
        assert task.priority == 8
        assert task.attempt_count == 0

    def test_create_task_with_context(self):
        """Test creating task with full context."""
        context = TaskContext(
            prd_refs=["REQ-001", "REQ-002"],
            files_hint=["src/auth.py", "src/models.py"],
            do_not_touch=["config/secrets.py"],
        )

        acceptance = AcceptanceCriteria(
            checks=["pytest tests/", "ruff check ."],
            criteria=["Users can log in with email/password"],
        )

        constraints = TaskConstraints(
            timebox_minutes=90,
            risk=RiskLevel.MEDIUM,
            merge_policy=MergePolicy.REQUIRE_REVIEW,
            retry_max=3,
        )

        task = TaskContract.create(
            task_type=TaskType.FEATURE,
            goal="Add login functionality",
            context=context,
            acceptance=acceptance,
            constraints=constraints,
        )

        assert task.context.prd_refs == ["REQ-001", "REQ-002"]
        assert task.acceptance.checks == ["pytest tests/", "ruff check ."]
        assert task.constraints.timebox_minutes == 90
        assert task.constraints.risk == RiskLevel.MEDIUM

    def test_task_serialization(self):
        """Test task to/from JSON."""
        task = TaskContract.create(
            task_type=TaskType.BUGFIX,
            goal="Fix login validation bug",
            priority=9,
        )

        # Serialize
        json_str = task.to_json()
        data = json.loads(json_str)

        assert data["type"] == "bugfix"
        assert data["goal"] == "Fix login validation bug"
        assert data["priority"] == 9

        # Deserialize
        restored = TaskContract.from_json(json_str)

        assert restored.id == task.id
        assert restored.type == task.type
        assert restored.goal == task.goal

    def test_branch_name_generation(self):
        """Test branch name generation from task."""
        task = TaskContract.create(
            task_type=TaskType.FEATURE,
            goal="Add user profile page",
        )

        branch = task.branch_name()

        assert branch.startswith("task/")
        assert task.id in branch
        assert "add-user-profile" in branch.lower()

    def test_branch_name_sanitization(self):
        """Test that branch names are sanitized."""
        task = TaskContract.create(
            task_type=TaskType.FEATURE,
            goal="Fix bug in user/profile endpoint!",
        )

        branch = task.branch_name()

        assert "/" not in branch[5:]  # After "task/"
        assert "!" not in branch


class TestSpec:
    """Tests for Spec schema."""

    def test_create_spec(self):
        """Test creating a spec."""
        spec = Spec.create(prd_hash="abc123def456")

        assert spec.version == "1.0.0"
        assert spec.prd_hash == "abc123def456"
        assert len(spec.requirements) == 0

    def test_add_requirements(self):
        """Test adding requirements to spec."""
        spec = Spec.create(prd_hash="abc123")

        req1 = Requirement.create(
            req_type=RequirementType.FUNCTIONAL,
            title="User Login",
            description="Users can log in with email/password",
            acceptance_criteria=["Login form displayed", "Successful login redirects"],
            priority=9,
        )

        req2 = Requirement.create(
            req_type=RequirementType.FUNCTIONAL,
            title="User Registration",
            description="Users can create new accounts",
            acceptance_criteria=["Registration form displayed"],
            priority=8,
            dependencies=[req1.id],
        )

        spec.add_requirement(req1)
        spec.add_requirement(req2)

        assert len(spec.requirements) == 2
        assert spec.get_requirement(req1.id) == req1
        assert spec.requirements[1].dependencies == [req1.id]

    def test_get_ready_requirements(self):
        """Test getting requirements ready for work."""
        spec = Spec.create(prd_hash="test")

        req1 = Requirement.create(
            req_type=RequirementType.FUNCTIONAL,
            title="Base feature",
            description="Foundation",
            acceptance_criteria=[],
        )

        req2 = Requirement.create(
            req_type=RequirementType.FUNCTIONAL,
            title="Dependent feature",
            description="Depends on base",
            acceptance_criteria=[],
            dependencies=[req1.id],
        )

        spec.add_requirement(req1)
        spec.add_requirement(req2)

        # Initially, only req1 should be ready (no dependencies)
        ready = spec.get_ready_requirements()
        assert len(ready) == 1
        assert ready[0].id == req1.id

        # After completing req1, req2 should be ready
        req1.status = RequirementStatus.COMPLETED
        ready = spec.get_ready_requirements()
        assert len(ready) == 1
        assert ready[0].id == req2.id

    def test_progress_summary(self):
        """Test progress calculation."""
        spec = Spec.create(prd_hash="test")

        for i in range(10):
            req = Requirement.create(
                req_type=RequirementType.FUNCTIONAL,
                title=f"Requirement {i}",
                description=f"Description {i}",
                acceptance_criteria=[],
            )
            spec.add_requirement(req)

        # Mark some as completed
        spec.requirements[0].status = RequirementStatus.COMPLETED
        spec.requirements[1].status = RequirementStatus.COMPLETED
        spec.requirements[2].status = RequirementStatus.IN_PROGRESS

        progress = spec.progress_summary()

        assert progress["total"] == 10
        assert progress["completed"] == 2
        assert progress["in_progress"] == 1
        assert progress["pending"] == 7
        assert progress["percent"] == 20.0

    def test_spec_serialization(self):
        """Test spec to/from JSON."""
        spec = Spec.create(prd_hash="serialization_test")

        req = Requirement.create(
            req_type=RequirementType.FUNCTIONAL,
            title="Test Requirement",
            description="For serialization test",
            acceptance_criteria=["Criteria 1", "Criteria 2"],
        )
        spec.add_requirement(req)

        # Serialize
        json_str = spec.to_json()

        # Deserialize
        restored = Spec.from_json(json_str)

        assert restored.prd_hash == spec.prd_hash
        assert len(restored.requirements) == 1
        assert restored.requirements[0].title == "Test Requirement"


class TestRequirement:
    """Tests for Requirement schema."""

    def test_create_requirement(self):
        """Test creating a requirement."""
        req = Requirement.create(
            req_type=RequirementType.FUNCTIONAL,
            title="User Authentication",
            description="System must authenticate users",
            acceptance_criteria=[
                "Users can log in with valid credentials",
                "Invalid credentials show error message",
            ],
            prd_section="Authentication",
            priority=9,
            complexity=7,
        )

        assert req.id.startswith("REQ-")
        assert req.type == RequirementType.FUNCTIONAL
        assert req.title == "User Authentication"
        assert len(req.acceptance_criteria) == 2
        assert req.status == RequirementStatus.PENDING
        assert req.priority == 9
        assert req.estimated_complexity == 7

    def test_requirement_serialization(self):
        """Test requirement to/from dict."""
        req = Requirement.create(
            req_type=RequirementType.NON_FUNCTIONAL,
            title="Performance",
            description="API response time < 200ms",
            acceptance_criteria=["Load test passes"],
        )

        data = req.to_dict()
        restored = Requirement.from_dict(data)

        assert restored.id == req.id
        assert restored.type == req.type
        assert restored.title == req.title


class TestEnums:
    """Tests for enum types."""

    def test_task_types(self):
        """Test all task types."""
        types = [
            TaskType.FEATURE,
            TaskType.BUGFIX,
            TaskType.TEST,
            TaskType.REFACTOR,
            TaskType.OPS,
            TaskType.DOC,
        ]

        for t in types:
            assert isinstance(t.value, str)

    def test_risk_levels(self):
        """Test risk level ordering."""
        assert RiskLevel.LOW.value == "low"
        assert RiskLevel.MEDIUM.value == "medium"
        assert RiskLevel.HIGH.value == "high"

    def test_merge_policies(self):
        """Test merge policies."""
        assert MergePolicy.MANUAL.value == "manual"
        assert MergePolicy.AUTO_ON_PASS.value == "auto_on_pass"
        assert MergePolicy.REQUIRE_REVIEW.value == "require_review"
