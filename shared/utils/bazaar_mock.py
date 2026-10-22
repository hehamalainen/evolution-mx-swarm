"""MoltBazaar Mock - Simulated Decentralized Marketplace Integration.

Simulates posting tasks to a public bazaar and receiving bids from agents.
This is used to demonstrate the "hiring" flow in MX Swarm.
"""

import asyncio
import random
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class BazaarBid:
    """A simulated bid from an external agent."""
    bid_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    agent_id: str = "u/External-Agent"
    trust_score: int = 80
    bid_amount_tokens: int = 100
    estimated_delivery_mins: int = 60
    notes: str = "Ready to assist."


class MoltBazaarMock:
    """Mock implementation of MoltBazaar for task outsourcing."""

    def __init__(self):
        self.active_postings: dict[str, dict] = {}

    async def post_task(self, task_contract: dict) -> str:
        """Post a task to the bazaar and return a posting ID."""
        posting_id = f"post-{uuid.uuid4().hex[:8]}"
        self.active_postings[posting_id] = {
            "task": task_contract,
            "posted_at": datetime.now(UTC).isoformat(),
            "status": "open"
        }
        print(f"[BazaarMock] Posted task {task_contract.get('id')} to bazaar: {posting_id}")
        return posting_id

    async def get_bids(self, posting_id: str) -> list[BazaarBid]:
        """Simulate receiving bids for a posting."""
        if posting_id not in self.active_postings:
            return []

        # Simulate delay
        await asyncio.sleep(1)

        # Generate 1-3 mock bids
        bids = []
        for _i in range(random.randint(1, 3)):
            agent_id = f"u/Agent-{random.choice(['Alpha', 'Beta', 'Gamma', 'Delta'])}-{random.randint(100, 999)}"
            bids.append(BazaarBid(
                agent_id=agent_id,
                trust_score=random.randint(50, 95),
                bid_amount_tokens=random.randint(50, 200),
                estimated_delivery_mins=random.randint(30, 120),
                notes=f"Experienced in {random.choice(['Python', 'TypeScript', 'DevOps', 'QA'])}."
            ))

        return bids

    async def accept_bid(self, posting_id: str, bid_id: str) -> bool:
        """Simulate accepting a bid and awarding the contract."""
        if posting_id in self.active_postings:
            self.active_postings[posting_id]["status"] = "awarded"
            print(f"[BazaarMock] Bid {bid_id} accepted for posting {posting_id}")
            return True
        return False
