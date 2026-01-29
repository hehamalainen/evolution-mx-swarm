"""Aether VCS Adapter - Submits proposals to Project Aether.

This adapter translates VCSAdapter.submit_change() calls into
Aether proposals via the Aether REST API.

Aether is an agent-native VCS that uses CRDT-based conflict-free
merging and probabilistic proposal scoring.
"""

import asyncio
import json
from typing import Any

from shared.adapters.vcs_adapter import SubmitResult, VCSAdapter

# Use aiohttp for async HTTP requests
try:
    import aiohttp
except ImportError:
    aiohttp = None  # Will use fallback sync


class AetherVCS(VCSAdapter):
    """Aether VCS adapter - submits proposals via REST API.

    Aether treats code changes as "proposals" with:
    - Confidence scores
    - Agent/model metadata
    - Risk tiers
    - Automatic conflict detection
    """

    def __init__(
        self,
        base_url: str = "http://localhost:3000",
        repo_id: str | None = None,
        agent_id: str = "mx-swarm",
    ):
        """Initialize Aether VCS adapter.

        Args:
            base_url: Aether server URL (e.g., http://localhost:3000)
            repo_id: Aether repository ID (required for submissions)
            agent_id: Agent identifier for proposals
        """
        self.base_url = base_url.rstrip("/")
        self.repo_id = repo_id
        self.agent_id = agent_id

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
        """Submit a proposal to Aether.

        Maps MX Swarm task data to Aether's ProposalCreate schema.
        """
        if not self.repo_id:
            return SubmitResult(
                success=False,
                submission_type="proposal",
                submission_id="",
                status="error",
                message="Aether repo_id not configured",
            )

        # Build Aether ProposalCreate payload
        proposal_data = {
            "agent_id": self.agent_id,
            "task_id": task_id,
            "change_type": "unified_diff",
            "change_content": diff,
            "affected_paths": affected_paths,
            "risk_tier": risk_tier + 1,  # Aether uses 1-3, we use 0-2
            "confidence_score": confidence,
            "model_id": model_id or "unknown",
            "rationale": f"{goal}\n\n{evidence_markdown}",
            "requires_tests": True,
            "requires_security_scan": risk_tier >= 2,
            "idempotency_key": f"mx-{task_id}-{branch_name}",
        }

        url = f"{self.base_url}/v1/repos/{self.repo_id}/proposals"

        try:
            if aiohttp:
                result = await self._submit_aiohttp(url, proposal_data)
            else:
                result = await self._submit_sync(url, proposal_data)

            return result

        except Exception as e:
            return SubmitResult(
                success=False,
                submission_type="proposal",
                submission_id="",
                status="error",
                message=f"Aether submission failed: {e}",
            )

    async def _submit_aiohttp(self, url: str, data: dict) -> SubmitResult:
        """Submit using aiohttp (async)."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=data,
                headers={"Content-Type": "application/json"},
            ) as response:
                body = await response.json()

                if response.status in (200, 201):
                    return SubmitResult(
                        success=True,
                        submission_type="proposal",
                        submission_id=body.get("proposal_id", ""),
                        url=f"{self.base_url}/repos/{self.repo_id}/proposals/{body.get('proposal_id')}",
                        status=body.get("status", "PENDING"),
                        message=body.get("message", "Proposal submitted"),
                        raw_response=body,
                    )
                else:
                    return SubmitResult(
                        success=False,
                        submission_type="proposal",
                        submission_id="",
                        status="error",
                        message=body.get("detail", f"HTTP {response.status}"),
                        raw_response=body,
                    )

    async def _submit_sync(self, url: str, data: dict) -> SubmitResult:
        """Fallback sync submission using urllib."""
        import urllib.request

        req = urllib.request.Request(
            url,
            data=json.dumps(data).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        # Run in thread to not block
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: urllib.request.urlopen(req, timeout=30),
        )

        body = json.loads(response.read().decode("utf-8"))

        return SubmitResult(
            success=True,
            submission_type="proposal",
            submission_id=body.get("proposal_id", ""),
            url=f"{self.base_url}/repos/{self.repo_id}/proposals/{body.get('proposal_id')}",
            status=body.get("status", "PENDING"),
            message=body.get("message", "Proposal submitted"),
            raw_response=body,
        )

    async def get_status(self, submission_id: str) -> dict[str, Any]:
        """Get proposal status from Aether.

        Args:
            submission_id: Aether proposal_id

        Returns:
            Dict with proposal state, gate results, etc.
        """
        if not self.repo_id:
            return {"error": "repo_id not configured"}

        url = f"{self.base_url}/v1/repos/{self.repo_id}/proposals/{submission_id}"

        try:
            if aiohttp:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as response:
                        if response.status == 200:
                            return await response.json()
                        else:
                            return {"error": f"HTTP {response.status}"}
            else:
                import urllib.request

                req = urllib.request.Request(url, method="GET")
                response = urllib.request.urlopen(req, timeout=10)
                return json.loads(response.read().decode("utf-8"))

        except Exception as e:
            return {
                "type": "proposal",
                "proposal_id": submission_id,
                "error": str(e),
            }

    async def health_check(self) -> bool:
        """Check if Aether server is reachable."""
        url = f"{self.base_url}/v1/health"

        try:
            if aiohttp:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as response:
                        if response.status == 200:
                            data = await response.json()
                            return data.get("status") == "healthy"
            else:
                import urllib.request

                req = urllib.request.Request(url, method="GET")
                response = urllib.request.urlopen(req, timeout=5)
                data = json.loads(response.read().decode("utf-8"))
                return data.get("status") == "healthy"

        except Exception:
            return False

        return False


# Factory function
def create_aether_vcs(
    base_url: str = "http://localhost:3000",
    repo_id: str | None = None,
    agent_id: str = "mx-swarm",
) -> AetherVCS:
    """Create an Aether VCS adapter.

    Args:
        base_url: Aether server URL
        repo_id: Aether repository ID
        agent_id: Agent identifier

    Returns:
        AetherVCS adapter instance
    """
    return AetherVCS(
        base_url=base_url,
        repo_id=repo_id,
        agent_id=agent_id,
    )
