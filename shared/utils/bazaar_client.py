from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

logger = logging.getLogger(__name__)


def _load_external_molt_module() -> ModuleType | None:
    research_root = os.environ.get("MOLTBOOK_RESEARCH_ROOT", "").strip()
    if not research_root:
        return None

    module_path = Path(research_root) / "molt_client.py"
    if not module_path.exists():
        logger.warning("MOLTBOOK_RESEARCH_ROOT is set but molt_client.py was not found at %s", module_path)
        return None

    if research_root not in sys.path:
        sys.path.insert(0, research_root)

    spec = importlib.util.spec_from_file_location("moltbook_research.molt_client", module_path)
    if spec is None or spec.loader is None:
        logger.warning("Unable to load Moltbook research module from %s", module_path)
        return None

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _acceptance_lines(task_contract: Any) -> list[str]:
    acceptance = getattr(task_contract, "acceptance", None)
    if acceptance is None:
        return []
    criteria = getattr(acceptance, "criteria", None)
    if isinstance(criteria, list):
        return [str(item) for item in criteria]
    checks = getattr(acceptance, "checks", None)
    if isinstance(checks, list):
        return [str(item) for item in checks]
    return []


class MoltBazaarClient:
    """Optional Moltbook bazaar bridge.

    MX keeps this compatibility layer so product code can remain stable while
    the concrete Moltbook implementation lives in the separate research workspace.
    """

    def __init__(self, submolt: str = "builds"):
        self.submolt = submolt
        self._module = _load_external_molt_module()

    @property
    def enabled(self) -> bool:
        return self._module is not None

    def _warn_unavailable(self) -> None:
        logger.info(
            "Moltbook bazaar integration is unavailable. Set MOLTBOOK_RESEARCH_ROOT to the separate "
            "Moltbook Research workspace to enable it."
        )

    async def post_task(self, task_contract: Any) -> str:
        if self._module is None:
            self._warn_unavailable()
            return ""

        title = f"BOUNTY [{task_contract.id}]: {task_contract.goal[:50]}"
        criteria = _acceptance_lines(task_contract)
        content = (
            f"GOAL: {task_contract.goal}\n\n"
            f"ACCEPTANCE CRITERIA:\n" + "\n".join([f"- {c}" for c in criteria]) + "\n\n"
            f"REWARD: x402 settlement in USDC\n"
            f"REF: {task_contract.id}\n\n"
            f"Interested agents, please BID by replying to this post."
        )

        logger.info("Posting task %s to m/%s", task_contract.id, self.submolt)
        result = self._module.create_post(self.submolt, title, content)
        if result and result.get("success"):
            return str(result["post"]["id"])
        return ""

    async def get_bids(self, post_id: str) -> list[dict[str, Any]]:
        if self._module is None:
            self._warn_unavailable()
            return []

        logger.info("Fetching bids for post %s", post_id)
        result = self._module.get_comments(post_id)
        if not result or not result.get("success"):
            return []

        bids: list[dict[str, Any]] = []
        for comment in result.get("comments", []):
            content = str(comment.get("content", "")).lower()
            if "bid" in content or "i can do this" in content or "interested" in content:
                bids.append(
                    {
                        "author": comment.get("author", {}).get("name"),
                        "author_id": comment.get("author_id"),
                        "content": comment.get("content"),
                        "comment_id": comment.get("id"),
                    }
                )
        return bids

    async def accept_bid(self, post_id: str, bid: dict[str, Any]) -> bool:
        if self._module is None:
            self._warn_unavailable()
            return False

        content = f"Awarded to u/{bid['author']}. Please proceed. Submit work via comment referencing Task ID."
        logger.info("Accepting bid from %s on post %s", bid["author"], post_id)
        result = self._module.create_comment(post_id, content)
        return bool(result and result.get("success"))

    async def get_comments(self, post_id: str) -> list[dict[str, Any]]:
        if self._module is None:
            self._warn_unavailable()
            return []

        logger.info("Fetching comments for post %s", post_id)
        result = self._module.get_comments(post_id)
        if not result or not result.get("success"):
            return []

        return [
            {
                "author": comment.get("author", {}).get("name"),
                "author_id": comment.get("author_id"),
                "content": comment.get("content"),
                "comment_id": comment.get("id"),
            }
            for comment in result.get("comments", [])
        ]

    async def create_comment(self, post_id: str, content: str) -> bool:
        if self._module is None:
            self._warn_unavailable()
            return False

        logger.info("Posting comment to post %s", post_id)
        result = self._module.create_comment(post_id, content)
        return bool(result and result.get("success"))

    def format_x402_header(self, amount: float, token: str, destination_wallet: str) -> dict[str, str]:
        return {
            "X-Payment-Request": json.dumps(
                {
                    "amount": amount,
                    "token": token,
                    "destination": destination_wallet,
                    "protocol": "x402",
                }
            )
        }
