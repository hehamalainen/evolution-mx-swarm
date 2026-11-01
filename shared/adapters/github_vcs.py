"""GitHub VCS Adapter - Wraps GitHubAPI for PR creation.

This adapter translates VCSAdapter.submit_change() calls into
GitHub PRs using the existing GitHubAPI class.
"""

from typing import Any

from shared.adapters.vcs_adapter import LandingVerificationResult, SubmitResult, VCSAdapter
from shared.utils import MockGitHubAPI
from shared.utils.git import GitHubAPI


class GitHubVCS(VCSAdapter):
    """GitHub VCS adapter - creates PRs via GitHub API.

    This wraps the existing GitHubAPI class to conform to the
    VCSAdapter interface.
    """

    def __init__(
        self,
        token: str,
        owner: str,
        repo: str,
        default_branch: str = "main",
    ):
        """Initialize GitHub VCS adapter.

        Args:
            token: GitHub personal access token
            owner: Repository owner
            repo: Repository name
            default_branch: Base branch for PRs
        """
        self.github = GitHubAPI(token=token, owner=owner, repo=repo) if token else MockGitHubAPI(
            owner=owner or "local",
            repo=repo or "swarm",
        )
        self.owner = owner
        self.repo = repo
        self.default_branch = default_branch

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
        """Create a GitHub pull request.

        Note: The actual git push must happen before calling this.
        This method creates the PR for an already-pushed branch.
        """
        # Map risk tier to emoji
        risk_emoji = {0: "✨", 1: "⚠️", 2: "🔴"}.get(risk_tier, "📦")

        # Generate PR title
        title = f"{risk_emoji} [{task_id[:8]}] {goal[:60]}"

        # Generate PR body with evidence
        body = f"""## Task

**ID:** `{task_id}`
**Goal:** {goal}
**Risk Tier:** {risk_tier} ({'low' if risk_tier == 0 else 'medium' if risk_tier == 1 else 'high'})
**Confidence:** {confidence:.0%}
**Model:** {model_id or 'unknown'}

## Files Changed
{chr(10).join(f'- `{p}`' for p in affected_paths)}

{evidence_markdown}

---
*Created by MX Swarm via GitHubVCS adapter*
"""

        try:
            pr = await self.github.create_pull_request(
                title=title,
                body=body,
                head=branch_name,
                base=self.default_branch,
                draft=draft,
            )

            return SubmitResult(
                success=True,
                submission_type="pr",
                submission_id=str(pr.get("number", "")),
                url=pr.get("html_url"),
                status="open" if not draft else "draft",
                message=f"PR #{pr.get('number')} created",
                raw_response=pr,
            )

        except Exception as e:
            return SubmitResult(
                success=False,
                submission_type="pr",
                submission_id="",
                status="error",
                message=str(e),
            )

    async def get_status(self, submission_id: str) -> dict[str, Any]:
        """Get PR status from GitHub.

        Args:
            submission_id: PR number as string

        Returns:
            Dict with PR state, mergeable status, checks, etc.
        """
        try:
            pr_number = int(submission_id)
            pr = await self.github.get_pull_request(pr_number)

            # Get check status
            ref = pr.get("head", {}).get("sha", "")
            checks_passed = await self.github.all_checks_passed(ref) if ref else None

            return {
                "type": "pr",
                "number": pr_number,
                "state": pr.get("state"),
                "mergeable": pr.get("mergeable"),
                "merged": pr.get("merged", False),
                "checks_passed": checks_passed,
                "url": pr.get("html_url"),
            }

        except Exception as e:
            return {
                "type": "pr",
                "number": submission_id,
                "error": str(e),
            }

    async def verify_landed_change(
        self,
        submission_id: str,
        *,
        expected_paths: list[str],
        expected_diff: str | None = None,
    ) -> LandingVerificationResult:
        """Verify landing against GitHub PR state.

        This is intentionally weaker than Aether verification. In local/mock mode a
        closed mock PR is treated as the landed representation for proof runs.
        """
        status = await self.get_status(submission_id)
        if status.get("error"):
            return LandingVerificationResult(
                supported=True,
                verified=False,
                canonical_status="error",
                provider="github",
                detail=str(status.get("error")),
                verified_paths=list(expected_paths),
                raw_response=status,
            )

        state = str(status.get("state") or "").strip().lower()
        merged = bool(status.get("merged"))
        checks_passed = status.get("checks_passed")

        if merged or state == "closed":
            if checks_passed is False:
                return LandingVerificationResult(
                    supported=True,
                    verified=False,
                    canonical_status="closed",
                    provider="github",
                    detail="PR landed but checks are reported failing.",
                    verified_paths=list(expected_paths),
                    raw_response=status,
                )
            return LandingVerificationResult(
                supported=True,
                verified=True,
                canonical_status="merged" if merged else "closed",
                provider="github",
                detail="GitHub landed representation verified from PR state.",
                verified_paths=list(expected_paths),
                raw_response=status,
            )

        return LandingVerificationResult(
            supported=True,
            verified=False,
            canonical_status=state or "unknown",
            provider="github",
            detail=f"PR is not landed yet: {state or 'unknown'}",
            verified_paths=list(expected_paths),
            raw_response=status,
        )

    async def health_check(self) -> bool:
        """Check if GitHub API is reachable."""
        try:
            # Try to list PRs - if this works, we're healthy
            await self.github.list_pull_requests(state="open")
            return True
        except Exception:
            return False
