"""SPEC schema - machine-readable requirements graph."""

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class RequirementStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    DEFERRED = "deferred"


class RequirementType(str, Enum):
    FUNCTIONAL = "functional"
    NON_FUNCTIONAL = "non_functional"
    CONSTRAINT = "constraint"
    ACCEPTANCE = "acceptance"


@dataclass
class Requirement:
    """A single requirement extracted from PRD."""
    id: str
    type: RequirementType
    title: str
    description: str
    acceptance_criteria: list[str]
    dependencies: list[str] = field(default_factory=list)  # Requirement IDs
    status: RequirementStatus = RequirementStatus.PENDING
    prd_section: str | None = None
    priority: int = 5  # 1-10
    estimated_complexity: int = 5  # 1-10
    assigned_tasks: list[str] = field(default_factory=list)  # TaskContract IDs
    completion_evidence: list[str] = field(default_factory=list)  # PR URLs, test results

    @classmethod
    def create(
        cls,
        req_type: RequirementType,
        title: str,
        description: str,
        acceptance_criteria: list[str],
        prd_section: str | None = None,
        priority: int = 5,
        complexity: int = 5,
        dependencies: list[str] | None = None,
    ) -> "Requirement":
        return cls(
            id=f"REQ-{str(uuid.uuid4())[:6].upper()}",
            type=req_type,
            title=title,
            description=description,
            acceptance_criteria=acceptance_criteria,
            dependencies=dependencies or [],
            prd_section=prd_section,
            priority=priority,
            estimated_complexity=complexity,
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type.value,
            "title": self.title,
            "description": self.description,
            "acceptance_criteria": self.acceptance_criteria,
            "dependencies": self.dependencies,
            "status": self.status.value,
            "prd_section": self.prd_section,
            "priority": self.priority,
            "estimated_complexity": self.estimated_complexity,
            "assigned_tasks": self.assigned_tasks,
            "completion_evidence": self.completion_evidence,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Requirement":
        return cls(
            id=data["id"],
            type=RequirementType(data["type"]),
            title=data["title"],
            description=data["description"],
            acceptance_criteria=data.get("acceptance_criteria", []),
            dependencies=data.get("dependencies", []),
            status=RequirementStatus(data.get("status", "pending")),
            prd_section=data.get("prd_section"),
            priority=data.get("priority", 5),
            estimated_complexity=data.get("estimated_complexity", 5),
            assigned_tasks=data.get("assigned_tasks", []),
            completion_evidence=data.get("completion_evidence", []),
        )


@dataclass
class Spec:
    """
    Machine-readable requirements specification.

    Generated from PRD.md by the planner.
    """
    version: str
    prd_hash: str  # Hash of source PRD for change detection
    generated_at: str
    requirements: list[Requirement]
    metadata: dict = field(default_factory=dict)

    @classmethod
    def create(cls, prd_hash: str) -> "Spec":
        return cls(
            version="1.0.0",
            prd_hash=prd_hash,
            generated_at=datetime.utcnow().isoformat(),
            requirements=[],
        )

    def add_requirement(self, req: Requirement) -> None:
        """Add a requirement to the spec."""
        self.requirements.append(req)

    def get_requirement(self, req_id: str) -> Requirement | None:
        """Get requirement by ID."""
        for req in self.requirements:
            if req.id == req_id:
                return req
        return None

    def get_ready_requirements(self) -> list[Requirement]:
        """Get requirements that are ready to work on (dependencies met)."""
        completed_ids = {
            r.id for r in self.requirements
            if r.status == RequirementStatus.COMPLETED
        }
        ready = []
        for req in self.requirements:
            if req.status == RequirementStatus.PENDING:
                deps_met = all(d in completed_ids for d in req.dependencies)
                if deps_met:
                    ready.append(req)
        return ready

    def progress_summary(self) -> dict:
        """Calculate progress statistics."""
        total = len(self.requirements)
        if total == 0:
            return {"total": 0, "completed": 0, "in_progress": 0, "pending": 0, "percent": 0}

        completed = sum(1 for r in self.requirements if r.status == RequirementStatus.COMPLETED)
        in_progress = sum(1 for r in self.requirements if r.status == RequirementStatus.IN_PROGRESS)
        pending = sum(1 for r in self.requirements if r.status == RequirementStatus.PENDING)
        blocked = sum(1 for r in self.requirements if r.status == RequirementStatus.BLOCKED)

        return {
            "total": total,
            "completed": completed,
            "in_progress": in_progress,
            "pending": pending,
            "blocked": blocked,
            "percent": round((completed / total) * 100, 1),
        }

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "prd_hash": self.prd_hash,
            "generated_at": self.generated_at,
            "requirements": [r.to_dict() for r in self.requirements],
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Spec":
        spec = cls(
            version=data["version"],
            prd_hash=data["prd_hash"],
            generated_at=data["generated_at"],
            requirements=[Requirement.from_dict(r) for r in data.get("requirements", [])],
            metadata=data.get("metadata", {}),
        )
        return spec

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_json(cls, json_str: str) -> "Spec":
        return cls.from_dict(json.loads(json_str))

    def save(self, path: str) -> None:
        """Save spec to file."""
        with open(path, "w") as f:
            f.write(self.to_json())

    @classmethod
    def load(cls, path: str) -> "Spec":
        """Load spec from file."""
        with open(path) as f:
            return cls.from_json(f.read())
