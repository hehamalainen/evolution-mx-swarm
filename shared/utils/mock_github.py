import json
import os
from datetime import datetime
from typing import Any


class MockGitHubAPI:
    """
    Mock GitHub API for Local Mode.
    Persists state to mock_prs.json in the repository root to allow
    coordination between Workers and Judge.
    Runs 'Auto-Merge' for non-draft PRs to simulate a healthy CI/CD pipeline.
    """

    def __init__(self, owner: str = "local", repo: str = "swarm"):
        self.owner = owner
        self.repo = repo
        self.db_path = "mock_prs.json"

    def _load_db(self) -> dict[str, Any]:
        if os.path.exists(self.db_path):
            try:
                with open(self.db_path) as f:
                    return json.load(f)
            except Exception:
                return {"prs": [], "next_id": 1}
        return {"prs": [], "next_id": 1}

    def _save_db(self, db: dict[str, Any]):
        with open(self.db_path, "w") as f:
            json.dump(db, f, indent=2)

    async def create_pull_request(
        self,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool = False
    ) -> dict[str, Any]:
        """Create a mock PR. Auto-merges if not a draft."""
        db = self._load_db()
        pr_id = db["next_id"]
        db["next_id"] += 1

        # Simulate Auto-Merge for passed tests (non-draft)
        state = "closed" if not draft else "open"
        merged_at = datetime.utcnow().isoformat() if not draft else None

        pr = {
            "number": pr_id,
            "html_url": f"http://localhost/mock/{self.owner}/{self.repo}/pull/{pr_id}",
            "title": title,
            "body": body,
            "state": state,
            "head": {"ref": head, "sha": "mock-sha"},
            "base": {"ref": base},
            "created_at": datetime.utcnow().isoformat(),
            "merged_at": merged_at,
            "draft": draft,
            "user": {"login": "mx-worker-local"}
        }

        db["prs"].append(pr)
        self._save_db(db)

        print(f"[MockGitHub] Created PR #{pr_id}: {title} (State: {state})")
        return pr

    async def get_pull_request(self, number: int) -> dict[str, Any]:
        db = self._load_db()
        for pr in db["prs"]:
            if pr["number"] == number:
                return pr
        return {"id": number, "state": "closed"} # Default fallback

    async def list_pull_requests(self, state: str = "open", head: str | None = None) -> list[dict[str, Any]]:
        db = self._load_db()
        result = []
        for pr in db["prs"]:
            # Filter by state
            if state == "open" and pr["state"] != "open":
                continue
            if state == "closed" and pr["state"] != "closed":
                continue

            # Filter by head branch
            if head and pr["head"]["ref"] != head:
                continue

            result.append(pr)
        return result

    async def all_checks_passed(self, ref: str) -> bool:
        """In local mode, we assume checks passed if the worker submitted."""
        return True

    def get_repo_url(self) -> str:
        """Return local CWD as repo URL."""
        return os.getcwd()
