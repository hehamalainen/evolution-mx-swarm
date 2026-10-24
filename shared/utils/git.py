"""Git utilities for MX Swarm workers."""

import asyncio
import base64
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

_URL_USERINFO_PATTERN = re.compile(r"(https?://)([^/\s@]+@)")


def _strip_url_credentials(url: str) -> tuple[str, str | None]:
    """Strip any embedded userinfo from an HTTP(S) URL and return (clean_url, extracted_secret)."""
    if not url.startswith(("http://", "https://")):
        return url, None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return _URL_USERINFO_PATTERN.sub(r"\1", url), None
    if parsed.username is None and parsed.password is None:
        return url, None
    extracted = parsed.password or parsed.username
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    clean_url = urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))
    return clean_url, extracted


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
    env: dict[str, str] | None = None,
) -> GitResult:
    """
    Run a git command asynchronously with workspace hooks and unsafe execution paths disabled.

    Args:
        args: Git command arguments (without 'git')
        cwd: Working directory
        env: Environment variables

    Returns:
        GitResult with output and status
    """
    empty_hooks_dir = tempfile.mkdtemp(prefix="mx-git-hooks-")
    os.chmod(empty_hooks_dir, 0o700)
    try:
        cmd = [
            "git",
            "-c",
            f"core.hooksPath={empty_hooks_dir}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.sshCommand=false",
            "-c",
            "core.pager=cat",
            "-c",
            "credential.helper=",
            "-c",
            "protocol.ext.allow=never",
            "-c",
            "submodule.recurse=false",
            *args,
        ]

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

        return_code = proc.returncode if proc.returncode is not None else 1
        return GitResult(
            success=return_code == 0,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            return_code=return_code,
        )
    finally:
        shutil.rmtree(empty_hooks_dir, ignore_errors=True)


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
        clean_url, embedded_token = _strip_url_credentials(repo_url)
        self.repo_url = clean_url
        self.local_path = local_path or tempfile.mkdtemp(prefix="mx-repo-")
        self.token = token if token is not None else embedded_token
        self._cloned = False

    @property
    def authenticated_url(self) -> str:
        """Return the credential-free repository URL.

        Authentication is supplied ephemerally per Git subprocess and never embedded
        in URLs, command arguments, or `.git/config`.
        """
        return self.repo_url

    def _redact_text(self, text: str, secrets: list[str]) -> str:
        redacted = _URL_USERINFO_PATTERN.sub(r"\1<REDACTED_CREDENTIALS>@", text)
        for secret in secrets:
            if secret:
                redacted = redacted.replace(secret, "<REDACTED_TOKEN>")
        return redacted

    def _canonical_git_config(self) -> str:
        """Build a trusted, minimal .git/config with no hooks, helpers, or custom commands."""
        lines = [
            "[core]",
            "\trepositoryformatversion = 0",
            "\tfilemode = true",
            "\tbare = false",
            "\tlogallrefupdates = true",
        ]
        if self.repo_url:
            lines.extend(
                [
                    '[remote "origin"]',
                    f"\turl = {self.repo_url}",
                    "\tfetch = +refs/heads/*:refs/remotes/origin/*",
                ]
            )
        return "\n".join(lines) + "\n"

    @staticmethod
    def _force_remove_tree_or_file(target_path: str) -> None:
        """Remove a file, symlink, or directory tree even if permissions were made read-only."""
        if not os.path.lexists(target_path):
            return
        if os.path.islink(target_path) or not os.path.isdir(target_path):
            try:
                os.unlink(target_path)
            except OSError:
                pass
            return
        try:
            os.chmod(target_path, 0o700)
        except OSError:
            pass
        for root, dirs, files in os.walk(target_path, topdown=False):
            for name in files:
                entry = os.path.join(root, name)
                try:
                    os.unlink(entry)
                except OSError:
                    pass
            for name in dirs:
                entry = os.path.join(root, name)
                try:
                    if os.path.islink(entry):
                        os.unlink(entry)
                    else:
                        os.chmod(entry, 0o700)
                        os.rmdir(entry)
                except OSError:
                    pass
        shutil.rmtree(target_path, ignore_errors=True)

    def scrub_workspace_credentials(self) -> None:
        """Restore trusted workspace .git metadata and remove hooks, credentials, or config tampering."""
        if not self.local_path or not os.path.lexists(self.local_path):
            return
        if os.path.islink(self.local_path) or not os.path.isdir(self.local_path):
            return
        git_dir = os.path.join(self.local_path, ".git")
        if not os.path.lexists(git_dir):
            return
        if os.path.islink(git_dir) or not os.path.isdir(git_dir):
            try:
                os.unlink(git_dir)
            except OSError:
                pass
            return

        try:
            os.chmod(git_dir, 0o700)
        except OSError:
            pass

        for target in (
            os.path.join(git_dir, "credentials"),
            os.path.join(git_dir, ".git-credentials"),
            os.path.join(self.local_path, ".git-credentials"),
            os.path.join(git_dir, "info", "attributes"),
            os.path.join(git_dir, "info", "grafts"),
            os.path.join(git_dir, "remotes"),
            os.path.join(git_dir, "branches"),
            os.path.join(git_dir, "modules"),
            os.path.join(git_dir, "worktrees"),
        ):
            self._force_remove_tree_or_file(target)

        hooks_dir = os.path.join(git_dir, "hooks")
        self._force_remove_tree_or_file(hooks_dir)
        os.makedirs(hooks_dir, mode=0o700, exist_ok=True)

        config_path = os.path.join(git_dir, "config")
        canonical_config = self._canonical_git_config()
        try:
            if os.path.lexists(config_path):
                os.unlink(config_path)
            with open(config_path, "w", encoding="utf-8") as handle:
                handle.write(canonical_config)
            os.chmod(config_path, 0o600)
        except OSError:
            pass

    def snapshot_trusted_git_dir(self) -> str | None:
        """Create an out-of-workspace snapshot of the trusted .git directory before untrusted execution."""
        self.scrub_workspace_credentials()
        if not self.local_path or not os.path.isdir(self.local_path):
            return None
        git_dir = os.path.join(self.local_path, ".git")
        if not os.path.isdir(git_dir) or os.path.islink(git_dir):
            return None
        snapshot_root = tempfile.mkdtemp(prefix="mx-git-trusted-")
        os.chmod(snapshot_root, 0o700)
        shutil.copytree(git_dir, os.path.join(snapshot_root, "git"), symlinks=False)
        return snapshot_root

    def restore_trusted_git_dir(self, snapshot_root: str | None) -> None:
        """Restore the workspace .git directory from a trusted snapshot and scrub metadata."""
        try:
            if (
                snapshot_root
                and os.path.isdir(snapshot_root)
                and self.local_path
                and os.path.isdir(self.local_path)
            ):
                snapshot_git = os.path.join(snapshot_root, "git")
                if os.path.isdir(snapshot_git):
                    git_dir = os.path.join(self.local_path, ".git")
                    self._force_remove_tree_or_file(git_dir)
                    shutil.copytree(snapshot_git, git_dir, symlinks=False)
        finally:
            if snapshot_root and os.path.exists(snapshot_root):
                shutil.rmtree(snapshot_root, ignore_errors=True)
            self.scrub_workspace_credentials()

    async def _run_local_git(self, args: list[str]) -> GitResult:
        """Run a non-authenticated repository Git operation after enforcing trusted metadata."""
        self.scrub_workspace_credentials()
        try:
            return await run_git(args, cwd=self.local_path)
        finally:
            self.scrub_workspace_credentials()

    async def _run_authenticated_git(
        self,
        args: list[str],
        cwd: str | None = None,
    ) -> GitResult:
        """Run a remote Git operation using ephemeral process-only authentication and trusted config."""
        self.scrub_workspace_credentials()
        sanitized_args = [_strip_url_credentials(arg)[0] for arg in args]
        if not self.token or os.path.isdir(self.repo_url):
            try:
                return await run_git(sanitized_args, cwd=cwd)
            finally:
                self.scrub_workspace_credentials()

        auth_dir = tempfile.mkdtemp(prefix="mx-git-auth-")
        os.chmod(auth_dir, 0o700)
        empty_hooks_dir = os.path.join(auth_dir, "empty-hooks")
        os.makedirs(empty_hooks_dir, mode=0o700, exist_ok=True)
        token_path = os.path.join(auth_dir, "token")
        askpass_path = os.path.join(auth_dir, "git-askpass.sh")
        basic_secret = base64.b64encode(
            f"x-access-token:{self.token}".encode()
        ).decode("ascii")

        try:
            with open(token_path, "w", encoding="utf-8") as token_handle:
                token_handle.write(self.token)
            os.chmod(token_path, stat.S_IRUSR | stat.S_IWUSR)

            quoted_token_path = token_path.replace("'", "'\\''")
            with open(askpass_path, "w", encoding="utf-8") as handle:
                handle.write(
                    "#!/bin/sh\n"
                    'case "$1" in\n'
                    '  *Username*|*username*) printf \'%s\\n\' "x-access-token" ;;\n'
                    f"  *) cat '{quoted_token_path}' ;;\n"
                    "esac\n"
                )
            os.chmod(askpass_path, stat.S_IRWXU)

            auth_env: dict[str, str] = {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS": askpass_path,
            }
            config_entries: list[tuple[str, str]] = [
                ("core.hooksPath", empty_hooks_dir),
                ("core.fsmonitor", "false"),
                ("core.sshCommand", "false"),
                ("core.pager", "cat"),
                ("credential.helper", ""),
                ("protocol.ext.allow", "never"),
                ("submodule.recurse", "false"),
                ("remote.origin.uploadpack", "git-upload-pack"),
                ("remote.origin.receivepack", "git-receive-pack"),
                ("remote.origin.url", self.repo_url),
            ]
            parsed = urlsplit(self.repo_url)
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                origin_prefix = f"{parsed.scheme}://{parsed.netloc}/"
                config_entries.append(
                    (f"http.{origin_prefix}.extraheader", f"AUTHORIZATION: basic {basic_secret}")
                )

            try:
                existing_count = int(os.environ.get("GIT_CONFIG_COUNT", "0") or "0")
            except ValueError:
                existing_count = 0
            auth_env["GIT_CONFIG_COUNT"] = str(existing_count + len(config_entries))
            for offset, (cfg_key, cfg_val) in enumerate(config_entries):
                idx = existing_count + offset
                auth_env[f"GIT_CONFIG_KEY_{idx}"] = cfg_key
                auth_env[f"GIT_CONFIG_VALUE_{idx}"] = cfg_val

            result = await run_git(sanitized_args, cwd=cwd, env=auth_env)
            secrets_to_redact = [self.token, basic_secret]
            return GitResult(
                success=result.success,
                stdout=self._redact_text(result.stdout, secrets_to_redact),
                stderr=self._redact_text(result.stderr, secrets_to_redact),
                return_code=result.return_code,
            )
        finally:
            shutil.rmtree(auth_dir, ignore_errors=True)
            self.scrub_workspace_credentials()

    async def clone(self, branch: str | None = None, depth: int = 1) -> GitResult:
        """
        Clone repository.

        Args:
            branch: Branch to checkout (default: default branch)
            depth: Clone depth (default: shallow clone)

        Returns:
            GitResult
        """
        repo_is_dir = os.path.isdir(self.repo_url)
        print(
            "[git.clone] "
            f"repo_url={self.repo_url!r} "
            f"is_dir={repo_is_dir} "
            f"branch={branch!r}"
        )

        if repo_is_dir:
            # Local-mode clones should preserve the source repo's current HEAD.
            # Using `--branch` here targets remote refs and breaks isolated
            # filesystem clones where the checked-out working branch is not
            # exposed as an upstream branch.
            args = ["clone", self.repo_url, self.local_path]
        else:
            args = ["clone", "--depth", str(depth)]

            if branch:
                args.extend(["--branch", branch])

            args.extend([self.repo_url, self.local_path])

        result = await self._run_authenticated_git(args)
        self._cloned = result.success
        if not result.success and os.path.isdir(self.local_path):
            self.scrub_workspace_credentials()
        return result

    async def fetch(self, branch: str | None = None) -> GitResult:
        """Fetch from remote."""
        args = ["fetch", "origin"]
        if branch:
            args.append(branch)
        return await self._run_authenticated_git(args, cwd=self.local_path)

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

        return await self._run_local_git(args)

    async def pull(self, branch: str | None = None) -> GitResult:
        """Pull from remote."""
        args = ["pull", "origin"]
        if branch:
            args.append(branch)
        return await self._run_authenticated_git(args, cwd=self.local_path)

    async def add(self, paths: list[str] | None = None) -> GitResult:
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

        return await self._run_local_git(args)

    async def commit(self, message: str, author: str | None = None) -> GitResult:
        """
        Create commit.

        Args:
            message: Commit message
            author: Author string (e.g., "Name <email>")

        Returns:
            GitResult
        """
        args = ["commit", "--no-verify", "-m", message]

        if author:
            args.extend(["--author", author])

        return await self._run_local_git(args)

    async def push(self, branch: str, force: bool = False) -> GitResult:
        """
        Push to remote.

        Args:
            branch: Branch to push
            force: Force push

        Returns:
            GitResult
        """
        args = ["push", "--no-verify", "-u", "origin", branch]

        if force:
            args.insert(1, "--force")

        return await self._run_authenticated_git(args, cwd=self.local_path)

    async def get_current_branch(self) -> str | None:
        """Get current branch name."""
        result = await self._run_local_git(["branch", "--show-current"])
        if result.success:
            return result.stdout.strip()
        return None

    async def get_status(self) -> GitResult:
        """Get repository status."""
        return await self._run_local_git(["status", "--porcelain"])

    async def get_diff(self, staged: bool = False) -> GitResult:
        """Get diff."""
        args = ["diff"]
        if staged:
            args.append("--staged")
        return await self._run_local_git(args)

    async def diff(self, ref_from: str, ref_to: str) -> GitResult:
        """Get diff between two refs (commits/branches).

        Args:
            ref_from: Starting ref (e.g., 'HEAD~1', 'main')
            ref_to: Ending ref (e.g., 'HEAD', 'feature-branch')

        Returns:
            GitResult with unified diff in stdout
        """
        args = ["diff", ref_from, ref_to]
        return await self._run_local_git(args)

    async def get_log(self, count: int = 10, oneline: bool = True) -> GitResult:
        """Get commit log."""
        args = ["log", f"-{count}"]
        if oneline:
            args.append("--oneline")
        return await self._run_local_git(args)

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

    @staticmethod
    def _as_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return cast(dict[str, Any], value)
        return {}

    @staticmethod
    def _as_list_of_dicts(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        return [cast(dict[str, Any], item) for item in value if isinstance(item, dict)]

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
        data: dict[str, Any] | None = None,
    ) -> Any:
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
    ) -> dict[str, Any]:
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
        return self._as_dict(await self._request(
            "POST",
            "/pulls",
            data={
                "title": title,
                "body": body,
                "head": head,
                "base": base,
                "draft": draft,
            },
        ))

    async def get_pull_request(self, number: int) -> dict[str, Any]:
        """Get pull request details."""
        return self._as_dict(await self._request("GET", f"/pulls/{number}"))

    async def list_pull_requests(
        self,
        state: str = "open",
        head: str | None = None,
    ) -> list[dict[str, Any]]:
        """List pull requests."""
        params = f"?state={state}"
        if head:
            params += f"&head={self.owner}:{head}"
        return self._as_list_of_dicts(await self._request("GET", f"/pulls{params}"))

    async def add_pr_comment(self, number: int, body: str) -> dict[str, Any]:
        """Add comment to pull request."""
        return self._as_dict(await self._request(
            "POST",
            f"/issues/{number}/comments",
            data={"body": body},
        ))

    async def merge_pull_request(
        self,
        number: int,
        merge_method: str = "squash",
        commit_message: str | None = None,
    ) -> dict[str, Any]:
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

        return self._as_dict(await self._request("PUT", f"/pulls/{number}/merge", data=data))

    async def get_check_runs(self, ref: str) -> list[dict[str, Any]]:
        """Get check runs for a ref (branch/commit)."""
        result = self._as_dict(await self._request("GET", f"/commits/{ref}/check-runs"))
        return self._as_list_of_dicts(result.get("check_runs"))

    async def get_combined_status(self, ref: str) -> dict[str, Any]:
        """Get combined status for a ref."""
        return self._as_dict(await self._request("GET", f"/commits/{ref}/status"))

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
