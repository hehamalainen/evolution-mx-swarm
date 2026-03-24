import json
import logging
import uuid
from typing import Optional

from shared.utils.bazaar_mock import MoltBazaarMock
from molt_client import create_post, get_feed, create_comment, get_comments

logger = logging.getLogger(__name__)

class MoltBazaarClient:
    """
    Real Moltbook Bazaar integration.
    Wraps molt_client.py logic to interact with the platform.
    """
    
    def __init__(self, submolt: str = "builds"):
        self.submolt = submolt

    async def post_task(self, task_contract) -> str:
        """Post a TaskContract as a Bounty on Moltbook."""
        title = f"BOUNTY [{task_contract.id}]: {task_contract.goal[:50]}"
        content = (
            f"GOAL: {task_contract.goal}\n\n"
            f"ACCEPTANCE CRITERIA:\n" + "\n".join([f"- {c}" for c in task_contract.acceptance.criteria]) + "\n\n"
            f"REWARD: x402 settlement in USDC\n"
            f"REF: {task_contract.id}\n\n"
            f"Interested agents, please BID by replying to this post."
        )
        
        logger.info(f"Posting task {task_contract.id} to m/{self.submolt}")
        result = create_post(self.submolt, title, content)
        if result and result.get("success"):
            return result["post"]["id"]
        return ""

    async def get_bids(self, post_id: str) -> list[dict]:
        """Fetch comments for a post and filter for bids."""
        logger.info(f"Fetching bids for post {post_id}")
        result = get_comments(post_id)
        if not result or not result.get("success"):
            return []
            
        bids = []
        for comment in result.get("comments", []):
            content = comment.get("content", "").lower()
            if "bid" in content or "i can do this" in content or "interested" in content:
                bids.append({
                    "author": comment.get("author", {}).get("name"),
                    "author_id": comment.get("author_id"),
                    "content": comment.get("content"),
                    "comment_id": comment.get("id")
                })
        return bids

    async def accept_bid(self, post_id: str, bid: dict) -> bool:
        """Award the task to an agent via a comment."""
        content = f"Awarded to u/{bid['author']}. Please proceed. Submit work via comment referencing Task ID."
        logger.info(f"Accepting bid from {bid['author']} on post {post_id}")
        result = create_comment(post_id, content)
        return result and result.get("success")

    async def get_comments(self, post_id: str) -> list[dict]:
        """Fetch all comments for a post."""
        logger.info(f"Fetching comments for post {post_id}")
        result = get_comments(post_id)
        if not result or not result.get("success"):
            return []
            
        comments = []
        for comment in result.get("comments", []):
            comments.append({
                "author": comment.get("author", {}).get("name"),
                "author_id": comment.get("author_id"),
                "content": comment.get("content"),
                "comment_id": comment.get("id")
            })
        return comments

    async def create_comment(self, post_id: str, content: str) -> bool:
        """Post a generic comment to a thread."""
        logger.info(f"Posting comment to post {post_id}")
        result = create_comment(post_id, content)
        return result and result.get("success")

    def format_x402_header(self, amount: float, token: str, destination_wallet: str) -> dict:
        """Format the x402 Payment Required header for pay-mcp."""
        return {
            "X-Payment-Request": json.dumps({
                "amount": amount,
                "token": token,
                "destination": destination_wallet,
                "protocol": "x402"
            })
        }
