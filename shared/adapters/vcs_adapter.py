"""VCS Adapter - Abstract base class for version control backends.

MX Swarm supports multiple VCS backends:
- GitHub (creates PRs)
- Aether (submits proposals)

Workers use this adapter to submit code changes without knowing the backend.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class SubmitResult:
    """Result of a code submission."""

    success: bool
    submission_type: str  # "pr" or "proposal"
    submission_id: str  # PR number or proposal_id
    url: str | None = None
    status: str = "pending"
    http_status: int | None = None
    message: str = ""
    raw_response: dict | None = None


@dataclass
class LandingVerificationResult:
    """Result of verifying that a canonical sink contains the intended artifact."""

    supported: bool
    verified: bool
    canonical_status: str = ""
    provider: str = ""
    detail: str = ""
    verified_paths: list[str] | None = None
    raw_response: dict[str, Any] | None = None


class VCSAdapter(ABC):
    """Abstract VCS adapter for code submission.

    Workers call submit_change() to push code changes.
    The adapter handles the backend-specific logic.
    """

    @abstractmethod
    async def submit_change(
        self,
        task_id: str,
        goal: str,
        branch_name: str,
        diff: str,
        affected_paths: list[str],
        evidence_markdown: str,
        confidence: float = 0.5,
        model_id: str | None = None,
        risk_tier: int = 0,
        draft: bool = False,
        source: str | None = None,
        source_run_id: str | None = None,
        source_task_id: str | None = None,
        source_agent_id: str | None = None,
        source_attempt_id: str | None = None,
        intent_type: str | None = None,
        contention_mode: bool | None = None,
        contention_group_id: str | None = None,
        contention_policy: str | None = None,
        target_paths: list[str] | None = None,
        base_head_id: str | None = None,
        defer_evaluation: bool | None = None,
    ) -> SubmitResult:
        """Submit a code change to the VCS backend.

        Args:
            task_id: Unique task identifier
            goal: Task goal description
            branch_name: Git branch name for the change
            diff: Unified diff of changes
            affected_paths: List of file paths affected
            evidence_markdown: Evidence pack as markdown
            confidence: Confidence score 0.0-1.0
            model_id: LLM model used (e.g., "mistral:7b")
            risk_tier: Risk level 0=low, 1=medium, 2=high
            draft: Create as draft/pending if True
            source: Optional producer/source label for sink provenance
            source_run_id: Optional end-to-end run correlation identifier
            source_task_id: Optional original task identifier from producer
            source_agent_id: Optional worker/lane/agent identifier from producer
            source_attempt_id: Optional original attempt identifier from producer
            intent_type: Optional normalized producer intent label

        Returns:
            SubmitResult with submission details
        """
        pass

    @abstractmethod
    async def get_status(self, submission_id: str) -> dict[str, Any]:
        """Get status of a previous submission.

        Args:
            submission_id: PR number or proposal_id

        Returns:
            Status dict with backend-specific fields
        """
        pass

    async def verify_landed_change(
        self,
        submission_id: str,
        *,
        expected_paths: list[str],
        expected_diff: str | None = None,
    ) -> LandingVerificationResult:
        """Verify that a canonical sink accepted and landed the intended artifact.

        Backends that do not support post-landing verification should return
        supported=False rather than raising.
        """
        return LandingVerificationResult(
            supported=False,
            verified=False,
            canonical_status="unsupported",
            provider=type(self).__name__,
            detail="Post-landing verification is not implemented for this backend.",
            verified_paths=[],
            raw_response=None,
        )

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if the VCS backend is reachable.

        Returns:
            True if healthy, False otherwise
        """
        pass
