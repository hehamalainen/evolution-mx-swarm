import asyncio
import os
import shutil
import logging
from shared.utils import MXConfig, create_llm_client
from planner.service import Planner
from shared.utils.bazaar_client import MoltBazaarClient
from shared.schemas import TaskContract, TaskType

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def main():
    config = MXConfig.load_or_create()
    # Ensure LLM is configured (defaulting to whatever is in env/config)
    
    # Setup temp workspace for Planner to find "PRD.md"
    temp_dir = "temp_planner_ws"
    os.makedirs(temp_dir, exist_ok=True)
    shutil.copy("prds/health_check.md", os.path.join(temp_dir, "PRD.md"))
    
    planner = Planner(config, repo_path=temp_dir)
    bazaar = MoltBazaarClient(submolt="builds")
    
    logger.info("--- Phase 1: Decomposition ---")
    spec = await planner.generate_spec(force=True)
    tasks = await planner.generate_tasks(spec)
    
    logger.info(f"Generated {len(tasks)} tasks.")
    
    # Find a task suitable for outsourcing (e.g., git hash retrieval)
    outsourced_task = None
    for task in tasks:
        if "git" in task.goal.lower() or "hash" in task.goal.lower():
            outsourced_task = task
            break
    
    if not outsourced_task and tasks:
        outsourced_task = tasks[0] # Fallback
        
    if outsourced_task:
        logger.info(f"--- Phase 2: Outsourcing Task {outsourced_task.id} ---")
        post_id = await bazaar.post_task(outsourced_task)
        if post_id:
            logger.info(f"SUCCESS: Task posted to Moltbook! Post ID: {post_id}")
            logger.info(f"URL: https://www.moltbook.com/posts/{post_id}")
        else:
            logger.error("FAILED to post task to Moltbook.")
    else:
        logger.error("No tasks found to outsource.")

    # Cleanup
    shutil.rmtree(temp_dir)

if __name__ == "__main__":
    asyncio.run(main())
