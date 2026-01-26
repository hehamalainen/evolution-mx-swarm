"""Git utilities for MX Swarm workers."""

import asyncio
import os
import shutil
import tempfile
from dataclasses import dataclass


@dataclass
class GitResult:
    """Result of a git operation."""
    success: bool
    stdout: str
    stderr: str
    return_code: int


async def run_git(
    args: list[str],
    cwd: str | None = None,
    env: dict | None = None,
) -> GitResult:
    """
    Run a git command asynchronously.

    Args:
        args: Git command arguments (without 'git')
        cwd: Working directory
        env: Environment variables

    Returns:
        GitResult with output and status
    """
    cmd = ["git"] + args

    # Merge environment
    full_env = os.environ.copy()
    if env:
        full_env.update(env)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=full_env,
    )

    stdout, stderr = await proc.communicate()

    return GitResult(
        success=proc.returncode == 0,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
        return_code=proc.returncode,
    )


class GitRepo:
    """Git repository helper for MX Swarm workers."""

    def __init__(
        self,
        repo_url: str,
        local_path: str | None = None,
        token: str | None = None,
    ):
        """
        Initialize GitRepo.

        Args:
            repo_url: Repository URL (https://github.com/owner/repo.git)
            local_path: Local path for clone (temp dir if not specified)
            token: GitHub token for authentication
        """
        self.repo_url = repo_url
        self.local_path = local_path or tempfile.mkdtemp(prefix="mx-repo-")
        self.token = token
        self._cloned = False

    @property
    def authenticated_url(self) -> str:
        """Get repo URL with token authentication."""
        if not self.token:
            return self.repo_url

        # Insert token into URL
        if "https://" in self.repo_url:
            return self.repo_url.replace(
                "https://",
                f"https://x-access-token:{self.token}@"
            )
        return self.repo_url

    async def clone(self, branch: str | None = None, depth: int = 1) -> GitResult:
        """
        Clone repository.

        Args:
            branch: Branch to checkout (default: default branch)
            depth: Clone depth (default: shallow clone)

        Returns:
            GitResult
        """
        args = ["clone", "--depth", str(depth)]

        if branch:
            args.extend(["--branch", branch])

        args.extend([self.authenticated_url, self.local_path])

        result = await run_git(args)
        self._cloned = result.success
        return result

    async def fetch(self, branch: str | None = None) -> GitResult:
        """Fetch from remote."""
        args = ["fetch", "origin"]
        if branch:
            args.append(branch)
        return await run_git(args, cwd=self.local_path)

    async def checkout(self, branch: str, create: bool = False) -> GitResult:
        """
        Checkout branch.

        Args:
            branch: Branch name
            create: Create branch if it doesn't exist

        Returns:
            GitResult
        """
        if create:
            args = ["checkout", "-b", branch]
        else:
            args = ["checkout", branch]

        return await run_git(args, cwd=self.local_path)

    async def pull(self, branch: str | None = None) -> GitResult:
        """Pull from remote."""
        args = ["pull", "origin"]
        if branch:
            args.append(branch)
        return await run_git(args, cwd=self.local_path)

    async def add(self, paths: list[str] = None) -> GitResult:
        """
        Stage files.

        Args:
            paths: Files to stage (default: all)

        Returns:
            GitResult
        """
        args = ["add"]
        if paths:
            args.extend(paths)
        else:
            args.append("-A")

        return await run_git(args, cwd=self.local_path)

    async def commit(self, message: str, author: str | None = None) -> GitResult:
        """
        Create commit.

        Args:
            message: Commit message
            author: Author string (e.g., "Name <email>")

        Returns:
            GitResult
        """
        args = ["commit", "-m", message]

        if author:
            args.extend(["--author", author])

        return await run_git(args, cwd=self.local_path)

    async def push(self, branch: str, force: bool = False) -> GitResult:
        """
        Push to remote.

        Args:
            branch: Branch to push
            force: Force push

        Returns:
            GitResult
        """
        args = ["push", "-u", "origin", branch]

        if force:
            args.insert(1, "--force")

        return await run_git(args, cwd=self.local_path)

    async def get_current_branch(self) -> str | None:
        """Get current branch name."""
        result = await run_git(["branch", "--show-current"], cwd=self.local_path)
        if result.success:
            return result.stdout.strip()
        return None

    async def get_status(self) -> GitResult:
        """Get repository status."""
        return await run_git(["status", "--porcelain"], cwd=self.local_path)

    async def get_diff(self, staged: bool = False) -> GitResult:
        """Get diff."""
        args = ["diff"]
        if staged:
            args.append("--staged")
        return await run_git(args, cwd=self.local_path)

    async def get_log(self, count: int = 10, oneline: bool = True) -> GitResult:
        """Get commit log."""
        args = ["log", f"-{count}"]
        if oneline:
            args.append("--oneline")
        return await run_git(args, cwd=self.local_path)

    async def has_changes(self) -> bool:
        """Check if there are uncommitted changes."""
        result = await self.get_status()
        return bool(result.stdout.strip())

    def cleanup(self) -> None:
        """Remove local repository directory."""
        if os.path.exists(self.local_path):
            shutil.rmtree(self.local_path, ignore_errors=True)


