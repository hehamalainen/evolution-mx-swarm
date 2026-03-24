import asyncio
import logging
import json
import os
from typing import Optional
from shared.utils import MXConfig, create_llm_client, Message
from shared.utils.metrics import append_decision_event, append_submission_event
from shared.utils.bazaar_client import MoltBazaarClient
from shared.schemas import TaskContract, TaskState

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# The active bounty ID from our previous run
BOUNTY_ID = "db5c6297-b893-4d76-90a8-3e73fa3d7895"

class FullLoopOrchestrator:
    def __init__(self, config: MXConfig):
        self.config = config
        self.bazaar = MoltBazaarClient(submolt="builds")
        self.llm = create_llm_client(config.llm.to_dict())
        self.selected_agent: Optional[str] = None
        self.is_awarded = False

    async def poll_for_bids(self):
        """Phase 1: Monitor Moltbook for interested agents."""
        logger.info(f"Checking for existing award on bounty {BOUNTY_ID}...")
        comments = await self.bazaar.get_comments(BOUNTY_ID)
        for comment in comments:
            if "Awarded to u/" in comment.get("content", ""):
                agent_name = comment["content"].split("u/")[1].split(".")[0]
                self.selected_agent = agent_name
                self.is_awarded = True
                logger.info(f"RESUME: Task already awarded to u/{self.selected_agent}")
                return

        logger.info(f"Polling for bids on bounty {BOUNTY_ID}...")
        while not self.is_awarded:
            bids = await self.bazaar.get_bids(BOUNTY_ID)
            if bids:
                logger.info(f"Found {len(bids)} potential bids.")
                # For this demo, we pick the first bid
                selected_bid = bids[0]
                logger.info(f"Selecting bid from u/{selected_bid['author']}")
                
                success = await self.bazaar.accept_bid(BOUNTY_ID, selected_bid)
                if success:
                    self.selected_agent = selected_bid['author']
                    self.is_awarded = True
                    logger.info(f"SUCCESS: Task awarded to u/{self.selected_agent}")
                    append_decision_event(
                        task_id=BOUNTY_ID,
                        reasoning=f"Agent u/{self.selected_agent} submitted the first valid bid.",
                        choice="Award Bounty",
                        impact_level="high",
                        context={"bid_author": self.selected_agent}
                    )
                else:
                    logger.error("Failed to post award comment.")
            else:
                logger.info("No bids yet. Retrying in 30s...")
                await asyncio.sleep(30)

    async def wait_for_submission(self):
        """Phase 2: Monitor for the work submission."""
        logger.info(f"Waiting for submission from u/{self.selected_agent}...")
        while True:
            # We fetch all comments and look for code/submission markers
            comments = await self.bazaar.get_comments(BOUNTY_ID)
            for comment in comments:
                author = comment.get("author")
                content = comment.get("content", "")
                
                # Heuristic: looking for code blocks or common submission keywords
                submission_keywords = ["submission", "solution", "implementation", "import ", "def ", "result ="]
                is_submission = any(kw in content.lower() for kw in submission_keywords) or "```" in content
                
                if author == self.selected_agent and is_submission:
                    logger.info(f"Detected SUBMISSION from u/{self.selected_agent}")
                    return content
            
            logger.info("No submission yet. Polling in 300s...")
            await asyncio.sleep(300)

    async def verify_and_pay(self, submission_content: str):
        """Phase 3: Verify work and generate settlement header."""
        logger.info("Verifying submission...")
        
        # In a real scenario, we'd pass this to the Judge. 
        # For the duct-tape loop, we use the LLM to verify the content matches the requirement.
        verification_prompt = f"""
        Does the following submission correctly implement a Python script to retrieve the git commit short SHA?
        
        SUBMISSION:
        {submission_content}
        
        Respond only with 'PASS' or 'FAIL'.
        """
        
        response = await self.llm.chat(messages=[Message(role="user", content=verification_prompt)])
        result = response.content.strip()
        
        if "PASS" in result:
            logger.info("VERIFICATION PASSED!")
            
            # Generate x402 header
            payment_header = self.bazaar.format_x402_header(
                amount=0.5, # 0.5 USDC for the demo
                token="USDC",
                destination_wallet="0xAntigravityNexusSettlementAddress" # Placeholder
            )
            
            logger.info("GENERTING SETTLEMENT...")
            logger.info(f"x402 Header: {json.dumps(payment_header, indent=2)}")
            
            # Post final success comment
            award_msg = f"Work verified. Settlement issued via x402. Verification Result: {result}"
            await self.bazaar.create_comment(BOUNTY_ID, award_msg)
            logger.info("FULL LOOP COMPLETED SUCCESSFULLY!")
            append_decision_event(
                task_id=BOUNTY_ID,
                reasoning=f"LLM Judge verified submission from u/{self.selected_agent} as PASS.",
                choice="Issue x402 Settlement",
                impact_level="high",
                context={"verification_result": result}
            )
        else:
            logger.warning(f"VERIFICATION FAILED: {result}")

async def main():
    config = MXConfig.load_or_create()
    orchestrator = FullLoopOrchestrator(config)
    
    # Run the phases
    await orchestrator.poll_for_bids()
    submission = await orchestrator.wait_for_submission()
    await orchestrator.verify_and_pay(submission)

if __name__ == "__main__":
    asyncio.run(main())
