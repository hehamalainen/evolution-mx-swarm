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
    message: str = ""
    raw_response: dict | None = None


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

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if the VCS backend is reachable.

        Returns:
            True if healthy, False otherwise
        """
        pass
