import json
import os
import subprocess
from datetime import UTC, datetime
from typing import Any, cast


class MockGitHubAPI:
    """
    Mock GitHub API for Local Mode.
    Persists state to mock_prs.json in the repository root to allow
    coordination between Workers and Judge.
    Runs 'Auto-Merge' for non-draft PRs to simulate a healthy CI/CD pipeline.
    """

    def __init__(self, owner: str = "local", repo: str = "swarm", repo_path: str | None = None):
        self.owner = owner
        self.repo = repo
        self.repo_path = os.path.abspath(repo_path or os.getcwd())
        self.db_path = os.path.join(self.repo_path, "mock_prs.json")

    def _load_db(self) -> dict[str, Any]:
        if os.path.exists(self.db_path):
            try:
                with open(self.db_path) as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return cast(dict[str, Any], data)
                    return {"prs": [], "next_id": 1}
            except Exception:
                return {"prs": [], "next_id": 1}
        return {"prs": [], "next_id": 1}

    def _save_db(self, db: dict[str, Any]) -> None:
        with open(self.db_path, "w") as f:
            json.dump(db, f, indent=2)

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            capture_output=True,
            check=False,
            cwd=self.repo_path,
            text=True,
        )

    def _auto_merge_local_branch(self, head: str, base: str) -> tuple[bool, str]:
        if not os.path.isdir(os.path.join(self.repo_path, ".git")):
            return False, "repo_path does not contain a git repository"

        checkout = self._git("checkout", base)
        if checkout.returncode != 0:
            return False, checkout.stderr.strip() or checkout.stdout.strip() or f"checkout {base} failed"

        merge = self._git("merge", "--ff-only", head)
        if merge.returncode != 0:
            return False, merge.stderr.strip() or merge.stdout.strip() or f"merge {head} -> {base} failed"

        return True, ""

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

        merged = False
        merge_detail = ""
        if not draft:
            merged, merge_detail = self._auto_merge_local_branch(head, base)

        state = "closed" if merged and not draft else "open"
        merged_at = datetime.now(UTC).isoformat() if merged and not draft else None

        pr = {
            "number": pr_id,
            "html_url": f"http://localhost/mock/{self.owner}/{self.repo}/pull/{pr_id}",
            "title": title,
            "body": body,
            "state": state,
            "head": {"ref": head, "sha": "mock-sha"},
            "base": {"ref": base},
            "created_at": datetime.now(UTC).isoformat(),
            "merged_at": merged_at,
            "merged": merged,
            "draft": draft,
            "user": {"login": "mx-worker-local"},
            "merge_detail": merge_detail,
        }

        db["prs"].append(pr)
        self._save_db(db)

        print(f"[MockGitHub] Created PR #{pr_id}: {title} (State: {state})")
        return pr

    async def get_pull_request(self, number: int) -> dict[str, Any]:
        db = self._load_db()
        for pr in db["prs"]:
            if isinstance(pr, dict) and pr.get("number") == number:
                return cast(dict[str, Any], pr)
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