class GitHubAPI:
    """GitHub API helper for MX Swarm."""

    def __init__(self, token: str, owner: str, repo: str):
        """
        Initialize GitHub API helper.

        Args:
            token: GitHub token
            owner: Repository owner
            repo: Repository name
        """
        self.token = token
        self.owner = owner
        self.repo = repo
        self.base_url = f"https://api.github.com/repos/{owner}/{repo}"

    async def _request(
        self,
        method: str,
        endpoint: str,
        data: dict | None = None,
    ) -> dict:
        """Make authenticated API request."""
        try:
            import httpx
        except ImportError:
            raise ImportError("httpx package required. Install with: pip install httpx") from None

        url = f"{self.base_url}{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        async with httpx.AsyncClient() as client:
            response = await client.request(
                method,
                url,
                headers=headers,
                json=data,
                timeout=30,
            )
            response.raise_for_status()
            return response.json() if response.content else {}

    async def create_pull_request(
        self,
        title: str,
        body: str,
        head: str,
        base: str = "main",
        draft: bool = False,
    ) -> dict:
        """
        Create a pull request.

        Args:
            title: PR title
            body: PR description (markdown)
            head: Head branch
            base: Base branch
            draft: Create as draft PR

        Returns:
            PR data from GitHub API
        """
        return await self._request(
            "POST",
            "/pulls",
            data={
                "title": title,
                "body": body,
                "head": head,
                "base": base,
                "draft": draft,
            },
        )

    async def get_pull_request(self, number: int) -> dict:
        """Get pull request details."""
        return await self._request("GET", f"/pulls/{number}")

    async def list_pull_requests(
        self,
        state: str = "open",
        head: str | None = None,
    ) -> list[dict]:
        """List pull requests."""
        params = f"?state={state}"
        if head:
            params += f"&head={self.owner}:{head}"
        return await self._request("GET", f"/pulls{params}")

    async def add_pr_comment(self, number: int, body: str) -> dict:
        """Add comment to pull request."""
        return await self._request(
            "POST",
            f"/issues/{number}/comments",
            data={"body": body},
        )

    async def merge_pull_request(
        self,
        number: int,
        merge_method: str = "squash",
        commit_message: str | None = None,
    ) -> dict:
        """
        Merge a pull request.

        Args:
            number: PR number
            merge_method: merge, squash, or rebase
            commit_message: Custom commit message

        Returns:
            Merge result
        """
        data = {"merge_method": merge_method}
        if commit_message:
            data["commit_message"] = commit_message

        return await self._request("PUT", f"/pulls/{number}/merge", data=data)

    async def get_check_runs(self, ref: str) -> list[dict]:
        """Get check runs for a ref (branch/commit)."""
        result = await self._request("GET", f"/commits/{ref}/check-runs")
        return result.get("check_runs", [])

    async def get_combined_status(self, ref: str) -> dict:
        """Get combined status for a ref."""
        return await self._request("GET", f"/commits/{ref}/status")

    async def all_checks_passed(self, ref: str) -> bool:
        """Check if all CI checks have passed."""
        # Check status checks
        status = await self.get_combined_status(ref)
        if status.get("state") not in ("success", "pending"):
            return False

        # Check check runs
        runs = await self.get_check_runs(ref)
        for run in runs:
            if run.get("conclusion") not in ("success", "skipped", None):
                return False
            if run.get("status") not in ("completed", "queued", "in_progress"):
                return False

        return True
