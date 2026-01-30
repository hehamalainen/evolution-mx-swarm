"""Worker Service - The Ralph Wiggum Loop.

Workers are the execution engines of MX Swarm:
1. Pull TaskContract from queue
2. Clone repo and checkout branch
3. Implement changes using LLM
4. Run acceptance checks
5. Open PR with evidence pack
6. Ack/Nack based on result
"""

import asyncio
import os
import signal
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from shared.adapters import (
    QueueAdapter,
    QueueMessage,
    VCSAdapter,
    create_queue_adapter,
    create_vcs_adapter,
)
from shared.schemas import (
    RiskLevel,
    TaskContract,
)
from shared.utils import (
    GitHubAPI,
    GitRepo,
    LLMClient,
    Message,
    MockGitHubAPI,
    MXConfig,
    create_llm_client,
)

WORKER_SYSTEM_PROMPT = """You are an expert software engineer implementing tasks autonomously.

When implementing a task:
1. Read and understand the codebase structure
2. Make minimal, focused changes
3. Follow existing code style and patterns
4. Add tests for new functionality
5. Document complex logic with comments

Guidelines:
- Never modify files in do_not_touch list
- Keep changes small and reviewable
- Prefer simple solutions over clever ones
- Ensure all tests pass before submitting

When outputting code changes, use this format:
```file:path/to/file.py
<complete file content>
```

For each file you modify, output the COMPLETE new content."""


@dataclass
class WorkerMetrics:
    """Worker execution metrics."""
    tasks_processed: int = 0
    tasks_succeeded: int = 0
    tasks_failed: int = 0
    prs_opened: int = 0
    llm_calls: int = 0
    total_tokens: int = 0
    start_time: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class EvidencePack:
    """Evidence pack for PR description."""
    task_id: str
    task_goal: str
    commands_run: list[tuple[str, bool, str]]  # (command, passed, output_summary)
    files_changed: list[str]
    verification_method: str
    known_risks: list[str]

    def to_markdown(self) -> str:
        """Generate markdown for PR description."""
        md = f"""## Evidence Pack

**Task ID:** `{self.task_id}`
**Goal:** {self.task_goal}

### Commands Run
| Command | Status | Summary |
|---------|--------|---------|
"""
        for cmd, passed, summary in self.commands_run:
            status = "✅ Pass" if passed else "❌ Fail"
            md += f"| `{cmd[:50]}` | {status} | {summary[:100]} |\n"

        md += """
### Files Changed
"""
        for f in self.files_changed:
            md += f"- `{f}`\n"

        md += f"""
### Verification
{self.verification_method}

### Known Risks
"""
        if self.known_risks:
            for risk in self.known_risks:
                md += f"- {risk}\n"
        else:
            md += "- No significant risks identified\n"

        return md


