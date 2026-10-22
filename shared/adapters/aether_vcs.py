"""Aether VCS Adapter - Submits proposals to Project Aether.

This adapter translates VCSAdapter.submit_change() calls into
Aether proposals via the Aether REST API.

Aether is an agent-native VCS that uses CRDT-based conflict-free
merging and probabilistic proposal scoring.
"""

import asyncio
import json
import re
import urllib.parse
from typing import Any, cast

from shared.adapters.vcs_adapter import LandingVerificationResult, SubmitResult, VCSAdapter
from shared.utils.metrics import append_decision_event

# Use aiohttp for async HTTP requests
aiohttp: Any = None
try:
    import aiohttp as aiohttp_module
    aiohttp = aiohttp_module
except ImportError:
    pass  # Will use fallback sync


def _assert_http_url(url: str) -> None:
    if urllib.parse.urlparse(url).scheme not in {"http", "https"}:
        raise ValueError(f"Unsupported URL scheme in {url!r}")


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return cast(dict[str, Any], value)
    return {}


def _as_list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [cast(dict[str, Any], item) for item in value if isinstance(item, dict)]


def _normalize_status(payload: dict[str, Any]) -> str:
    return str(payload.get("status") or payload.get("state") or "").strip().lower()


def _extract_paths(payload: dict[str, Any]) -> list[str]:
    for key in ("affected_paths", "paths", "changed_paths", "landed_paths"):
        value = payload.get(key)
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
    proposal = payload.get("proposal")
    if isinstance(proposal, dict):
        for key in ("affected_paths", "paths", "changed_paths", "landed_paths"):
            value = proposal.get(key)
            if isinstance(value, list):
                return [str(item).strip() for item in value if str(item).strip()]
    diff_payload = str(
        payload.get("change_content")
        or payload.get("landed_change_content")
        or (proposal.get("change_content") if isinstance(proposal, dict) else "")
        or (proposal.get("landed_change_content") if isinstance(proposal, dict) else "")
        or ""
    )
    if diff_payload:
        paths: list[str] = []
        seen: set[str] = set()
        for line in diff_payload.splitlines():
            stripped = line.strip()
            match = re.match(r"^\+\+\+\s+b/(.+)$", stripped)
            if not match:
                match = re.match(r"^diff --git a/(.+?) b/(.+)$", stripped)
                if match:
                    candidate = match.group(2).strip()
                else:
                    continue
            else:
                candidate = match.group(1).strip()
            if not candidate or candidate == "/dev/null" or candidate in seen:
                continue
            seen.add(candidate)
            paths.append(candidate)
        if paths:
            return paths
    return []


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
        max_retries: int = 3,
        base_backoff_s: float = 1.0,
    ):
        """Initialize Aether VCS adapter.

        Args:
            base_url: Aether server URL (e.g., http://localhost:3000)
            repo_id: Aether repository ID (required for submissions)
            agent_id: Agent identifier for proposals
            max_retries: Max retry attempts for submissions
            base_backoff_s: Base backoff (seconds) between retries
        """
        self.base_url = base_url.rstrip("/")
        self.repo_id = repo_id
        self.agent_id = agent_id
        self.max_retries = max_retries
        self.base_backoff_s = base_backoff_s

    def _build_proposal_payload(
        self,
        *,
        task_id: str,
        goal: str,
        branch_name: str,
        diff: str,
        affected_paths: list[str],
        evidence_markdown: str,
        confidence: float,
        model_id: str | None,
        risk_tier: int,
        source: str | None,
        source_run_id: str | None,
        source_task_id: str | None,
        source_agent_id: str | None,
        source_attempt_id: str | None,
        intent_type: str | None,
    ) -> dict[str, Any]:
        """Build the exact Aether ProposalCreate payload used for submission."""
        return {
            "agent_id": self.agent_id,
            "task_id": task_id,
            "change_type": "unified_diff",
            "change_content": diff,
            "affected_paths": affected_paths,
            "risk_tier": risk_tier + 1,  # Aether uses 1-3, MX uses 0-2
            "confidence_score": confidence,
            "model_id": model_id or "unknown",
            "rationale": f"{goal}\n\n{evidence_markdown}",
            "requires_tests": True,
            "requires_security_scan": risk_tier >= 2,
            "idempotency_key": f"mx-{task_id}-{branch_name}",
            "source": source or "mx_swarm",
            "source_run_id": source_run_id,
            "source_task_id": source_task_id or task_id,
            "source_agent_id": source_agent_id,
            "source_attempt_id": source_attempt_id,
            "intent_type": intent_type or "other",
        }

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

        proposal_data = self._build_proposal_payload(
            task_id=task_id,
            goal=goal,
            branch_name=branch_name,
            diff=diff,
            affected_paths=affected_paths,
            evidence_markdown=evidence_markdown,
            confidence=confidence,
            model_id=model_id,
            risk_tier=risk_tier,
            source=source,
            source_run_id=source_run_id,
            source_task_id=source_task_id,
            source_agent_id=source_agent_id,
            source_attempt_id=source_attempt_id,
            intent_type=intent_type,
        )

        # Decision Transparency: Log the intent to submit
        append_decision_event(
            task_id=task_id,
            reasoning=f"Submitting Aether proposal for task: {goal[:100]}",
            choice="submit_proposal",
            impact_level="medium",
            context={
                "affected_paths": affected_paths,
                "confidence": confidence,
                "risk_tier": risk_tier
            }
        )

        url = f"{self.base_url}/v1/repos/{self.repo_id}/proposals"

        last_error: str | None = None
        for attempt in range(1, self.max_retries + 1):
            healthy = await self.health_check()
            if not healthy:
                last_error = "Aether health check failed"
            else:
                try:
                    if aiohttp:
                        return await self._submit_aiohttp(url, proposal_data)
                    return await self._submit_sync(url, proposal_data)
                except Exception as e:
                    last_error = str(e)

            if attempt < self.max_retries:
                backoff = self.base_backoff_s * (2 ** (attempt - 1))
                await asyncio.sleep(backoff)

        return SubmitResult(
            success=False,
            submission_type="proposal",
            submission_id="",
            status="error",
            message=f"Aether submission failed after {self.max_retries} attempts: {last_error}",
        )

    async def get_agent_proposals(self, agent_id: str) -> list[dict[str, Any]]:
        """Fetch all proposals submitted by a specific agent.

        Used for syncing state on startup.
        """
        if not self.repo_id:
            return []

        url = f"{self.base_url}/v1/repos/{self.repo_id}/proposals"
        params = {"agent_id": agent_id}

        try:
            if aiohttp:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, params=params) as resp:
                        if resp.status != 200:
                            print(f"Error fetching proposals: {resp.status}")
                            return []
                        data = _as_dict(await resp.json())
                        return _as_list_of_dicts(data.get("proposals"))
            else:
                # Sync fallback
                import urllib.request

                query = urllib.parse.urlencode(params)
                full_url = f"{url}?{query}"
                _assert_http_url(full_url)
                req = urllib.request.Request(full_url)
                with urllib.request.urlopen(req) as response:  # nosec B310
                    raw = response.read().decode("utf-8")
                    data = _as_dict(json.loads(raw))
                    return _as_list_of_dicts(data.get("proposals"))

        except Exception:
            # print(f"Failed to fetch agent proposals: {e}")
            return []

    async def _submit_aiohttp(self, url: str, data: dict) -> SubmitResult:
        """Submit using aiohttp (async)."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=data,
                headers={"Content-Type": "application/json"},
            ) as response:
                body = _as_dict(await response.json())

                if response.status in (200, 201):
                    return SubmitResult(
                        success=True,
                        submission_type="proposal",
                        submission_id=body.get("proposal_id", ""),
                        url=f"{self.base_url}/v1/repos/{self.repo_id}/proposals/{body.get('proposal_id')}",
                        status=body.get("status", "PENDING"),
                        http_status=response.status,
                        message=body.get("message", "Proposal submitted"),
                        raw_response=body,
                    )
                else:
                    return SubmitResult(
                        success=False,
                        submission_type="proposal",
                        submission_id="",
                        status="error",
                        http_status=response.status,
                        message=body.get("detail", f"HTTP {response.status}"),
                        raw_response=body,
                    )

    async def _submit_sync(self, url: str, data: dict) -> SubmitResult:
        """Fallback sync submission using urllib."""
        import urllib.request

        _assert_http_url(url)
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
            lambda: urllib.request.urlopen(req, timeout=30),  # nosec B310
        )

        body = _as_dict(json.loads(response.read().decode("utf-8")))
        http_status = response.getcode()

        return SubmitResult(
            success=True,
            submission_type="proposal",
            submission_id=body.get("proposal_id", ""),
            url=f"{self.base_url}/v1/repos/{self.repo_id}/proposals/{body.get('proposal_id')}",
            status=body.get("status", "PENDING"),
            http_status=http_status,
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
                            return _as_dict(await response.json())
                        else:
                            return {"error": f"HTTP {response.status}"}
            else:
                import urllib.request

                _assert_http_url(url)
                req = urllib.request.Request(url, method="GET")
                response = urllib.request.urlopen(req, timeout=10)  # nosec B310
                return _as_dict(json.loads(response.read().decode("utf-8")))

        except Exception as e:
            return {
                "type": "proposal",
                "proposal_id": submission_id,
                "error": str(e),
            }

    async def verify_landed_change(
        self,
        submission_id: str,
        *,
        expected_paths: list[str],
        expected_diff: str | None = None,
    ) -> LandingVerificationResult:
        """Verify Aether canonical landing using returned proposal evidence."""
        payload = await self.get_status(submission_id)
        normalized = _normalize_status(payload)
        provider = "aether"
        accepted = {"accepted", "committed", "merged", "settled_ack", "complete", "completed", "promoted"}

        if payload.get("error"):
            return LandingVerificationResult(
                supported=True,
                verified=False,
                canonical_status=normalized or "error",
                provider=provider,
                detail=str(payload.get("error")),
                verified_paths=[],
                raw_response=payload,
            )

        if normalized not in accepted:
            return LandingVerificationResult(
                supported=True,
                verified=False,
                canonical_status=normalized or "unknown",
                provider=provider,
                detail=f"Canonical status is not accepted: {normalized or 'unknown'}",
                verified_paths=[],
                raw_response=payload,
            )

        landed_paths = _extract_paths(payload)
        if not landed_paths:
            return LandingVerificationResult(
                supported=True,
                verified=False,
                canonical_status=normalized,
                provider=provider,
                detail="Aether accepted the proposal but did not return landed path evidence.",
                verified_paths=[],
                raw_response=payload,
            )

        expected = {path.strip() for path in expected_paths if path.strip()}
        actual = set(landed_paths)
        missing = sorted(expected - actual)
        if missing:
            return LandingVerificationResult(
                supported=True,
                verified=False,
                canonical_status=normalized,
                provider=provider,
                detail=f"Aether landed-path evidence is missing expected paths: {missing}",
                verified_paths=landed_paths,
                raw_response=payload,
            )

        if expected_diff:
            diff_echo = str(
                payload.get("change_content")
                or payload.get("landed_change_content")
                or _as_dict(payload.get("proposal")).get("change_content")
                or ""
            )
            if diff_echo and diff_echo.strip() != expected_diff.strip():
                return LandingVerificationResult(
                    supported=True,
                    verified=False,
                    canonical_status=normalized,
                    provider=provider,
                    detail="Aether landed diff evidence does not match the submitted diff.",
                    verified_paths=landed_paths,
                    raw_response=payload,
                )

        return LandingVerificationResult(
            supported=True,
            verified=True,
            canonical_status=normalized,
            provider=provider,
            detail="Aether canonical landing verified.",
            verified_paths=landed_paths,
            raw_response=payload,
        )

    async def health_check(self) -> bool:
        """Check if Aether server is reachable."""
        url = f"{self.base_url}/v1/health"

        try:
            if aiohttp:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as response:
                        if response.status == 200:
                            data = _as_dict(await response.json())
                            return data.get("status") == "healthy"
            else:
                import urllib.request

                _assert_http_url(url)
                req = urllib.request.Request(url, method="GET")
                response = urllib.request.urlopen(req, timeout=5)  # nosec B310
                data = _as_dict(json.loads(response.read().decode("utf-8")))
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