class Worker:
    """
    Worker service for MX Swarm.

    Implements the "Ralph Wiggum Loop":
    - Infinite loop with timeboxes
    - Automatic retries
    - Evidence-based PR creation
    """

    def __init__(
        self,
        config: MXConfig,
        worker_id: str | None = None,
        queue: QueueAdapter | None = None,
        llm: LLMClient | None = None,
        vcs: VCSAdapter | None = None,
    ):
        self.config = config
        self.worker_id = worker_id or f"worker-{str(uuid.uuid4())[:8]}"
        self.queue = queue or create_queue_adapter(config.queue.to_dict())
        self.llm = llm or create_llm_client(config.llm.to_dict())
        self.metrics = WorkerMetrics()
        self._running = False
        self._github: GitHubAPI | None = None

        # Initialize VCS adapter (Aether or GitHub)
        self._vcs = vcs or create_vcs_adapter(self._build_vcs_config())

    def _build_vcs_config(self) -> dict:
        """Build VCS config dict from MXConfig."""
        vcs_cfg = self.config.vcs
        if vcs_cfg.provider == "aether":
            return {
                "provider": "aether",
                "aether": {
                    "base_url": vcs_cfg.aether_base_url,
                    "repo_id": vcs_cfg.aether_repo_id,
                    "agent_id": vcs_cfg.aether_agent_id or self.worker_id,
                },
            }
        else:
            return {
                "provider": "github",
                "token": self.config.github.token,
                "owner": self.config.github.repo_owner,
                "repo": self.config.github.repo_name,
                "default_branch": self.config.github.default_branch,
            }

    @property
    def github(self) -> GitHubAPI:
        """Lazy-initialize GitHub API."""
        if self._github is None:
            token = self.config.github.token or os.environ.get("GITHUB_TOKEN")
            if not token:
                # Use Mock API in Local Mode
                print(f"[{self.worker_id}] No GITHUB_TOKEN found - using Mock GitHub API (Local Mode)")
                self._github = MockGitHubAPI()
            else:
                self._github = GitHubAPI(
                    token=token,
                    owner=self.config.github.repo_owner,
                    repo=self.config.github.repo_name,
                )
        return self._github

    async def initialize(self) -> None:
        """Initialize worker connections."""
        await self.queue.connect()

    async def shutdown(self) -> None:
        """Cleanup worker connections."""
        self._running = False
        await self.queue.disconnect()

    def _get_repo_url(self) -> str:
        """Get repository URL."""
        # In Aether mode, always use local filesystem as the source repo.
        if self.config.vcs.provider == "aether":
            return os.getcwd()

        # If running in Local Mode (no token), use local filesystem as 'remote'
        token = self.config.github.token or os.environ.get("GITHUB_TOKEN")
        if not token:
            return os.getcwd()

        owner = self.config.github.repo_owner
        repo = self.config.github.repo_name
        return f"https://github.com/{owner}/{repo}.git"

    async def _read_file(self, repo: GitRepo, path: str) -> str | None:
        """Read file from repo."""
        full_path = os.path.join(repo.local_path, path)
        if os.path.exists(full_path):
            with open(full_path) as f:
                return f.read()
        return None

    async def _write_file(self, repo: GitRepo, path: str, content: str) -> None:
        """Write file to repo."""
        full_path = os.path.join(repo.local_path, path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w") as f:
            f.write(content)

    async def _list_files(self, repo: GitRepo, pattern: str = "**/*") -> list[str]:
        """List files in repo matching pattern."""
        import glob
        files = glob.glob(
            os.path.join(repo.local_path, pattern),
            recursive=True,
        )
        # Return relative paths
        return [
            os.path.relpath(f, repo.local_path)
            for f in files
            if os.path.isfile(f)
        ]

    async def _run_command(
        self,
        command: str,
        cwd: str,
        timeout: int = 300,
    ) -> tuple[bool, str]:
        """
        Run shell command and return (success, output).
        """
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
            )

            stdout, _ = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )

            output = stdout.decode("utf-8", errors="replace")
            success = proc.returncode == 0

            return success, output[:5000]  # Truncate long outputs

        except TimeoutError:
            return False, f"Command timed out after {timeout}s"
        except Exception as e:
            return False, f"Error: {str(e)}"

    async def _get_codebase_context(self, repo: GitRepo, task: TaskContract) -> str:
        """Build context about the codebase for LLM."""
        context_parts = []

        # List relevant files
        if task.context.files_hint:
            for path in task.context.files_hint[:5]:
                content = await self._read_file(repo, path)
                if content:
                    context_parts.append(f"### {path}\n```\n{content[:3000]}\n```")

        # Try to find key files
        key_files = ["README.md", "pyproject.toml", "package.json", "setup.py"]
        for key_file in key_files:
            content = await self._read_file(repo, key_file)
            if content:
                context_parts.append(f"### {key_file}\n```\n{content[:2000]}\n```")
                break

        # List directory structure
        all_files = await self._list_files(repo, "**/*.py")
        all_files.extend(await self._list_files(repo, "**/*.ts"))
        all_files.extend(await self._list_files(repo, "**/*.js"))

        # Filter out common non-essential paths
        filtered = [
            f for f in all_files[:50]
            if not any(x in f for x in ["node_modules", "__pycache__", ".git", "dist", "build"])
        ]

        if filtered:
            context_parts.append("### File Structure\n```\n" + "\n".join(filtered[:30]) + "\n```")

        return "\n\n".join(context_parts)

    async def _implement_task(
        self,
        repo: GitRepo,
        task: TaskContract,
    ) -> tuple[list[str], str]:
        """
        Use LLM to implement the task.

        Returns:
            (list of changed files, implementation notes)
        """
        context = await self._get_codebase_context(repo, task)

        # Read AGENTS.md for guidelines
        agents_md = await self._read_file(repo, "AGENTS.md") or ""

        prompt = f"""Implement this task:

**Goal:** {task.goal}

**Task Type:** {task.type.value}

**Acceptance Criteria:**
{chr(10).join(f'- {c}' for c in task.acceptance.criteria)}

**Files to consider modifying:** {task.context.files_hint}
**DO NOT modify these files:** {task.context.do_not_touch}

**Agent Guidelines:**
{agents_md[:2000]}

**Codebase Context:**
{context}

Implement the task. For each file you create or modify, output:
```file:path/to/file.ext
<complete new file content>
```

After the implementation, provide a brief summary of what you did and any notes.
"""

        response = await self.llm.chat(
            messages=[Message(role="user", content=prompt)],
            system=WORKER_SYSTEM_PROMPT,
            max_tokens=self.config.llm.max_tokens,
            temperature=0.4,
        )

        self.metrics.llm_calls += 1
        if hasattr(response, "usage"):
            self.metrics.total_tokens += response.usage.get("input_tokens", 0)
            self.metrics.total_tokens += response.usage.get("output_tokens", 0)

        content = response.content

        # Debug: print LLM response preview
        print(f"[{self.worker_id}] LLM response preview: {content[:500]}...")

        # Parse file changes
        changed_files = []
        import re
        # Match multiple formats: ```file:path, ```python:path, ```typescript:path, etc.
        file_pattern = r'```(?:file|python|typescript|javascript|js|ts|py):([^\n]+)\n(.*?)```'
        matches = re.findall(file_pattern, content, re.DOTALL)

        # Also try to match ```python with # file: comment on first line
        if not matches:
            alt_pattern = r'```(?:python|typescript|javascript)?\n#\s*file:\s*([^\n]+)\n(.*?)```'
            matches = re.findall(alt_pattern, content, re.DOTALL)

        print(f"[{self.worker_id}] Found {len(matches)} file blocks in response")

        for path, file_content in matches:
            path = path.strip()

            # Skip if in do_not_touch
            if path in task.context.do_not_touch:
                continue

            await self._write_file(repo, path, file_content.strip())
            changed_files.append(path)

        # Extract notes (text after last code block)
        parts = content.split("```")
        notes = parts[-1].strip() if len(parts) > 1 else "Implementation complete."

        return changed_files, notes

    async def _run_acceptance_checks(
        self,
        repo: GitRepo,
        task: TaskContract,
    ) -> list[tuple[str, bool, str]]:
        """Run acceptance check commands."""
        results = []

        for check in task.acceptance.checks:
            passed, output = await self._run_command(
                check,
                cwd=repo.local_path,
                timeout=120,
            )

            # Summarize output
            lines = output.strip().split("\n")
            summary = lines[-1] if lines else "No output"
            if not passed and len(lines) > 1:
                summary = f"{lines[0]}...{lines[-1]}"

            results.append((check, passed, summary[:200]))

        return results

    async def _submit_to_vcs(
        self,
        repo: GitRepo,
        task: TaskContract,
        evidence: EvidencePack,
        changed_files: list[str],
        all_passed: bool,
        draft: bool = False,
    ) -> dict | None:
        """Submit code changes via VCS adapter (Aether or GitHub)."""
        # Generate diff for the commit
        # Try commit diff first, fallback to staged diff for shallow clones
        try:
            diff_result = await repo.diff("HEAD~1", "HEAD")
            if not diff_result.success or not diff_result.stdout.strip():
                # Fallback: get staged diff
                diff_result = await repo.get_diff(staged=True)
        except Exception:
            # Last resort: get any diff
            diff_result = await repo.get_diff(staged=True)

        diff_content = diff_result.stdout if diff_result.success else f"Files changed: {', '.join(changed_files)}"

        # Map risk level to tier (0=low, 1=medium, 2=high)
        risk_tier = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}.get(task.constraints.risk, 0)

        # Compute confidence based on check pass rate
        confidence = 0.85 if all_passed else 0.5

        try:
            result = await self._vcs.submit_change(
                task_id=task.id,
                goal=task.goal,
                branch_name=task.branch_name(),
                diff=diff_content,
                affected_paths=changed_files,
                evidence_markdown=evidence.to_markdown(),
                confidence=confidence,
                model_id=self.config.llm.model,
                risk_tier=risk_tier,
                draft=draft,
            )

            if result.success:
                self.metrics.prs_opened += 1
                print(f"[{self.worker_id}] VCS submission: {result.submission_type} {result.submission_id} / {result.status}")
                return {"number": result.submission_id, "type": result.submission_type, "url": result.url}
            else:
                print(f"[{self.worker_id}] VCS submission failed: {result.message}")
                raise RuntimeError(result.message)

        except Exception as e:
            print(f"[{self.worker_id}] Failed to submit to VCS: {e}")
            raise

    # Keep legacy method for backwards compatibility
    async def _create_pr(
        self,
        repo: GitRepo,
        task: TaskContract,
        evidence: EvidencePack,
        draft: bool = False,
    ) -> dict | None:
        """Legacy method - redirects to _submit_to_vcs."""
        # This is now a wrapper; actual submission goes through VCS adapter
        return await self._submit_to_vcs(repo, task, evidence, [], True, draft)

    async def process_task(self, message: QueueMessage) -> bool:
        """
        Process a single task from queue.

        Returns:
            True if task completed successfully
        """
        # Parse task
        task = TaskContract.from_json(message.body)
        task.attempt_count = message.receive_count
        task.assigned_worker = self.worker_id

        print(f"[{self.worker_id}] Processing task {task.id}: {task.goal}")
        self._current_task = task

        # Determine if we are in Local Mode (no token)
        token = self.config.github.token or os.environ.get("GITHUB_TOKEN")
        is_local_mode = not token

        # Initialize Repo
        repo = GitRepo(
            repo_url=self._get_repo_url(),
            token=token,
        )

        try:
            # Clone and checkout branch
            # In Local Mode, this clones from the local filesystem
            result = await repo.clone(
                branch=self.config.github.default_branch,
                depth=1,
            )
            if not result.success:
                if is_local_mode:
                    # In local mode, if clone fails (e.g. shallow clone on non-bare), try just copy?
                    # But 'git clone . target' usually works.
                    print(f"[{self.worker_id}] Local clone failed: {result.stderr}")
                raise RuntimeError(f"Clone failed: {result.stderr}")

            # Create task branch
            branch_name = task.branch_name()
            await repo.checkout(branch_name, create=True)

            # Implement the task
            changed_files, notes = await self._implement_task(repo, task)

            if not changed_files:
                print(f"[{self.worker_id}] No changes made for task {task.id}")
                await self.queue.ack(message.receipt_handle)
                return True

            # Run acceptance checks
            check_results = await self._run_acceptance_checks(repo, task)
            all_passed = all(passed for _, passed, _ in check_results)

            # Determine risks
            risks = []
            if task.constraints.risk == RiskLevel.HIGH:
                risks.append("High-risk task - requires careful review")
            if any("TODO" in f for f in changed_files):
                risks.append("Contains TODO markers")

            # Create evidence pack
            evidence = EvidencePack(
                task_id=task.id,
                task_goal=task.goal,
                commands_run=check_results,
                files_changed=changed_files,
                verification_method=notes,
                known_risks=risks,
            )

            # Stage and commit changes
            await repo.add()
            commit_msg = f"[{task.id}] {task.goal}\n\n{notes[:500]}"
            await repo.commit(
                message=commit_msg,
                author="MX Swarm <mx-swarm@automated.local>",
            )

            # Push branch only if using GitHub VCS (not Aether-only)
            # Aether submissions use the diff directly, no GitHub branch needed
            vcs_provider = self.config.vcs.provider
            if vcs_provider != "aether":
                push_result = await repo.push(branch_name)
                if not push_result.success:
                    raise RuntimeError(f"Push failed: {push_result.stderr}")

                if is_local_mode:
                    print(f"[{self.worker_id}] Local Mode: Pushed branch {branch_name} to {self._get_repo_url()}")
            else:
                print(f"[{self.worker_id}] Aether mode: Skipping GitHub push, submitting diff directly")

            # Submit via VCS adapter (Aether or GitHub)
            draft = not all_passed  # Draft if checks failed
            submission = await self._submit_to_vcs(repo, task, evidence, changed_files, all_passed, draft=draft)

            if submission:
                print(f"[{self.worker_id}] Submitted {submission.get('type')} #{submission.get('number')} for task {task.id}")

            # Success
            await self.queue.ack(message.receipt_handle)
            self.metrics.tasks_succeeded += 1
            return True

        except Exception as e:
            print(f"[{self.worker_id}] Task {task.id} failed: {e}")
            task.last_error = str(e)

            # Nack with delay for retry
            if task.attempt_count < task.constraints.retry_max:
                delay = min(30 * (2 ** task.attempt_count), 300)  # Exponential backoff
                await self.queue.nack(message.receipt_handle, delay=delay)
            else:
                # Max retries exceeded - acknowledge to remove from queue
                # In production, would move to dead letter queue
                await self.queue.ack(message.receipt_handle)
                print(f"[{self.worker_id}] Task {task.id} exceeded max retries")

            self.metrics.tasks_failed += 1
            return False

        finally:
            repo.cleanup()
            self._current_task = None
            self.metrics.tasks_processed += 1

    async def run_once(self) -> bool:
        """
        Process one task from queue.

        Returns:
            True if a task was processed
        """
        messages = await self.queue.receive(
            max_messages=1,
            visibility_timeout=self.config.worker.visibility_timeout_seconds,
        )

        if not messages:
            return False

        return await self.process_task(messages[0])

    async def run_loop(self) -> None:
        """
        Run the Ralph Wiggum Loop - infinite worker loop.

        Polls for tasks and processes them until stopped.
        """
        self._running = True
        print(f"[{self.worker_id}] Starting worker loop")

        while self._running:
            # Check if paused
            try:
                config = MXConfig.load()
                if config.paused:
                    print(f"[{self.worker_id}] Paused, waiting...")
                    await asyncio.sleep(30)
                    continue
            except Exception:
                pass

            # Process task
            processed = await self.run_once()

            if not processed:
                # No task available, wait before polling again
                await asyncio.sleep(self.config.worker.poll_interval_seconds)

        print(f"[{self.worker_id}] Worker loop stopped")

    async def run(self) -> WorkerMetrics:
        """
        Main entry point - initialize, run loop, cleanup.

        Returns:
            Worker metrics
        """
        await self.initialize()

        # Handle graceful shutdown
        loop = asyncio.get_event_loop()

        def signal_handler():
            print(f"\n[{self.worker_id}] Shutdown signal received")
            self._running = False

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, signal_handler)
            except NotImplementedError:
                pass  # Windows doesn't support add_signal_handler

        try:
            await self.run_loop()
        finally:
            await self.shutdown()

        return self.metrics





async def main():
    """CLI entry point for worker."""
    import argparse

    parser = argparse.ArgumentParser(description="MX Swarm Worker")
    parser.add_argument("--config", default="mx.config.json", help="Config file path")
    parser.add_argument("--worker-id", help="Worker ID (auto-generated if not set)")
    parser.add_argument("--once", action="store_true", help="Process one task and exit")
    args = parser.parse_args()

    config = MXConfig.load(args.config)
    worker = Worker(config, worker_id=args.worker_id)

    if args.once:
        await worker.initialize()
        try:
            result = await worker.run_once()
            print(f"Processed: {result}")
        finally:
            await worker.shutdown()
    else:
        metrics = await worker.run()
        print(f"\nWorker metrics: {metrics}")


if __name__ == "__main__":
    asyncio.run(main())
