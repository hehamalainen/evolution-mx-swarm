"""Worker Service - The Ralph Wiggum Loop.

Workers are the execution engines of MX Swarm:
1. Pull TaskContract from queue
2. Clone repo and checkout branch
3. Implement changes using LLM
4. Run acceptance checks
5. Open PR with evidence pack
6. Ack/Nack based on result
"""

import ast
import asyncio
import contextlib
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import sys
import tarfile
import tempfile
import time
import traceback
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any

from shared.adapters import (
    QueueAdapter,
    QueueMessage,
    SecretsAdapter,
    VCSAdapter,
    create_queue_adapter,
    create_secrets_adapter,
    create_vcs_adapter,
)
from shared.schemas import (
    ModelTier,
    NackReason,
    RiskLevel,
    TaskContract,
    TaskState,
    build_lane_key,
)
from shared.utils import (
    GitHubAPI,
    GitRepo,
    LLMClient,
    LLMResponse,
    Message,
    MockGitHubAPI,
    MXConfig,
    TerminalSettlementEvent,
    create_llm_client,
    get_capability_registry,
    snapshot_task,
)
from shared.utils.ast_merger import merge_partial_python_update
from shared.utils.metrics import (
    append_budget_event,
    append_decision_event,
    append_llm_usage_event,
    append_submission_event,
    append_trace_span,
    get_budget_snapshot,
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


MAX_NACKS_BEFORE_DLQ = 5
MAX_CHECK_FAIL_NACKS_BEFORE_DLQ = 3
_PRIMARY_FILE_BLOCK_RE = re.compile(
    r"```(?:file|python|typescript|javascript|js|ts|py):(?P<path>[^\r\n]+)\r?\n(?P<body>.*?)(?:(?:\r?\n)?```|$)",
    re.DOTALL,
)
_ALT_FILE_BLOCK_RE = re.compile(
    r"```(?:python|typescript|javascript)?\r?\n#\s*file:\s*(?P<path>[^\r\n]+)\r?\n(?P<body>.*?)(?:(?:\r?\n)?```|$)",
    re.DOTALL,
)


class WorkerFastFailError(RuntimeError):
    """Fast-fail error that carries an explicit queue settlement reason."""

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason


class WorkerPendingLedgerError(RuntimeError):
    """Signals that canonical ledger acceptance is pending and requires reconciliation."""

    def __init__(self, submission_id: str, detail: str):
        super().__init__(detail)
        self.submission_id = submission_id


@dataclass
class WorkerMetrics:
    """Worker execution metrics."""
    tasks_processed: int = 0
    tasks_succeeded: int = 0
    tasks_failed: int = 0
    prs_opened: int = 0
    llm_calls: int = 0
    total_tokens: int = 0
    start_time: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass
class EvidencePack:
    """Evidence pack for PR description."""
    task_id: str
    task_goal: str
    commands_run: list[tuple[str, bool, str]]  # (command, passed, output_summary)
    files_changed: list[str]
    verification_method: str
    known_risks: list[str]
    execution_trace: dict[str, str | int | bool] = field(default_factory=dict)

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

        if self.execution_trace:
            md += """
### Execution Trace
| Key | Value |
|-----|-------|
"""
            for key, value in self.execution_trace.items():
                md += f"| `{key}` | `{value}` |\n"

        return md


class Worker:
    """
    Worker service for MX Swarm.

    Implements the "Ralph Wiggum Loop":
    - Infinite loop with timeboxes
    - Automatic retries
    - Evidence-based PR creation
    """
    _global_inflight_task_ids: set[str] = set()

    def __init__(
        self,
        config: MXConfig,
        worker_id: str | None = None,
        queue: QueueAdapter | None = None,
        llm: LLMClient | None = None,
        vcs: VCSAdapter | None = None,
        secrets: SecretsAdapter | None = None,
    ):
        self.config = config
        self.worker_id = worker_id or f"worker-{str(uuid.uuid4())[:8]}"
        self.secrets = secrets or create_secrets_adapter(config.secrets.to_dict())
        self.queue = queue or create_queue_adapter(config.queue.to_dict())
        self.llm = llm or create_llm_client(config.llm.to_dict())
        self._frontier_llm: LLMClient | None = None
        self.metrics = WorkerMetrics()
        self._running = False
        self._github: GitHubAPI | MockGitHubAPI | None = None
        self._current_task: TaskContract | None = None
        self._settled_receipts: set[str] = set()
        self._settlement_lock = asyncio.Lock()
        self._reconciliation_stop = asyncio.Event()
        self._reconciliation_task: asyncio.Task[None] | None = None
        self._reconciliation_path = os.path.join(".runtime", "aether_reconciliation_backlog.json")
        self._event_log_path = os.path.join(".runtime", "task_state_events.jsonl")
        self._submission_records: dict[tuple[str, str], dict] = {}
        self._attempt_started_at: dict[tuple[str, str], datetime] = {}
        self._attempt_llm_cost_usd: dict[tuple[str, str], float] = {}
        self._executed_acceptance_checks: dict[str, list[dict[str, Any]]] = {}
        self._owned_workspace_roots: set[Path] = set()
        self._capability_registry = get_capability_registry(repo_path=os.getcwd())

        # Initialize VCS adapter (Aether or GitHub)
        self._vcs = vcs or create_vcs_adapter(self._build_vcs_config())

    def get_executed_acceptance_checks(self) -> dict[str, list[dict[str, Any]]]:
        """Return a copy of recorded acceptance check execution outcomes per task."""
        return {
            task_id: [dict(entry) for entry in entries]
            for task_id, entries in self._executed_acceptance_checks.items()
        }

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
                "token": self._effective_github_token(),
                "owner": self.config.github.repo_owner,
                "repo": self.config.github.repo_name,
                "default_branch": self.config.github.default_branch,
            }

    def _force_local_mode(self) -> bool:
        value = os.environ.get("MX_FORCE_LOCAL_MODE", "").strip().lower()
        return value in {"1", "true", "yes", "on"}

    def _effective_github_token(self) -> str | None:
        if self._force_local_mode():
            return None
        return self.config.github.token or os.environ.get("GITHUB_TOKEN")

    @property
    def github(self) -> GitHubAPI | MockGitHubAPI:
        """Lazy-initialize GitHub API."""
        if self._github is None:
            token = self._effective_github_token()
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
        """Initialize worker connections and resolve secrets."""
        await self.secrets.connect()

        # Resolve LLM API key if referenced as a secret
        if self.config.llm.api_key and self.config.llm.api_key.startswith("secret:"):
            secret_name = self.config.llm.api_key.split(":", 1)[1]
            try:
                self.config.llm.api_key = await self.secrets.get_secret(secret_name)
                # Re-initialize LLM client with resolved key
                self.llm = create_llm_client(self.config.llm.to_dict())
            except KeyError:
                print(f"Warning: Secret {secret_name} not found for LLM API key")

        if (
            self.config.frontier_llm
            and self.config.frontier_llm.api_key
            and self.config.frontier_llm.api_key.startswith("secret:")
        ):
            secret_name = self.config.frontier_llm.api_key.split(":", 1)[1]
            try:
                self.config.frontier_llm.api_key = await self.secrets.get_secret(secret_name)
                self._frontier_llm = create_llm_client(self.config.frontier_llm.to_dict())
            except KeyError:
                print(f"Warning: Secret {secret_name} not found for frontier LLM API key")

        # Resolve GitHub token if referenced as a secret
        if (
            self.config.vcs.provider == "github"
            and self.config.github.token
            and self.config.github.token.startswith("secret:")
        ):
            secret_name = self.config.github.token.split(":", 1)[1]
            try:
                self.config.github.token = await self.secrets.get_secret(secret_name)
                # Re-initialize VCS adapter with resolved token
                self._vcs = create_vcs_adapter(self._build_vcs_config())
            except KeyError:
                print(f"Warning: Secret {secret_name} not found for GitHub token")

        await self.queue.connect()

        if self.config.vcs.provider == "aether" and self.config.vcs.reconciliation_enabled:
            self._reconciliation_task = asyncio.create_task(self._reconciliation_loop())

    async def shutdown(self) -> None:
        """Cleanup worker connections."""
        self._running = False
        self._reconciliation_stop.set()
        if self._reconciliation_task is not None:
            self._reconciliation_task.cancel()
            await asyncio.gather(self._reconciliation_task, return_exceptions=True)
            self._reconciliation_task = None
        await self.queue.disconnect()

    def _get_repo_url(self) -> str:
        """Get repository URL."""
        local_override = os.environ.get("MX_LOCAL_REPO_PATH", "").strip()
        if local_override:
            return local_override

        # In Aether mode, always use local filesystem as the source repo.
        if self.config.vcs.provider == "aether":
            return os.getcwd()

        # If running in Local Mode (no token), use local filesystem as 'remote'
        token = self._effective_github_token()
        if not token:
            return os.getcwd()

        owner = self.config.github.repo_owner
        repo = self.config.github.repo_name
        return f"https://github.com/{owner}/{repo}.git"

    async def _read_file(self, repo: GitRepo, path: str) -> str | None:
        """Read file from repo after enforcing canonical workspace containment."""
        full_path = self._resolve_contained_workspace_path(repo.local_path, path)
        if os.path.isfile(full_path):
            with open(full_path, encoding="utf-8") as f:
                return f.read()
        return None

    async def _write_file(self, repo: GitRepo, path: str, content: str) -> None:
        """Write file to repo after enforcing canonical workspace containment."""
        full_path = self._resolve_contained_workspace_path(repo.local_path, path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        if os.path.islink(full_path):
            raise PermissionError(f"Symlink target rejected: {path!r}")
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)

    async def _list_files(self, repo: GitRepo, pattern: str = "**/*") -> list[str]:
        """List files in repo matching pattern, excluding symlinks and escaping paths."""
        import glob

        root_real = os.path.realpath(os.path.abspath(repo.local_path))
        files = glob.glob(
            os.path.join(repo.local_path, pattern),
            recursive=True,
        )
        results: list[str] = []
        for f in files:
            if os.path.islink(f) or not os.path.isfile(f):
                continue
            real_f = os.path.realpath(os.path.abspath(f))
            if os.path.commonpath([root_real, real_f]) != root_real:
                continue
            results.append(os.path.relpath(real_f, root_real))
        return results

    def _create_workspace_root(self) -> str:
        """Create an isolated worker workspace in /tmp."""
        workspace_root = os.path.join(tempfile.gettempdir(), f"mx_worker_{uuid.uuid4().hex}")
        os.makedirs(workspace_root, exist_ok=True)
        self._owned_workspace_roots.add(Path(workspace_root).resolve())
        return workspace_root

    def _strip_path_wrappers(self, path: str) -> str:
        """Strip outer whitespace and matching or stray quote wrappers."""
        cleaned = str(path or "").strip()
        while len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"`", "'", '"'}:
            cleaned = cleaned[1:-1].strip()
        return cleaned.strip("`").strip("'").strip('"').strip()

    def _normalize_relative_path(self, path: str) -> str:
        """Normalize generated file paths to stable repo-relative form."""
        cleaned = self._strip_path_wrappers(path)
        if not cleaned:
            return ""
        normalized = os.path.normpath(cleaned.replace("\\", "/")).replace("\\", "/")
        return "" if normalized == "." else normalized

    def _is_unsafe_relative_path(self, raw_path: str, normalized_path: str) -> bool:
        """Return True if raw_path or normalized_path is absolute, escaping, or malformed."""
        cleaned = self._strip_path_wrappers(raw_path)
        if not cleaned or not normalized_path:
            return True
        return bool(
            raw_path.endswith(("/", "\\"))
            or cleaned.endswith(("/", "\\"))
            or os.path.isabs(raw_path)
            or os.path.isabs(cleaned)
            or os.path.isabs(normalized_path)
            or cleaned.startswith(("/", "\\"))
            or normalized_path.startswith(("/", "\\"))
            or os.path.splitdrive(cleaned)[0]
            or os.path.splitdrive(normalized_path)[0]
            or normalized_path == ".."
            or normalized_path.startswith("../")
            or "/../" in f"/{normalized_path}/"
            or normalized_path == ".git"
            or normalized_path.startswith(".git/")
        )

    def _resolve_contained_workspace_path(self, workspace_root: str, path: str) -> str:
        """Resolve a workspace-relative path and reject symlink or parent-directory escapes."""
        normalized_path = self._normalize_relative_path(path)
        if self._is_unsafe_relative_path(path, normalized_path):
            raise PermissionError(f"Unsafe or malformed workspace path rejected: {path!r}")

        root_abs = os.path.abspath(workspace_root)
        root_real = os.path.realpath(root_abs)
        parts = [part for part in normalized_path.split("/") if part and part != "."]
        if not parts:
            raise PermissionError(f"Empty workspace path rejected: {path!r}")

        current = root_abs
        for part in parts:
            if part in {"..", ".git"}:
                raise PermissionError(f"Disallowed path segment {part!r} in {path!r}")
            current = os.path.join(current, part)
            if os.path.islink(current):
                raise PermissionError(f"Symlink path traversal rejected: {path!r}")

        target_real = os.path.realpath(current)
        if os.path.commonpath([root_real, target_real]) != root_real or target_real == root_real:
            raise PermissionError(f"Resolved path escapes workspace containment: {path!r}")

        return target_real

    def _resolve_allowed_target_path(
        self,
        raw_path: str,
        allowed_paths_set: set[str],
    ) -> tuple[str | None, str | None]:
        """Resolve model output paths against strict allowed targets."""
        normalized_path = self._normalize_relative_path(raw_path)
        if self._is_unsafe_relative_path(raw_path, normalized_path):
            return None, "unsafe_or_malformed"

        if not allowed_paths_set:
            return normalized_path, None

        if normalized_path in allowed_paths_set:
            return normalized_path, None

        basename = os.path.basename(normalized_path)
        basename_matches = [
            candidate for candidate in allowed_paths_set
            if os.path.basename(candidate) == basename
        ]
        if len(basename_matches) == 1:
            return basename_matches[0], f"basename_remap:{normalized_path}->{basename_matches[0]}"

        return None, "path_not_allowed"

    def _overlay_local_workspace_files(
        self,
        source_root: str,
        workspace_root: str,
        rel_paths: list[str],
    ) -> list[str]:
        """Copy selected local workspace files into the ephemeral clone."""
        copied: list[str] = []
        seen: set[str] = set()

        for rel_path in rel_paths:
            normalized = self._normalize_relative_path(rel_path)
            if not normalized or normalized in seen:
                continue
            if self._is_unsafe_relative_path(rel_path, normalized):
                continue
            seen.add(normalized)

            try:
                source_path = self._resolve_contained_workspace_path(source_root, normalized)
                target_path = self._resolve_contained_workspace_path(workspace_root, normalized)
            except PermissionError:
                continue
            if not os.path.isfile(source_path):
                continue

            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            shutil.copy2(source_path, target_path)
            copied.append(normalized)

        return copied

    def _destroy_workspace_root(self, workspace_root: str) -> None:
        """Destroy workspace regardless of outcome."""
        shutil.rmtree(workspace_root, ignore_errors=True)

    def _validate_python_syntax(self, path: str, content: str) -> None:
        """Fail fast on invalid Python before filesystem writes."""
        try:
            ast.parse(content, filename=path)
        except SyntaxError as exc:
            line = f" line {exc.lineno}" if exc.lineno is not None else ""
            raise WorkerFastFailError(
                reason="ERR_SYNTAX",
                detail=f"Syntax validation failed for {path}{line}: {exc.msg}",
            ) from exc

    def _is_refusal_response(self, content: str) -> bool:
        """Detect explicit model refusal / insufficient-information responses."""
        normalized = " ".join(content.lower().split())
        refusal_markers = (
            "insufficient information",
            "cannot complete this task",
            "can't complete this task",
            "i cannot complete",
            "i can't complete",
            "unable to complete",
            "cannot make code changes",
            "can't make code changes",
            "as an ai language model",
        )
        return any(marker in normalized for marker in refusal_markers)

    def _emit_task_event(
        self,
        task: TaskContract,
        state_from: TaskState,
        state_to: TaskState,
        reason: str | None = None,
    ) -> None:
        payload = {
            "event_id": f"evt-{uuid.uuid4().hex[:16]}",
            "trace_id": task.trace_id,
            "task_id": task.id,
            "attempt_id": task.attempt_id,
            "state_from": state_from.value,
            "state_to": state_to.value,
            "reason": reason,
            "detail": task.last_error,
            "provider": self.config.queue.provider,
            "timestamp": datetime.now(UTC).isoformat(),
            "actor_id": self.worker_id,
        }
        os.makedirs(os.path.dirname(self._event_log_path), exist_ok=True)
        with open(self._event_log_path, "a") as handle:
            handle.write(json.dumps(payload) + "\n")

    def _attempt_key(self, task: TaskContract) -> tuple[str, str]:
        return (task.id, task.attempt_id)

    def _infer_toolchain(self, task: TaskContract) -> str:
        candidates = list(task.context.files_hint) + list(task.context.do_not_touch)
        if candidates:
            extension_map = {
                ".py": "python",
                ".ts": "typescript",
                ".tsx": "typescript",
                ".js": "javascript",
                ".jsx": "javascript",
                ".go": "go",
                ".rs": "rust",
                ".sh": "shell",
                ".bash": "shell",
                ".zsh": "shell",
                ".yml": "yaml",
                ".yaml": "yaml",
                ".toml": "toml",
                ".json": "json",
                ".md": "markdown",
            }
            for path in candidates:
                _, ext = os.path.splitext(str(path).lower())
                toolchain = extension_map.get(ext)
                if toolchain:
                    return toolchain
        checks = " ".join(task.acceptance.checks).lower()
        if "pytest" in checks or "ruff" in checks:
            return "python"
        if "npm" in checks or "vitest" in checks or "tsc" in checks:
            return "typescript"
        return "unknown"

    def _lane_key_for_task(self, task: TaskContract) -> str:
        llm_config = self._llm_config_for_task(task)
        provider_model = f"{llm_config.provider}/{llm_config.model}"
        return build_lane_key(
            rig_id=task.rig_id,
            stream_id=task.stream.value,
            shard_id=task.shard_key or "default",
            model_tier=task.model_tier.value,
            provider_model=provider_model,
        )

    def _record_terminal_capability_event(
        self,
        task: TaskContract,
        state: TaskState,
        *,
        reason: str | None = None,
    ) -> None:
        if state not in {TaskState.SETTLED_ACK, TaskState.SETTLED_NACK, TaskState.DEAD_LETTER}:
            return

        attempt_key = self._attempt_key(task)
        started_at = self._attempt_started_at.pop(attempt_key, None)
        latency_s = 0.0
        if started_at is not None:
            latency_s = max(0.0, (datetime.now(UTC) - started_at).total_seconds())
        cost_usd = float(self._attempt_llm_cost_usd.pop(attempt_key, 0.0))
        outcome = "success" if state == TaskState.SETTLED_ACK else "failed"

        event = TerminalSettlementEvent(
            task_id=task.id,
            attempt_id=task.attempt_id,
            trace_id=task.trace_id,
            lane_key=self._lane_key_for_task(task),
            rig_id=task.rig_id,
            stream_id=task.stream.value,
            shard_id=task.shard_key or "default",
            model_tier=task.model_tier.value,
            outcome=outcome,
            terminal_state=state.value,
            reason_code=reason,
            detail=task.last_error,
            task_class=task.type.value,
            toolchain=self._infer_toolchain(task),
            cost_usd=cost_usd,
            latency_s=latency_s,
            false_success=False,
        )
        self._capability_registry.record_settlement_event(event)

    def _materialize_local_landing(
        self,
        *,
        workspace_root: str,
        source_root: str,
        changed_files: list[str],
    ) -> list[str]:
        """Copy verified changed files back into the local source repo for dependent tasks."""
        copied: list[str] = []
        for relative_path in changed_files:
            rel = str(relative_path).strip()
            if not rel:
                continue
            source_path = self._resolve_contained_workspace_path(workspace_root, rel)
            target_path = self._resolve_contained_workspace_path(source_root, rel)
            if not os.path.exists(source_path):
                raise FileNotFoundError(f"Local landing source missing: {rel}")
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            shutil.copy2(source_path, target_path)
            copied.append(self._normalize_relative_path(rel))
        if copied:
            print(f"[{self.worker_id}] Local landing materialized paths: {copied}")
        return copied

    def _close_local_mock_submission(self, submission_id: str) -> None:
        """Mark a mock submission as landed once local materialization has completed."""
        if not submission_id or self.config.vcs.provider != "github" or not self._is_local_submission_mode():
            return
        path = os.path.join(os.getcwd(), "mock_prs.json")
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
            prs = payload.get("prs")
            if not isinstance(prs, list):
                return
            updated = False
            for pr in prs:
                if not isinstance(pr, dict):
                    continue
                if str(pr.get("number", "")) != str(submission_id):
                    continue
                pr["state"] = "closed"
                pr["merged"] = True
                pr["merged_at"] = datetime.now(UTC).isoformat()
                pr["merge_detail"] = "materialized_local_landing"
                updated = True
                break
            if updated:
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, indent=2)
        except Exception as exc:
            print(f"[{self.worker_id}] Failed to finalize mock submission {submission_id}: {self._describe_exception(exc)}")

    def _set_state(self, task: TaskContract, state: TaskState, reason: str | None = None) -> None:
        """Best-effort state transition that enforces legal edges where possible."""
        state_from = task.state
        if task.state == state:
            return
        if task.can_transition_to(state):
            task.transition_to(state)
            if state == TaskState.CLAIMED:
                self._attempt_started_at[self._attempt_key(task)] = datetime.now(UTC)
            self._emit_task_event(task, state_from, state, reason=reason)
            self._emit_submission_settlement(task, state, reason=reason)
            self._record_terminal_capability_event(task, state, reason=reason)
            snapshot_task(task, actor_id=self.worker_id)
            return
        if state == TaskState.CLAIMED and task.state in {
            TaskState.CLAIMED,
            TaskState.EXECUTING,
            TaskState.ARTIFACT_READY,
            TaskState.VERIFIED,
            TaskState.SUBMITTED_PENDING_LEDGER,
            TaskState.SETTLED_NACK,
        }:
            task.state = TaskState.CLAIMED
            self._attempt_started_at[self._attempt_key(task)] = datetime.now(UTC)
            self._emit_task_event(task, state_from, state, reason=reason)
            self._emit_submission_settlement(task, state, reason=reason)
            self._record_terminal_capability_event(task, state, reason=reason)
            snapshot_task(task, actor_id=self.worker_id)
            return
        raise ValueError(f"Illegal task transition for {task.id}: {task.state.value} -> {state.value}")

    def _submission_key(self, task: TaskContract) -> tuple[str, str]:
        return (task.id, task.attempt_id)

    def _is_local_submission_mode(self) -> bool:
        token = self._effective_github_token()
        return self.config.vcs.provider != "aether" and not token

    def _normalize_retry_reason(self, value: object) -> NackReason | None:
        if isinstance(value, NackReason):
            return value
        if isinstance(value, str):
            try:
                return NackReason(value)
            except ValueError:
                return None
        return None

    def _llm_config_for_task(self, task: TaskContract):
        if task.model_tier.value == "frontier" and self.config.frontier_llm:
            return self.config.frontier_llm
        return self.config.llm

    def _llm_client_for_task(self, task: TaskContract) -> LLMClient:
        if task.model_tier.value == "frontier" and self.config.frontier_llm:
            if self._frontier_llm is None:
                self._frontier_llm = create_llm_client(self.config.frontier_llm.to_dict())
            return self._frontier_llm
        return self.llm

    def _cost_bucket_for_task(self, task: TaskContract) -> str:
        if task.model_tier == ModelTier.FRONTIER:
            return "high"
        if self._llm_config_for_task(task).provider == "ollama":
            return "low"
        return "medium"

    def _lane_for_task(self, task: TaskContract) -> str:
        if task.model_tier == ModelTier.FRONTIER:
            return "frontier"
        actor_id = getattr(self, "worker_id", "worker")
        if actor_id in {"planner", "judge", "governor"}:
            return "control_plane"
        return "bulk"

    def _llm_chat_timeout_seconds(self, task: TaskContract) -> int:
        if self._llm_config_for_task(task).provider == "ollama":
            if self._local_model_proof_mode_enabled():
                if task.prerequisite_task_ids or task.prerequisite_requirement_ids:
                    return 600
                return 420
            if task.prerequisite_task_ids or task.prerequisite_requirement_ids:
                return 240
            return 180
        return 120

    def _local_model_proof_mode_enabled(self) -> bool:
        raw = os.environ.get("MX_LOCAL_MODEL_PROOF_MODE", "").strip().lower()
        return raw in {"1", "true", "yes", "on"}

    def _canned_completion_fixture_path(self) -> str | None:
        raw = os.environ.get("MX_CANNED_COMPLETION_FIXTURE", "").strip()
        return raw or None

    def _build_canned_completion(self, task: TaskContract) -> tuple[LLMResponse, dict[str, object]] | None:
        fixture_path = self._canned_completion_fixture_path()
        if fixture_path is None:
            return None

        try:
            with open(fixture_path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception as exc:
            raise WorkerFastFailError(
                reason=NackReason.ERR_TASK_FAILED.value,
                detail=f"Unable to load canned completion fixture {fixture_path}: {self._describe_exception(exc)}",
            ) from exc

        task_specific = payload.get(task.id) or payload.get(task.goal)
        if isinstance(task_specific, dict):
            files_payload = task_specific.get("files")
            note = task_specific.get("note")
        else:
            files_payload = payload.get("files")
            note = payload.get("note")

        if not isinstance(files_payload, dict):
            raise WorkerFastFailError(
                reason=NackReason.ERR_TASK_FAILED.value,
                detail=f"Canned completion fixture {fixture_path} is missing a 'files' object.",
            )

        allowed_paths = [path.strip() for path in task.context.files_hint if path.strip()]
        selected_items: list[tuple[str, str]] = []
        for relative_path in allowed_paths:
            content = files_payload.get(relative_path)
            if isinstance(content, str):
                selected_items.append((relative_path, content))

        if not selected_items and len(files_payload) == 1:
            only_path, only_content = next(iter(files_payload.items()))
            if isinstance(only_path, str) and isinstance(only_content, str):
                selected_items.append((only_path, only_content))

        if not selected_items:
            return None

        blocks = []
        emitted_paths: list[str] = []
        for relative_path, content in selected_items:
            emitted_paths.append(relative_path)
            fence = "python" if relative_path.endswith(".py") else "file"
            blocks.append(f"```{fence}:{relative_path}\n{content.rstrip()}\n```")

        note = (note if isinstance(note, str) else None) or payload.get("note")
        metadata: dict[str, object] = {
            "fixture_path": fixture_path,
            "emitted_paths": emitted_paths,
        }
        if isinstance(note, str) and note.strip():
            metadata["note"] = note.strip()

        return (
            LLMResponse(
                content="\n\n".join(blocks),
                model="canned-completion",
                usage={},
                stop_reason="synthetic",
            ),
            metadata,
        )

    @staticmethod
    def _is_forbidden_snapshot_segment(segment: str) -> bool:
        """Return True if a path segment refers to .git metadata or any filesystem alias."""
        cleaned = segment.strip().rstrip(". ").lower()
        if not cleaned or cleaned in {".", ".."}:
            return True
        if cleaned in {".git", ".gitmodules", ".gitattributes", ".gitignore"} or cleaned.startswith(".git"):
            return True
        if re.match(r"^\.?git~\d*$", cleaned):
            return True
        return False

    @classmethod
    def _validate_snapshot_member_path(cls, raw_name: str, *, allow_root_dir: bool = False) -> str:
        """Validate a tar member path and return a normalized POSIX relative path."""
        if not raw_name or "\x00" in raw_name or "\\" in raw_name or ":" in raw_name:
            raise WorkerFastFailError(
                reason=NackReason.ERR_TASK_FAILED.value,
                detail=f"Malformed or unsafe path detected in tar: {raw_name!r}",
            )
        if raw_name.startswith("/") or os.path.isabs(raw_name):
            raise WorkerFastFailError(
                reason=NackReason.ERR_TASK_FAILED.value,
                detail=f"Absolute path detected in tar: {raw_name}",
            )
        stripped = raw_name.strip("/")
        if allow_root_dir and stripped in {"", "."}:
            return "."
        parts = [part for part in raw_name.split("/") if part not in {"", "."}]
        if not parts:
            raise WorkerFastFailError(
                reason=NackReason.ERR_TASK_FAILED.value,
                detail=f"Empty path detected in tar: {raw_name!r}",
            )
        for seg in raw_name.split("/"):
            if seg == "..":
                raise WorkerFastFailError(
                    reason=NackReason.ERR_TASK_FAILED.value,
                    detail=f"Path traversal detected in tar: {raw_name}",
                )
            if seg and seg != "." and cls._is_forbidden_snapshot_segment(seg):
                raise WorkerFastFailError(
                    reason=NackReason.ERR_TASK_FAILED.value,
                    detail=f"Disallowed VCS metadata segment {seg!r} in snapshot tar: {raw_name}",
                )
        normalized = os.path.normpath("/".join(parts)).replace("\\", "/")
        if normalized.startswith("..") or "/../" in f"/{normalized}/":
            raise WorkerFastFailError(
                reason=NackReason.ERR_TASK_FAILED.value,
                detail=f"Path traversal detected in tar: {raw_name}",
            )
        return normalized

    async def extract_snapshot_securely(
        self,
        tar_bytes: bytes,
        extract_dir: str | Path,
        repo_id: str | None = None,
        snapshot_id: str | None = None,
        tree_hash: str | None = None,
        *,
        require_manifest: bool = True,
        clean_destination: bool = False,
    ) -> list[str]:
        """Extract snapshot tar bytes via an empty staging directory, rejecting .git metadata and links."""
        import hashlib

        max_file_size = 10 * 1024 * 1024
        max_total_size = 100 * 1024 * 1024
        max_file_count = 1000
        raw_extract_path = Path(extract_dir)
        raw_fspath = os.fspath(raw_extract_path).rstrip("/\\") or os.fspath(raw_extract_path)
        if os.path.islink(raw_fspath) or raw_extract_path.is_symlink():
            raise WorkerFastFailError(
                reason=NackReason.ERR_TASK_FAILED.value,
                detail=f"Destination root must not be a symlink: {extract_dir}",
            )
        system_root_symlinks = {Path("/tmp"), Path("/var"), Path("/etc")}  # nosec B108
        for anc in raw_extract_path.expanduser().absolute().parents:
            if anc in system_root_symlinks:
                continue
            if anc.exists() and anc.is_symlink():
                raise WorkerFastFailError(
                    reason=NackReason.ERR_TASK_FAILED.value,
                    detail=f"Destination ancestor must not be a symlink: {anc}",
                )
        extract_path = raw_extract_path.resolve()

        try:
            with tarfile.open(fileobj=BytesIO(tar_bytes), mode="r:gz") as tar:
                members = tar.getmembers()
                non_dir_members = [m for m in members if not m.isdir()]
                if len(non_dir_members) > max_file_count:
                    raise WorkerFastFailError(
                        reason=NackReason.ERR_TASK_FAILED.value,
                        detail=f"Tar contains {len(non_dir_members)} files, exceeding limit of {max_file_count}",
                    )

                total_size = 0
                manifest_member = None
                validated_members: list[tuple[tarfile.TarInfo, str]] = []
                for member in members:
                    name = member.name
                    if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                        raise WorkerFastFailError(
                            reason=NackReason.ERR_TASK_FAILED.value,
                            detail=f"Symlinks, hardlinks, and special entries are prohibited: {name}",
                        )
                    normalized = self._validate_snapshot_member_path(
                        name,
                        allow_root_dir=member.isdir(),
                    )
                    if normalized == ".":
                        continue
                    resolved = (extract_path / normalized).resolve()
                    try:
                        resolved.relative_to(extract_path)
                    except ValueError as exc:
                        raise WorkerFastFailError(
                            reason=NackReason.ERR_TASK_FAILED.value,
                            detail=f"Path traversal resolved outside destination: {name}",
                        ) from exc
                    if member.isfile():
                        if member.size < 0 or member.size > max_file_size:
                            raise WorkerFastFailError(
                                reason=NackReason.ERR_TASK_FAILED.value,
                                detail=f"File {name} size {member.size} exceeds limit of {max_file_size}",
                            )
                        total_size += member.size
                        if total_size > max_total_size:
                            raise WorkerFastFailError(
                                reason=NackReason.ERR_TASK_FAILED.value,
                                detail=f"Total size {total_size} exceeds limit of {max_total_size}",
                            )
                    if normalized == ".aether_manifest.json":
                        manifest_member = member
                    else:
                        validated_members.append((member, normalized))

                manifest_files: dict[str, str] | None = None
                if require_manifest or manifest_member is not None:
                    if manifest_member is None:
                        raise WorkerFastFailError(
                            reason=NackReason.ERR_TASK_FAILED.value,
                            detail="Tar is missing .aether_manifest.json",
                        )
                    extracted_manifest = tar.extractfile(manifest_member)
                    if extracted_manifest is None:
                        raise WorkerFastFailError(
                            reason=NackReason.ERR_TASK_FAILED.value,
                            detail="Could not extract manifest file",
                        )
                    manifest_data = json.loads(extracted_manifest.read().decode("utf-8"))
                    if repo_id is not None and manifest_data.get("repo_id") != repo_id:
                        raise WorkerFastFailError(
                            reason=NackReason.ERR_TASK_FAILED.value,
                            detail=f"Repo ID mismatch: expected {repo_id}, got {manifest_data.get('repo_id')}",
                        )
                    if snapshot_id is not None and manifest_data.get("snapshot_id") != snapshot_id:
                        raise WorkerFastFailError(
                            reason=NackReason.ERR_TASK_FAILED.value,
                            detail=f"Snapshot ID mismatch: expected {snapshot_id}, got {manifest_data.get('snapshot_id')}",
                        )
                    if tree_hash is not None and manifest_data.get("tree_hash") != tree_hash:
                        raise WorkerFastFailError(
                            reason=NackReason.ERR_TASK_FAILED.value,
                            detail=f"Tree hash mismatch: expected {tree_hash}, got {manifest_data.get('tree_hash')}",
                        )
                    raw_files = manifest_data.get("files")
                    if not isinstance(raw_files, dict):
                        raise WorkerFastFailError(
                            reason=NackReason.ERR_TASK_FAILED.value,
                            detail="Manifest is missing valid 'files' mapping",
                        )
                    manifest_files = {str(k): str(v) for k, v in raw_files.items()}

                copied_rel_paths: list[str] = []
                with tempfile.TemporaryDirectory(prefix="mx_snapshot_stage_") as stage_dir:
                    stage_root = Path(stage_dir).resolve()
                    staged_files: list[tuple[str, Path]] = []

                    for member, normalized in validated_members:
                        if member.isdir():
                            continue
                        if manifest_files is not None and normalized not in manifest_files:
                            raise WorkerFastFailError(
                                reason=NackReason.ERR_TASK_FAILED.value,
                                detail=f"File {normalized} is not listed in manifest.",
                            )
                        extracted_file = tar.extractfile(member)
                        if extracted_file is None:
                            raise WorkerFastFailError(
                                reason=NackReason.ERR_TASK_FAILED.value,
                                detail=f"Could not read file {normalized} from tar.",
                            )
                        content = extracted_file.read()
                        if manifest_files is not None:
                            expected_sha = manifest_files[normalized]
                            actual_sha = hashlib.sha256(content).hexdigest()
                            if actual_sha != expected_sha:
                                raise WorkerFastFailError(
                                    reason=NackReason.ERR_TASK_FAILED.value,
                                    detail=f"SHA-256 mismatch for file {normalized}: expected {expected_sha}, got {actual_sha}",
                                )
                        stage_target = (stage_root / normalized).resolve()
                        stage_target.relative_to(stage_root)
                        stage_target.parent.mkdir(parents=True, exist_ok=True)
                        stage_target.write_bytes(content)
                        staged_files.append((normalized, stage_target))

                    if os.path.islink(raw_fspath) or raw_extract_path.is_symlink() or extract_path.is_symlink():
                        raise WorkerFastFailError(
                            reason=NackReason.ERR_TASK_FAILED.value,
                            detail=f"Destination root must not be a symlink: {extract_dir}",
                        )
                    extract_path.mkdir(parents=True, exist_ok=True)

                    if clean_destination:
                        forbidden_roots = {
                            Path("/").resolve(),
                            Path.home().resolve(),
                            Path.cwd().resolve(),
                        }
                        if extract_path in forbidden_roots:
                            raise WorkerFastFailError(
                                reason=NackReason.ERR_TASK_FAILED.value,
                                detail=f"Refusing destructive cleanup on protected root: {extract_path}",
                            )
                        owned_roots: set[Path] = getattr(self, "_owned_workspace_roots", set())
                        git_dir = extract_path / ".git"
                        allowed_temp_roots = {
                            Path(tempfile.gettempdir()).resolve(),
                            Path("/tmp").resolve(),  # nosec B108
                        }
                        in_temp_root = any(
                            extract_path != tr and tr in extract_path.parents
                            for tr in allowed_temp_roots
                        )
                        is_valid_worktree = (
                            git_dir.is_dir() and not git_dir.is_symlink() and in_temp_root
                        )
                        if extract_path not in owned_roots and not is_valid_worktree:
                            raise WorkerFastFailError(
                                reason=NackReason.ERR_TASK_FAILED.value,
                                detail=f"Refusing destructive snapshot cleanup on non-worker-owned directory: {extract_path}",
                            )
                        for existing in list(extract_path.iterdir()):
                            if existing.name == ".git":
                                continue
                            if existing.is_symlink() or existing.is_file():
                                existing.unlink()
                            elif existing.is_dir():
                                shutil.rmtree(existing)

                    for normalized, stage_target in staged_files:
                        if stage_target.is_symlink() or not stage_target.is_file():
                            raise WorkerFastFailError(
                                reason=NackReason.ERR_TASK_FAILED.value,
                                detail=f"Refusing non-regular staged file: {normalized}",
                            )
                        cur = extract_path
                        parts = normalized.split("/")
                        for seg in parts[:-1]:
                            if self._is_forbidden_snapshot_segment(seg):
                                raise WorkerFastFailError(
                                    reason=NackReason.ERR_TASK_FAILED.value,
                                    detail=f"Disallowed directory segment {seg!r} on copy",
                                )
                            cur = cur / seg
                            if cur.is_symlink():
                                raise WorkerFastFailError(
                                    reason=NackReason.ERR_TASK_FAILED.value,
                                    detail=f"Refusing to traverse symlink directory: {cur}",
                                )
                            cur.mkdir(parents=True, exist_ok=True)
                        leaf = parts[-1]
                        if self._is_forbidden_snapshot_segment(leaf):
                            raise WorkerFastFailError(
                                reason=NackReason.ERR_TASK_FAILED.value,
                                detail=f"Disallowed file segment {leaf!r} on copy",
                            )
                        dest_file = cur / leaf
                        if dest_file.is_symlink():
                            raise WorkerFastFailError(
                                reason=NackReason.ERR_TASK_FAILED.value,
                                detail=f"Refusing to overwrite symlink file: {dest_file}",
                            )
                        dest_resolved = dest_file.resolve()
                        dest_resolved.relative_to(extract_path)
                        shutil.copyfile(stage_target, dest_resolved)
                        copied_rel_paths.append(normalized)

                return copied_rel_paths
        except WorkerFastFailError:
            raise
        except Exception as exc:
            raise WorkerFastFailError(
                reason=NackReason.ERR_TASK_FAILED.value,
                detail=f"Failed to extract snapshot securely: {exc}",
            ) from exc

    async def commit_synthetic_baseline(self, repo_dir: str | Path) -> None:
        """Configure git identity, bypass hooks, and create a synthetic baseline commit."""
        import subprocess  # nosec B404

        path = Path(repo_dir).resolve()

        def run_git(args: list[str]) -> None:
            try:
                subprocess.run(  # nosec B603 B607
                    ["git", *args],
                    cwd=path,
                    capture_output=True,
                    text=True,
                    check=True,
                )
            except subprocess.CalledProcessError as exc:
                stderr_str = exc.stderr.strip() if exc.stderr else ""
                raise WorkerFastFailError(
                    reason=NackReason.ERR_TASK_FAILED.value,
                    detail=f"Git command failed: git {' '.join(args)}: {stderr_str}",
                ) from exc

        run_git(["config", "core.hooksPath", "/dev/null"])
        run_git(["config", "user.name", "MX Swarm Worker"])
        run_git(["config", "user.email", "worker@mx-swarm.local"])
        run_git(["add", "-A"])
        run_git(["commit", "-m", "Synthetic baseline commit", "--allow-empty"])

    def _llm_max_tokens_for_task(self, task: TaskContract) -> int:
        llm_config = self._llm_config_for_task(task)
        max_tokens = int(llm_config.max_tokens)
        if llm_config.provider != "ollama":
            return max_tokens

        strict_paths = [
            path.strip()
            for path in task.context.files_hint
            if path.strip()
        ]
        if self._local_model_proof_mode_enabled():
            if len(strict_paths) == 1:
                return min(max_tokens, 320)
            if strict_paths:
                return min(max_tokens, 512)
            return min(max_tokens, 768)

        if len(strict_paths) == 1:
            return min(max_tokens, 512)
        if strict_paths:
            return min(max_tokens, 768)
        return min(max_tokens, 1024)

    def _use_compact_local_prompt(self, task: TaskContract) -> bool:
        return self._llm_config_for_task(task).provider == "ollama" and bool(task.context.files_hint)

    def _max_failure_attempts(self) -> int:
        configured_retries = max(0, self.config.worker.retry_max)
        return configured_retries + 1

    def _estimate_llm_cost_usd(
        self,
        task: TaskContract,
        usage: dict[str, int] | None,
    ) -> float:
        usage = usage or {}
        llm_config = self._llm_config_for_task(task)
        input_tokens = max(0, int(usage.get("input_tokens", 0) or 0))
        output_tokens = max(0, int(usage.get("output_tokens", 0) or 0))
        input_rate = max(0.0, float(getattr(llm_config, "input_cost_per_million_usd", 0.0) or 0.0))
        output_rate = max(0.0, float(getattr(llm_config, "output_cost_per_million_usd", 0.0) or 0.0))
        return round(((input_tokens / 1_000_000) * input_rate) + ((output_tokens / 1_000_000) * output_rate), 8)

    def _should_dead_letter_failure(self, message: QueueMessage, *, check_fail: bool = False) -> bool:
        configured_limit = self._max_failure_attempts()
        hard_limit = MAX_CHECK_FAIL_NACKS_BEFORE_DLQ if check_fail else MAX_NACKS_BEFORE_DLQ
        return message.receive_count >= min(hard_limit, configured_limit)

    def _build_task_prompt(
        self,
        *,
        task: TaskContract,
        context: str,
        agents_md: str,
        allowed_paths_text: str,
        strict_editing_guidance: str,
        retry_guidance: str,
    ) -> str:
        acceptance_text = chr(10).join(f"- {c}" for c in task.acceptance.criteria)
        tools_text = ", ".join(task.context.tools) if task.context.tools else "read, write, bash, grep"

        if self._use_compact_local_prompt(task):
            return f"""Task: {task.goal}
Type: {task.type.value}
Editable paths:
{allowed_paths_text}
Read-only paths: {task.context.do_not_touch}
Acceptance:
{acceptance_text}
Retry:
{retry_guidance}
Guidance:
{strict_editing_guidance or "- Keep the scaffold and output format intact."}
Tools: {tools_text}
Context:
{context[:2200]}
OUTPUT CONTRACT:
- Return only fenced code blocks. No prose before or after.
- The first non-whitespace characters must be ```python:relative/path or ```file:relative/path.
- The final non-whitespace characters must be closing ```.
- For existing Python scaffolds with NotImplementedError stubs, emit only replacement function/class definitions inside the fence.
- Do not redesign the module. Keep parser, imports, error handling, and main intact.
"""

        return f"""Implement this task:

**Goal:** {task.goal}

**Task Type:** {task.type.value}

**Acceptance Criteria:**
{acceptance_text}

**Files to consider modifying:** {task.context.files_hint}
**DO NOT modify these files:** {task.context.do_not_touch}
**Allowed target paths (exact):**
{allowed_paths_text}

**Strict Editing Guidance:**
{strict_editing_guidance or "- No additional strict editing guidance."}

**Retry Context:**
{retry_guidance}

**Allowed Tools for this task:**
{tools_text}

**Agent Guidelines:**
{agents_md[:2000]}

**Codebase Context:**
{context}

Implement the task.

OUTPUT CONTRACT (STRICT):
1) Return ONLY fenced code blocks. Do not output prose, notes, or explanations.
2) Use only one of these fence headers: ```file:relative/path or ```python:relative/path.
3) If allowed target paths are listed above, every emitted path MUST exactly match one of them.
4) Emit complete file content for each block.
5) If an editable target already exists, patch that file minimally instead of replacing the whole design.
6) Read-only reference files are authoritative for expected behavior. Match them exactly, but do not modify them.

Example:
```python:path/to/file.py
<complete new file content>
```
"""

    def _build_retry_payload(
        self,
        task: TaskContract,
        reason: NackReason,
        detail: str | None,
    ) -> str:
        retry_task = TaskContract.from_json(task.to_json())
        retry_task.state = TaskState.QUEUED
        retry_task.assigned_worker = None
        retry_task.settlement_reason = None
        retry_task.previous_failure_reason = reason
        retry_task.previous_failure_detail = detail
        retry_task.previous_attempt_id = task.attempt_id
        retry_task.previous_attempt_number = task.attempt_count
        retry_task.last_error = detail
        return retry_task.to_json()

    def _attach_retry_context(
        self,
        task: TaskContract,
        message: QueueMessage,
        reason: NackReason,
        detail: str | None,
    ) -> None:
        normalized_detail = " ".join((detail or "").split())[:1000] or None
        message.body = self._build_retry_payload(task, reason, normalized_detail)
        message.attributes["last_nack_reason"] = reason.value
        if normalized_detail:
            message.attributes["last_nack_detail"] = normalized_detail
        message.attributes["previous_attempt_id"] = task.attempt_id
        message.attributes["previous_attempt_number"] = task.attempt_count

    def _build_retry_guidance(self, task: TaskContract) -> str:
        lines = [f"- Current attempt number: {max(task.attempt_count, 1)}."]
        previous_reason = task.previous_failure_reason
        if previous_reason is None:
            lines.append("- No previous failure context is available for this task.")
            return "\n".join(lines)

        lines.append(
            f"- Previous attempt `{task.previous_attempt_id or 'unknown'}` failed with `{previous_reason.value}`."
        )
        if task.previous_attempt_number is not None:
            lines.append(f"- Previous attempt number: {task.previous_attempt_number}.")
        if task.previous_failure_detail:
            lines.append(f"- Previous failure detail: {task.previous_failure_detail}")

        if previous_reason == NackReason.ERR_NO_DIFF:
            lines.append(
                "- Previous attempt produced no valid file blocks. You MUST emit at least one fenced code block using ```file:relative/path or ```python:relative/path."
            )
        elif previous_reason == NackReason.ERR_SYNTAX:
            lines.append("- Previous attempt failed Python syntax validation. Ensure emitted Python parses before returning it.")
        elif previous_reason == NackReason.ERR_CHECK_FAIL:
            lines.append("- Previous attempt failed acceptance checks. Fix the implementation to satisfy the listed checks without modifying read-only files or weakening tests.")
        elif previous_reason == NackReason.ERR_REFUSAL:
            lines.append("- Previous attempt refused or avoided the task. Do not explain limitations; return concrete file edits only.")

        return "\n".join(lines)

    def _merge_partial_python_update(
        self,
        existing_content: str,
        emitted_content: str,
    ) -> str | None:
        """Merge top-level emitted definitions into an existing Python scaffold."""
        result = merge_partial_python_update(existing_content, emitted_content)
        task_id = self._current_task.id if self._current_task is not None else "unknown"

        if result:
            append_decision_event(
                task_id=task_id,
                reasoning="Detected Python source file with mergeable definitions (Functions/Classes).",
                choice="AST-based Partial Merge",
                impact_level="high",
                context={"file_size": len(existing_content), "update_size": len(emitted_content)}
            )
        else:
            append_decision_event(
                task_id=task_id,
                reasoning="No matching Python definitions found or syntax error in emitted content.",
                choice="Omit Partial Merge (Fallback to Full Replace or Manual Resolution)",
                impact_level="medium"
            )

        return result

    def _extract_file_blocks(self, content: str) -> list[tuple[str, str]]:
        """Extract file blocks, tolerating missing closing fences at EOF."""
        normalized = content.replace("\r\n", "\n")

        for pattern in (_PRIMARY_FILE_BLOCK_RE, _ALT_FILE_BLOCK_RE):
            matches = [
                (match.group("path").strip(), match.group("body").strip())
                for match in pattern.finditer(normalized)
                if match.group("path").strip() and match.group("body").strip()
            ]
            if matches:
                return matches

        return []

    def _record_submission_event(
        self,
        task: TaskContract,
        *,
        submission_id: str,
        submission_type: str,
        status: str,
        url: str | None,
        affected_paths: list[str],
        expected_diff: str | None = None,
    ) -> None:
        created_at = datetime.now(UTC).isoformat()
        source_run_id = self._submission_source_run_id(task)
        intent_type = self._submission_intent_type(task)
        branch_name = task.branch_name()
        provider = self.config.vcs.provider
        local_mode = self._is_local_submission_mode()
        payload: dict[str, Any] = {
            "branch_name": branch_name,
            "submission_id": submission_id,
            "submission_type": submission_type,
            "provider": provider,
            "url": url,
            "status": status,
            "goal": task.goal,
            "affected_paths": affected_paths,
            "local_mode": local_mode,
            "created_at": created_at,
            "canonical_status": None,
            "landing_verification_status": None,
            "landing_verification_detail": None,
            "expected_diff": expected_diff,
            "source": "mx_swarm",
            "source_run_id": source_run_id,
            "source_task_id": task.id,
            "source_agent_id": self.worker_id,
            "source_attempt_id": task.attempt_id,
            "intent_type": intent_type,
        }
        self._submission_records[self._submission_key(task)] = payload
        append_submission_event(
            task_id=task.id,
            trace_id=task.trace_id,
            attempt_id=task.attempt_id,
            branch_name=branch_name,
            submission_id=submission_id,
            submission_type=submission_type,
            provider=provider,
            url=url,
            status=status,
            goal=task.goal,
            affected_paths=affected_paths,
            local_mode=local_mode,
            created_at=created_at,
            canonical_status=None,
            landing_verification_status=None,
            landing_verification_detail=None,
            source="mx_swarm",
            source_run_id=source_run_id,
            source_task_id=task.id,
            source_agent_id=self.worker_id,
            source_attempt_id=task.attempt_id,
            intent_type=intent_type,
        )

    def _update_submission_record(self, task: TaskContract, **fields: object) -> None:
        record = self._submission_records.get(self._submission_key(task))
        if record is None:
            return
        record.update(fields)

    def _emit_submission_settlement(
        self,
        task: TaskContract,
        state: TaskState,
        *,
        reason: str | None = None,
    ) -> None:
        if state not in {
            TaskState.SETTLED_ACK,
            TaskState.SETTLED_NACK,
            TaskState.DEAD_LETTER,
        }:
            return

        record = self._submission_records.get(self._submission_key(task))
        if not record:
            return

        append_submission_event(
            task_id=task.id,
            trace_id=task.trace_id,
            attempt_id=task.attempt_id,
            branch_name=str(record.get("branch_name") or task.branch_name()),
            submission_id=str(record.get("submission_id") or task.submission_ledger_id or ""),
            submission_type=str(record.get("submission_type") or "unknown"),
            provider=str(record.get("provider") or self.config.vcs.provider),
            url=record.get("url"),
            status=state.value,
            goal=task.goal,
            affected_paths=list(record.get("affected_paths") or []),
            local_mode=bool(record.get("local_mode")),
            created_at=str(record.get("created_at") or datetime.now(UTC).isoformat()),
            settled_at=datetime.now(UTC).isoformat(),
            reason=reason,
            canonical_status=str(record.get("canonical_status") or "") or None,
            landing_verification_status=str(record.get("landing_verification_status") or "") or None,
            landing_verification_detail=str(record.get("landing_verification_detail") or "") or None,
            source=str(record.get("source") or "") or None,
            source_run_id=str(record.get("source_run_id") or "") or None,
            source_task_id=str(record.get("source_task_id") or "") or None,
            source_agent_id=str(record.get("source_agent_id") or "") or None,
            source_attempt_id=str(record.get("source_attempt_id") or "") or None,
            intent_type=str(record.get("intent_type") or "") or None,
        )

    async def _verify_post_landing_submission(
        self,
        task: TaskContract,
        *,
        submission_id: str,
        affected_paths: list[str],
    ) -> None:
        record = self._submission_records.get(self._submission_key(task), {})
        expected_diff = record.get("expected_diff")
        result = await self._vcs.verify_landed_change(
            submission_id,
            expected_paths=affected_paths,
            expected_diff=expected_diff if isinstance(expected_diff, str) else None,
        )
        status = "verified" if result.verified else ("unsupported" if not result.supported else "failed")
        self._update_submission_record(
            task,
            canonical_status=result.canonical_status or record.get("canonical_status"),
            landing_verification_status=status,
            landing_verification_detail=result.detail,
        )
        append_trace_span(
            trace_id=task.trace_id,
            service=self.config.vcs.provider,
            operation="verify_landed_change",
            actor_id=self.worker_id,
            task_id=task.id,
            attempt_id=task.attempt_id,
            status="ok" if result.verified else "error",
            attributes={
                "submission_id": submission_id,
                "provider": result.provider or self.config.vcs.provider,
                "supported": result.supported,
                "verified": result.verified,
                "canonical_status": result.canonical_status,
                "verified_paths": result.verified_paths or [],
                "detail": result.detail,
            },
        )
        if not result.verified:
            raise WorkerFastFailError(
                reason=NackReason.ERR_POST_LANDING_VERIFY.value,
                detail=result.detail or f"Post-landing verification failed for {submission_id}",
            )

    def _normalize_reason(self, reason: str | NackReason) -> NackReason:
        if isinstance(reason, NackReason):
            return reason
        try:
            return NackReason(reason)
        except ValueError:
            return NackReason.ERR_TASK_FAILED

    def _describe_exception(self, exc: BaseException, *, context: str | None = None) -> str:
        """Produce a non-empty error detail for runtime failures."""
        exc_type = type(exc).__name__
        message = " ".join(str(exc).split()).strip()
        summary = f"{exc_type}: {message}" if message else exc_type
        last_frame = None
        if exc.__traceback__ is not None:
            frames = traceback.extract_tb(exc.__traceback__)
            if frames:
                frame = frames[-1]
                last_frame = f"{os.path.basename(frame.filename)}:{frame.lineno} in {frame.name}"
        if last_frame:
            summary = f"{summary} [{last_frame}]"
        return f"{context}: {summary}" if context else summary

    def _submission_source_run_id(self, task: TaskContract) -> str:
        """Return the best available producer-side run identifier."""
        configured_run_id = os.getenv("MX_RUN_ID", "").strip()
        return configured_run_id or task.trace_id

    def _submission_intent_type(self, task: TaskContract) -> str:
        """Map MX task semantics into the initial canonical intent vocabulary."""
        if task.routing_target.value == "replan":
            return "replan"
        if task.stream.value == "qa" or task.type.value == "test":
            return "qa"
        if task.stream.value == "ops" or task.type.value == "ops":
            return "ops"
        if task.type.value == "feature":
            return "feature"
        if task.type.value == "bugfix":
            return "bugfix"
        return "other"

    @classmethod
    def _try_acquire_task_single_flight(cls, task_id: str) -> bool:
        """Best-effort per-process single-flight for duplicate task execution."""
        if task_id in cls._global_inflight_task_ids:
            return False
        cls._global_inflight_task_ids.add(task_id)
        return True

    @classmethod
    def _release_task_single_flight(cls, task_id: str) -> None:
        cls._global_inflight_task_ids.discard(task_id)

    async def _register_settlement(self, receipt_handle: str) -> bool:
        """Idempotency guard to prevent duplicate ack/nack for the same receipt."""
        async with self._settlement_lock:
            if receipt_handle in self._settled_receipts:
                return False
            self._settled_receipts.add(receipt_handle)
            return True

    async def _lease_heartbeat_loop(
        self,
        message: QueueMessage,
        stop_event: asyncio.Event,
    ) -> None:
        """Extend queue visibility while long-running execution is active."""
        if not self.config.worker.lease_heartbeat_enabled:
            return
        interval = max(1, self.config.worker.lease_heartbeat_interval_seconds)
        extension = max(1, self.config.worker.lease_extension_seconds)
        max_lease = max(extension, self.config.worker.lease_max_seconds)
        start = datetime.now(UTC)

        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                break
            except TimeoutError:
                pass

            elapsed = (datetime.now(UTC) - start).total_seconds()
            if elapsed >= max_lease:
                print(
                    f"[{self.worker_id}] Lease heartbeat max reached "
                    f"for receipt {message.receipt_handle}; no further extensions"
                )
                break

            try:
                await self.queue.extend_visibility(message.receipt_handle, extension)
                message.lease_expires_at = datetime.now(UTC) + timedelta(seconds=extension)
            except Exception as exc:
                print(
                    f"[{self.worker_id}] Lease heartbeat failed for "
                    f"receipt {message.receipt_handle}: {self._describe_exception(exc)}"
                )

    async def _await_canonical_acceptance(self, submission_id: str) -> str:
        """Poll VCS status for canonical settlement signal."""
        retry_max = max(1, self.config.vcs.settlement_retry_max)
        backoff_seconds = max(1, self.config.vcs.settlement_retry_backoff_seconds)
        accepted = {"accepted", "committed", "merged", "settled_ack", "complete", "completed", "promoted"}
        rejected = {"rejected", "failed", "settled_nack", "denied"}

        for attempt in range(1, retry_max + 1):
            status = await self._vcs.get_status(submission_id)
            normalized = str(status.get("status") or status.get("state") or "").strip().lower()
            if normalized in accepted:
                return "accepted"
            if normalized in rejected:
                return "rejected"
            if attempt < retry_max:
                await asyncio.sleep(min(backoff_seconds * attempt, 60))

        return "timeout"

    def _budget_usage_pct(self) -> float:
        raw = os.environ.get("MX_BUDGET_USAGE_PCT", "0")
        try:
            return max(0.0, float(raw))
        except ValueError:
            return 0.0

    def _budget_snapshot(self) -> dict[str, Any]:
        snapshot = get_budget_snapshot(self.config.budget)
        env_override_pct = self._budget_usage_pct()
        if env_override_pct > snapshot.get("total_budget_pct", 0.0):
            snapshot["total_budget_pct"] = env_override_pct
            snapshot["total_frozen"] = env_override_pct >= float(self.config.budget.freeze_threshold_pct)
        return snapshot

    def _claims_frozen(self) -> bool:
        if self.config.budget.enforcement.lower() != "on":
            return False
        snapshot = self._budget_snapshot()
        if self.config.budget.freeze_mode == "throttle_only":
            return False
        return bool(snapshot.get("total_frozen", False))

    def _frontier_claim_block_reason(self, task: TaskContract) -> str | None:
        if self.config.budget.enforcement.lower() != "on":
            return None
        if task.model_tier != ModelTier.FRONTIER:
            if self._claims_frozen():
                return "global claim freeze active"
            return None

        snapshot = self._budget_snapshot()
        if not task.frontier_eligible:
            return "frontier lane requires explicit frontier_eligible=true"
        if task.frontier_attempt_count >= self.config.budget.max_frontier_attempts_per_task_contract:
            return "frontier attempt limit reached for task contract"
        if snapshot.get("active_frontier_claims", 0) >= self.config.budget.max_frontier_concurrency:
            return "frontier concurrency cap reached"
        if snapshot.get("frontier_claims_last_hour", 0) >= self.config.budget.max_frontier_escalations_per_hour:
            return "frontier escalation hourly cap reached"
        if float(snapshot.get("frontier_spend_today_usd", 0.0)) >= float(self.config.budget.frontier_daily_usd_cap):
            return "frontier daily budget exhausted"
        if (
            float(snapshot.get("cost_per_success_usd", 0.0)) > float(self.config.budget.cost_per_success_ceiling_usd)
            and int(snapshot.get("successful_settlements_today", 0)) > 0
        ):
            return "frontier cost per success ceiling exceeded"
        if self.config.budget.freeze_mode != "frontier_only" and self._claims_frozen():
            return "global claim freeze active"
        if self.config.budget.freeze_mode == "frontier_only" and bool(snapshot.get("frontier_frozen", False)):
            return "frontier-only freeze active"
        return None

    def _emit_budget_freeze(self, task: TaskContract, detail: str) -> None:
        lane = self._lane_for_task(task)
        append_budget_event(
            event_type="claim_blocked",
            trace_id=task.trace_id,
            task_id=task.id,
            attempt_id=task.attempt_id,
            actor_id=self.worker_id,
            lane=lane,
            detail=detail,
            attributes={
                "model_tier": task.model_tier.value,
                "routing_target": task.routing_target.value,
            },
        )
        append_trace_span(
            trace_id=task.trace_id,
            service="worker",
            operation="budget_claim_block",
            actor_id=self.worker_id,
            task_id=task.id,
            attempt_id=task.attempt_id,
            status="error",
            attributes={
                "detail": detail,
                "lane": lane,
                "model_tier": task.model_tier.value,
                "routing_target": task.routing_target.value,
            },
        )

    def _record_frontier_claim_start(self, task: TaskContract) -> None:
        append_budget_event(
            event_type="frontier_claim_started",
            trace_id=task.trace_id,
            task_id=task.id,
            attempt_id=task.attempt_id,
            actor_id=self.worker_id,
            lane="frontier",
            detail="Frontier claim admitted",
            attributes={
                "frontier_attempt_count": task.frontier_attempt_count,
                "estimated_cost_bucket": task.estimated_cost_bucket,
            },
        )

    def _record_frontier_claim_finish(self, task: TaskContract, *, status: str, reason: str | None = None) -> None:
        append_budget_event(
            event_type="frontier_claim_finished",
            trace_id=task.trace_id,
            task_id=task.id,
            attempt_id=task.attempt_id,
            actor_id=self.worker_id,
            lane="frontier",
            detail=status,
            attributes={
                "status": status,
                "reason": reason,
            },
        )

    @contextlib.contextmanager
    def _reconciliation_lock(self):
        lock_dir = os.path.dirname(self._reconciliation_path) or "."
        os.makedirs(lock_dir, exist_ok=True)
        lock_path = f"{self._reconciliation_path}.lock"
        with open(lock_path, "a+") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def _load_reconciliation_backlog_unlocked(self) -> list[dict]:
        if not os.path.exists(self._reconciliation_path):
            return []
        try:
            with open(self._reconciliation_path) as handle:
                payload = json.load(handle)
            if isinstance(payload, list):
                return [item for item in payload if isinstance(item, dict)]
        except Exception:
            return []
        return []

    def _load_reconciliation_backlog(self) -> list[dict]:
        with self._reconciliation_lock():
            return self._load_reconciliation_backlog_unlocked()

    def _save_reconciliation_backlog_unlocked(self, backlog: list[dict]) -> None:
        target_dir = os.path.dirname(self._reconciliation_path) or "."
        os.makedirs(target_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=".reconciliation_backlog_",
            suffix=".tmp",
            dir=target_dir,
        )
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(backlog, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self._reconciliation_path)
        finally:
            if os.path.exists(tmp_path):
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)

    def _save_reconciliation_backlog(self, backlog: list[dict]) -> None:
        with self._reconciliation_lock():
            self._save_reconciliation_backlog_unlocked(backlog)

    def _update_reconciliation_backlog(
        self,
        mutator: Callable[[list[dict]], bool],
    ) -> list[dict]:
        with self._reconciliation_lock():
            backlog = self._load_reconciliation_backlog_unlocked()
            if mutator(backlog):
                self._save_reconciliation_backlog_unlocked(backlog)
            return backlog

    @staticmethod
    def _reconciliation_item_key(item: dict) -> tuple[str, str, str]:
        return (
            str(item.get("task_id") or ""),
            str(item.get("attempt_id") or ""),
            str(item.get("submission_id") or ""),
        )

    async def _enqueue_reconciliation_item(
        self,
        task: TaskContract,
        submission_id: str,
        detail: str,
        affected_paths: list[str] | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        new_entry = {
            "task_id": task.id,
            "trace_id": task.trace_id,
            "attempt_id": task.attempt_id,
            "submission_id": submission_id,
            "state": TaskState.SUBMITTED_PENDING_LEDGER.value,
            "status": "pending",
            "attempts": 0,
            "version": 0,
            "reason": NackReason.ERR_LEDGER_UNAVAILABLE.value,
            "detail": detail,
            "affected_paths": list(affected_paths or []),
            "created_at": now,
            "updated_at": now,
            "actor_id": self.worker_id,
        }

        def _append(backlog: list[dict]) -> bool:
            backlog.append(new_entry)
            return True

        self._update_reconciliation_backlog(_append)

    async def _reconcile_backlog_once(self) -> None:
        backlog = self._load_reconciliation_backlog()
        if not backlog:
            return

        changed = False
        updated_items: dict[tuple[str, str, str], dict] = {}
        snapshot_versions: dict[tuple[str, str, str], tuple[int, int]] = {}
        max_attempts = max(1, self.config.vcs.settlement_retry_max)
        accepted = {"accepted", "committed", "merged", "settled_ack", "complete", "completed"}
        rejected = {"rejected", "failed", "settled_nack", "denied"}

        for raw_item in backlog:
            if raw_item.get("status") != "pending":
                continue
            item = dict(raw_item)
            item_key = self._reconciliation_item_key(item)
            base_attempts = int(item.get("attempts", 0))
            base_version = int(item.get("version", base_attempts))
            snapshot_versions[item_key] = (base_version, base_attempts)

            submission_id = str(item.get("submission_id") or "").strip()
            if not submission_id:
                item["status"] = "dead_letter"
                item["reason"] = NackReason.ERR_RECON_DIVERGENCE.value
                item["version"] = base_version + 1
                item["updated_at"] = datetime.now(UTC).isoformat()
                updated_items[item_key] = dict(item)
                changed = True
                continue

            status = await self._vcs.get_status(submission_id)
            normalized = str(status.get("status") or status.get("state") or "").strip().lower()
            attempts = base_attempts + 1
            item["attempts"] = attempts
            item["version"] = base_version + 1
            item["last_status"] = normalized or "unknown"
            item["updated_at"] = datetime.now(UTC).isoformat()
            changed = True

            if normalized in accepted:
                if self.config.vcs.post_landing_verification_required:
                    verification = await self._vcs.verify_landed_change(
                        submission_id,
                        expected_paths=list(item.get("affected_paths") or []),
                    )
                    if verification.verified:
                        item["status"] = "settled_ack"
                        item["state"] = TaskState.SETTLED_ACK.value
                        item["reason"] = None
                        item["canonical_status"] = verification.canonical_status
                        item["landing_verification_status"] = "verified"
                        item["landing_verification_detail"] = verification.detail
                    else:
                        item["status"] = "settled_nack"
                        item["state"] = TaskState.SETTLED_NACK.value
                        item["reason"] = NackReason.ERR_POST_LANDING_VERIFY.value
                        item["canonical_status"] = verification.canonical_status
                        item["landing_verification_status"] = (
                            "unsupported" if not verification.supported else "failed"
                        )
                        item["landing_verification_detail"] = verification.detail
                else:
                    item["status"] = "settled_ack"
                    item["state"] = TaskState.SETTLED_ACK.value
                    item["reason"] = None
                updated_items[item_key] = dict(item)
                continue

            if normalized in rejected:
                item["status"] = "settled_nack"
                item["state"] = TaskState.SETTLED_NACK.value
                item["reason"] = NackReason.ERR_RECON_DIVERGENCE.value
                updated_items[item_key] = dict(item)
                continue

            if attempts >= max_attempts:
                item["status"] = "dead_letter"
                item["state"] = TaskState.DEAD_LETTER.value
                item["reason"] = NackReason.ERR_LEDGER_UNAVAILABLE.value

            updated_items[item_key] = dict(item)

        if changed:
            def _merge_updates(current_backlog: list[dict]) -> bool:
                mutated = False
                for idx, existing in enumerate(current_backlog):
                    key = self._reconciliation_item_key(existing)
                    if key not in updated_items:
                        continue
                    # Never reverse or overwrite an item that has already reached a non-pending terminal state
                    if existing.get("status") != "pending":
                        continue
                    candidate = dict(updated_items[key])
                    existing_attempts = int(existing.get("attempts", 0))
                    existing_version = int(existing.get("version", existing_attempts))
                    base_version, base_attempts = snapshot_versions.get(
                        key, (existing_version, existing_attempts)
                    )
                    if candidate.get("status") != "pending":
                        # Terminal settlement on a still-pending item advances version and preserves max attempts
                        candidate["attempts"] = max(existing_attempts + 1, int(candidate.get("attempts", 1)))
                        candidate["version"] = existing_version + 1
                        current_backlog[idx] = candidate
                        mutated = True
                        continue
                    # Reject stale pending poll results when a newer reconciliation already updated this item
                    if existing_version != base_version or existing_attempts != base_attempts:
                        continue
                    current_backlog[idx] = candidate
                    mutated = True
                return mutated

            self._update_reconciliation_backlog(_merge_updates)

    async def _reconciliation_loop(self) -> None:
        interval = max(5, self.config.vcs.reconciliation_interval_seconds)
        while not self._reconciliation_stop.is_set():
            try:
                await self._reconcile_backlog_once()
            except Exception as exc:
                print(f"[{self.worker_id}] Reconciliation loop error: {self._describe_exception(exc)}")
            try:
                await asyncio.wait_for(self._reconciliation_stop.wait(), timeout=interval)
            except TimeoutError:
                pass

    async def _settle_failure(
        self,
        task: TaskContract,
        message: QueueMessage,
        reason: str | NackReason,
    ) -> None:
        """Settle failed work using strict nack/dead-letter semantics."""
        if not await self._register_settlement(message.receipt_handle):
            return

        reason_enum = self._normalize_reason(reason)
        task.settlement_reason = reason_enum
        detail = task.last_error or reason_enum.value
        append_trace_span(
            trace_id=task.trace_id,
            service="worker",
            operation="settle_failure",
            actor_id=self.worker_id,
            status="error",
            task_id=task.id,
            attempt_id=task.attempt_id,
            attributes={"reason": reason_enum.value, "queue_provider": message.provider},
        )

        if reason_enum == NackReason.ERR_CHECK_FAIL and self._should_dead_letter_failure(
            message,
            check_fail=True,
        ):
            self._set_state(task, TaskState.DEAD_LETTER, reason=reason_enum.value)
            await self.queue.dead_letter(message.receipt_handle, reason=reason_enum.value)
            return

        if self._should_dead_letter_failure(message):
            self._set_state(task, TaskState.DEAD_LETTER, reason=reason_enum.value)
            await self.queue.dead_letter(message.receipt_handle, reason=reason_enum.value)
            return

        self._attach_retry_context(task, message, reason_enum, detail)
        self._set_state(task, TaskState.SETTLED_NACK, reason=reason_enum.value)
        delay = min(30 * (2 ** max(message.receive_count, 1)), 300)
        await self.queue.nack(message.receipt_handle, reason=reason_enum.value, delay=delay)

    def _build_sanitized_command_env(self, cwd: str) -> dict[str, str]:
        """Build a minimal environment for acceptance commands without host credentials."""
        _ = cwd
        allowed_keys = {
            "PATH",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "TERM",
            "SYSTEMROOT",
            "PYTHONPATH",
            "VIRTUAL_ENV",
        }
        sanitized: dict[str, str] = {}
        for key in allowed_keys:
            val = os.environ.get(key)
            if val is not None:
                sanitized[key] = val

        python_bin_dir = str(Path(sys.executable).resolve().parent)
        existing_path = sanitized.get("PATH", "")
        path_parts = [part for part in existing_path.split(os.pathsep) if part]
        if python_bin_dir not in path_parts:
            path_parts.insert(0, python_bin_dir)
        sanitized["PATH"] = os.pathsep.join(path_parts)

        isolated_env_root = os.path.join(tempfile.gettempdir(), f"mx_cmd_env_{os.getpid()}")
        isolated_home = os.path.join(isolated_env_root, "home")
        isolated_tmp = os.path.join(isolated_env_root, "tmp")
        os.makedirs(isolated_home, exist_ok=True)
        os.makedirs(isolated_tmp, exist_ok=True)
        sanitized["HOME"] = isolated_home
        sanitized["TMPDIR"] = isolated_tmp
        sanitized["TMP"] = isolated_tmp
        sanitized["TEMP"] = isolated_tmp
        sanitized["XDG_CACHE_HOME"] = os.path.join(isolated_home, ".cache")
        sanitized["PYTHONDONTWRITEBYTECODE"] = "1"
        sanitized["PYTHONUNBUFFERED"] = "1"
        sanitized["GIT_TERMINAL_PROMPT"] = "0"
        return sanitized

    @staticmethod
    def _process_group_alive(pgid: int) -> bool:
        if pgid <= 0 or not hasattr(os, "killpg"):
            return False
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False

    async def _terminate_subprocess_tree(self, proc: object) -> None:
        """Terminate, kill if needed, and reap a timed-out or cancelled subprocess group."""
        pid = getattr(proc, "pid", None)
        pgid: int = pid if isinstance(pid, int) and pid > 0 and hasattr(os, "killpg") else 0
        returncode = getattr(proc, "returncode", None)

        if returncode is not None and (pgid <= 0 or not self._process_group_alive(pgid)):
            return

        killed_group = False
        if pgid > 0:
            try:
                os.killpg(pgid, signal.SIGTERM)
                killed_group = True
            except (ProcessLookupError, PermissionError, OSError):
                pass
        if not killed_group and returncode is None:
            terminate = getattr(proc, "terminate", None)
            if callable(terminate):
                try:
                    terminate()
                except ProcessLookupError:
                    pass

        loop = asyncio.get_running_loop()
        wait_fn = getattr(proc, "wait", None)
        deadline = loop.time() + 0.5
        while loop.time() < deadline:
            if callable(wait_fn) and getattr(proc, "returncode", None) is None:
                try:
                    await asyncio.wait_for(wait_fn(), timeout=0.05)
                except (TimeoutError, Exception):
                    pass
            group_alive = pgid > 0 and self._process_group_alive(pgid)
            proc_alive = getattr(proc, "returncode", None) is None
            if not group_alive and not proc_alive:
                return
            if group_alive:
                await asyncio.sleep(0.05)

        if pgid > 0:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        if getattr(proc, "returncode", None) is None:
            kill_fn = getattr(proc, "kill", None)
            if callable(kill_fn):
                try:
                    kill_fn()
                except ProcessLookupError:
                    pass

        kill_deadline = loop.time() + 1.0
        while pgid > 0 and self._process_group_alive(pgid) and loop.time() < kill_deadline:
            await asyncio.sleep(0.02)

        if callable(wait_fn) and getattr(proc, "returncode", None) is None:
            try:
                await asyncio.wait_for(wait_fn(), timeout=1.0)
            except Exception:
                pass

    async def _run_command(
        self,
        command: str,
        cwd: str,
        timeout: int = 300,
    ) -> tuple[bool, str]:
        """
        Run shell command and return (success, output).
        """
        proc = None
        try:
            resolved_command = command
            try:
                parts = shlex.split(command)
            except ValueError:
                parts = command.split()

            tool = parts[0] if parts else ""
            rewritten_python_module = False
            if len(parts) >= 3 and tool in {"python", "python3"} and parts[1] == "-m" and parts[2] == "ruff":
                ruff_binary = Path(sys.executable).parent / "ruff"
                ruff_cmd = str(ruff_binary) if ruff_binary.exists() else (shutil.which("ruff") or "ruff")
                args = " ".join(shlex.quote(part) for part in parts[3:])
                resolved_command = f'"{ruff_cmd}" {args}'.strip()
                rewritten_python_module = True
            if tool and shutil.which(tool) is None and not rewritten_python_module:
                args = " ".join(parts[1:]) if len(parts) > 1 else ""
                if tool == "pytest":
                    resolved_command = f'"{sys.executable}" -m pytest {args}'.strip()
                elif tool == "ruff":
                    resolved_command = f'"{sys.executable}" -m ruff {args}'.strip()
                elif tool in {"python", "python3"}:
                    resolved_command = f'"{sys.executable}" {args}'.strip()

            proc = await asyncio.create_subprocess_shell(
                resolved_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
                env=self._build_sanitized_command_env(cwd),
                start_new_session=True,
            )

            stdout, _ = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )

            output = stdout.decode("utf-8", errors="replace")
            success = proc.returncode == 0

            return success, output[:5000]  # Truncate long outputs

        except TimeoutError:
            if proc is not None:
                await self._terminate_subprocess_tree(proc)
            return False, f"Command timed out after {timeout}s"
        except asyncio.CancelledError:
            if proc is not None:
                await self._terminate_subprocess_tree(proc)
            raise
        except Exception as e:
            if proc is not None:
                await self._terminate_subprocess_tree(proc)
            return False, self._describe_exception(e, context="command execution failed")
        finally:
            if proc is not None:
                await self._terminate_subprocess_tree(proc)

    async def _repo_has_changes(self, repo: object, changed_files: list[str]) -> bool:
        """Prefer repo-native diff detection but tolerate lightweight repo stubs."""
        has_changes = getattr(repo, "has_changes", None)
        if callable(has_changes):
            result = has_changes()
            if asyncio.iscoroutine(result):
                return bool(await result)
            return bool(result)
        return bool(changed_files)

    async def _get_codebase_context(self, repo: GitRepo, task: TaskContract) -> str:
        """Build context about the codebase for LLM."""
        context_parts = []

        # Include editable targets plus read-only reference files such as tests.
        reference_paths: list[str] = []
        for path in [*task.context.files_hint, *task.context.do_not_touch]:
            normalized = self._normalize_relative_path(path)
            if normalized and normalized not in reference_paths:
                reference_paths.append(normalized)

        editable_limit = 3000
        read_only_limit = 3000
        remaining_total: int | None = None
        if self._local_model_proof_mode_enabled() and self._use_compact_local_prompt(task):
            editable_limit = 1600
            read_only_limit = 1200
            remaining_total = 2200

        for path in reference_paths[:8]:
            content = await self._read_file(repo, path)
            if not content:
                continue
            header = "### Editable Target" if path in task.context.files_hint else "### Read-Only Reference"
            content_limit = editable_limit if path in task.context.files_hint else read_only_limit
            if remaining_total is not None:
                if remaining_total <= 0:
                    break
                content_limit = min(content_limit, remaining_total)
            snippet = content[:content_limit]
            context_parts.append(f"{header}: {path}\n```\n{snippet}\n```")
            if remaining_total is not None:
                remaining_total -= len(snippet)

        # For strict-file tasks, extra repo-wide context is mostly noise for weak models.
        if reference_paths:
            return "\n\n".join(context_parts)

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

    async def _build_strict_editing_guidance(
        self,
        repo: GitRepo,
        allowed_paths: list[str],
    ) -> str:
        """Build extra instructions for existing strict-file scaffolds."""
        if len(allowed_paths) != 1:
            return ""

        target_path = self._normalize_relative_path(allowed_paths[0])
        if not target_path:
            return ""

        content = await self._read_file(repo, target_path)
        if not content:
            return ""

        guidance = [
            "- Single editable file task. Keep the existing module structure and imports unless correctness requires a change.",
            "- Do not add non-standard-library imports.",
            "- Do not add new third-party dependencies or helper libraries.",
        ]
        if "NotImplementedError" in content:
            guidance.append("- Replace the existing NotImplementedError stub implementations instead of redesigning the file.")
            guidance.append("- Keep the surrounding parser, error handling, and output format intact.")

            stub_functions: list[str] = []
            current_function: str | None = None
            current_contains_stub = False
            for raw_line in content.splitlines():
                stripped = raw_line.strip()
                if stripped.startswith("def ") and stripped.endswith(":"):
                    if current_function and current_contains_stub:
                        stub_functions.append(current_function)
                    current_function = stripped.split("(", 1)[0].removeprefix("def ").strip()
                    current_contains_stub = False
                    continue
                if "NotImplementedError" in stripped:
                    current_contains_stub = True
            if current_function and current_contains_stub:
                stub_functions.append(current_function)
            if stub_functions:
                guidance.append(
                    "- Only replace the stub bodies for these functions: "
                    + ", ".join(stub_functions[:8])
                    + "."
                )
                guidance.append(
                    "- In the fenced block, emit only replacement definitions for those stub functions."
                )
                guidance.append(
                    "- Do not repeat the module docstring, imports, parser, main entrypoint, or tests."
                )
                guidance.append("- Leave the rest of the file structure untouched unless a test strictly requires a tiny fix.")

        if "sorted(" in content or "key_count" in content or "keys:" in content:
            guidance.append("- Use built-in sorted(...) and plain Python lists; do not introduce custom collection types.")

        signatures = [
            line.strip()
            for line in content.splitlines()
            if line.strip().startswith("def ") and line.strip().endswith(":")
        ]
        if signatures:
            guidance.append("- Preserve these function signatures exactly:")
            guidance.extend(f"  - {signature}" for signature in signatures[:8])

        return "\n".join(guidance)

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
        fixture_path = self._canned_completion_fixture_path()
        if fixture_path is not None:
            try:
                with open(fixture_path, encoding="utf-8") as handle:
                    payload = json.load(handle)
            except Exception as exc:
                raise WorkerFastFailError(
                    reason=NackReason.ERR_TASK_FAILED.value,
                    detail=f"Unable to load canned completion fixture {fixture_path}: {self._describe_exception(exc)}",
                ) from exc

            if not isinstance(payload.get("files"), dict):
                task_specific = payload.get(task.id) or payload.get(task.goal)
                if not isinstance(task_specific, dict):
                    raise WorkerFastFailError(
                        reason=NackReason.ERR_TASK_FAILED.value,
                        detail=f"Canned completion fixture {fixture_path} is missing target task ID/goal {task.id}",
                    )
                files_payload = task_specific.get("files")
                note = task_specific.get("note")
                if not isinstance(files_payload, dict):
                    raise WorkerFastFailError(
                        reason=NackReason.ERR_TASK_FAILED.value,
                        detail=f"Canned completion fixture {fixture_path} is missing a 'files' object.",
                    )
                apply_attr = "apply_changes"
                apply_fn = getattr(self, apply_attr, None)
                if callable(apply_fn):
                    changed_files, notes = await apply_fn(repo, task)
                    return changed_files, str(notes)
                changed_files = []
                for relative_path, content in files_payload.items():
                    if relative_path.endswith(".py"):
                        self._validate_python_syntax(relative_path, content)
                    await self._write_file(repo, relative_path, content)
                    changed_files.append(relative_path)
                return changed_files, str(note or "Implementation complete (canned).")

        context = await self._get_codebase_context(repo, task)

        # Read AGENTS.md for guidelines
        agents_md = await self._read_file(repo, "AGENTS.md") or ""
        allowed_paths = [path.strip() for path in task.context.files_hint if path.strip()]
        allowed_paths_set = {
            self._normalize_relative_path(path)
            for path in allowed_paths
            if self._normalize_relative_path(path)
        }
        do_not_touch_set = {
            self._normalize_relative_path(path)
            for path in task.context.do_not_touch
            if self._normalize_relative_path(path)
        }
        allowed_paths_text = (
            "\n".join(f"- {path}" for path in allowed_paths)
            if allowed_paths
            else "- (No strict file hints provided. Infer minimal paths from goal.)"
        )
        strict_editing_guidance = await self._build_strict_editing_guidance(repo, allowed_paths)
        retry_guidance = self._build_retry_guidance(task)

        prompt = self._build_task_prompt(
            task=task,
            context=context,
            agents_md=agents_md,
            allowed_paths_text=allowed_paths_text,
            strict_editing_guidance=strict_editing_guidance,
            retry_guidance=retry_guidance,
        )

        llm_config = self._llm_config_for_task(task)
        llm_client = self._llm_client_for_task(task)
        llm_timeout_seconds = self._llm_chat_timeout_seconds(task)
        llm_max_tokens = self._llm_max_tokens_for_task(task)
        print(
            f"[{self.worker_id}] LLM request budget: "
            f"provider={llm_config.provider} "
            f"model={llm_config.model} "
            f"prompt_chars={len(prompt)} "
            f"max_tokens={llm_max_tokens} "
            f"timeout={llm_timeout_seconds}s"
        )
        append_trace_span(
            trace_id=task.trace_id,
            service="worker",
            operation="llm_chat",
            actor_id=self.worker_id,
            task_id=task.id,
            attempt_id=task.attempt_id,
            status="started",
            attributes={
                "provider": llm_config.provider,
                "model": llm_config.model,
                "prompt_chars": len(prompt),
                "max_tokens": llm_max_tokens,
                "timeout_seconds": llm_timeout_seconds,
            },
        )
        canned_completion = self._build_canned_completion(task)
        try:
            if canned_completion is not None:
                response, canned_metadata = canned_completion
                append_trace_span(
                    trace_id=task.trace_id,
                    service="worker",
                    operation="model_bypassed_canned",
                    actor_id=self.worker_id,
                    task_id=task.id,
                    attempt_id=task.attempt_id,
                    attributes={
                        **canned_metadata,
                        "provider": llm_config.provider,
                        "model": llm_config.model,
                    },
                )
                append_trace_span(
                    trace_id=task.trace_id,
                    service="worker",
                    operation="model_response_received",
                    actor_id=self.worker_id,
                    task_id=task.id,
                    attempt_id=task.attempt_id,
                    attributes={
                        "provider": llm_config.provider,
                        "model": llm_config.model,
                        "synthetic": True,
                        **canned_metadata,
                    },
                )
            else:
                response = await asyncio.wait_for(
                    llm_client.chat(
                        messages=[Message(role="user", content=prompt)],
                        system=WORKER_SYSTEM_PROMPT,
                        max_tokens=llm_max_tokens,
                        temperature=0.0 if strict_editing_guidance else 0.2,
                    ),
                    timeout=llm_timeout_seconds,
                )
                if response.content:
                    append_trace_span(
                        trace_id=task.trace_id,
                        service="worker",
                        operation="model_response_received",
                        actor_id=self.worker_id,
                        task_id=task.id,
                        attempt_id=task.attempt_id,
                        attributes={
                            "provider": llm_config.provider,
                            "model": llm_config.model,
                            "synthetic": False,
                            "response_chars": len(response.content),
                        },
                    )
            append_trace_span(
                trace_id=task.trace_id,
                service="worker",
                operation="llm_chat",
                actor_id=self.worker_id,
                task_id=task.id,
                attempt_id=task.attempt_id,
                status="ok",
                attributes={
                    "provider": llm_config.provider,
                    "model": llm_config.model,
                    "prompt_chars": len(prompt),
                    "max_tokens": llm_max_tokens,
                    "timeout_seconds": llm_timeout_seconds,
                },
            )
        except TimeoutError as exc:
            append_trace_span(
                trace_id=task.trace_id,
                service="worker",
                operation="model_timeout",
                actor_id=self.worker_id,
                task_id=task.id,
                attempt_id=task.attempt_id,
                status="error",
                attributes={
                    "provider": llm_config.provider,
                    "model": llm_config.model,
                    "prompt_chars": len(prompt),
                    "max_tokens": llm_max_tokens,
                    "timeout_seconds": llm_timeout_seconds,
                },
            )
            append_trace_span(
                trace_id=task.trace_id,
                service="worker",
                operation="llm_chat",
                actor_id=self.worker_id,
                task_id=task.id,
                attempt_id=task.attempt_id,
                status="timeout",
                attributes={
                    "provider": llm_config.provider,
                    "model": llm_config.model,
                    "prompt_chars": len(prompt),
                    "max_tokens": llm_max_tokens,
                    "timeout_seconds": llm_timeout_seconds,
                },
            )
            raise WorkerFastFailError(
                reason=NackReason.ERR_TASK_FAILED.value,
                detail=(
                    f"LLM chat timed out after {llm_timeout_seconds}s "
                    f"for {llm_config.provider}/{llm_config.model}"
                ),
            ) from exc
        except Exception as exc:
            append_trace_span(
                trace_id=task.trace_id,
                service="worker",
                operation="llm_chat",
                actor_id=self.worker_id,
                task_id=task.id,
                attempt_id=task.attempt_id,
                status="error",
                attributes={
                    "provider": llm_config.provider,
                    "model": llm_config.model,
                    "prompt_chars": len(prompt),
                    "max_tokens": llm_max_tokens,
                    "error": self._describe_exception(exc),
                },
            )
            raise

        self.metrics.llm_calls += 1
        usage = response.usage if hasattr(response, "usage") else {}
        if usage:
            self.metrics.total_tokens += usage.get("input_tokens", 0)
            self.metrics.total_tokens += usage.get("output_tokens", 0)
        estimated_cost_usd = self._estimate_llm_cost_usd(task, usage)
        append_llm_usage_event(
            trace_id=task.trace_id,
            task_id=task.id,
            attempt_id=task.attempt_id,
            actor_id=self.worker_id,
            provider=llm_config.provider,
            model=llm_config.model,
            model_tier=task.model_tier.value,
            routing_target=task.routing_target.value,
            estimated_cost_bucket=task.estimated_cost_bucket or self._cost_bucket_for_task(task),
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            estimated_cost_usd=estimated_cost_usd,
        )
        attempt_key = self._attempt_key(task)
        self._attempt_llm_cost_usd[attempt_key] = self._attempt_llm_cost_usd.get(attempt_key, 0.0) + estimated_cost_usd

        content = response.content
        if self._is_refusal_response(content):
            raise WorkerFastFailError(
                reason=NackReason.ERR_REFUSAL.value,
                detail="Model refused task execution or reported insufficient information.",
            )

        # Debug: print LLM response preview
        print(f"[{self.worker_id}] LLM response preview: {content[:500]}...")

        # Parse file changes
        changed_files = []
        matches = self._extract_file_blocks(content)

        print(f"[{self.worker_id}] Found {len(matches)} file blocks in response")
        if matches:
            emitted_paths = [path.strip() for path, _ in matches]
            print(f"[{self.worker_id}] Emitted paths: {emitted_paths}")

        for path, file_content in matches:
            raw_path = path.strip()
            resolved_path, resolution_note = self._resolve_allowed_target_path(raw_path, allowed_paths_set)
            if resolved_path is None:
                print(f"[{self.worker_id}] Skipping file block {raw_path!r}: {resolution_note}")
                continue

            if resolution_note:
                print(f"[{self.worker_id}] Remapped file block {raw_path!r}: {resolution_note}")

            # Skip if in do_not_touch
            if resolved_path in do_not_touch_set:
                print(f"[{self.worker_id}] Skipping file block {raw_path!r}: do_not_touch")
                continue

            normalized_content = file_content.strip()
            if resolved_path.endswith(".py") and len(allowed_paths_set) == 1:
                existing_content = await self._read_file(repo, resolved_path)
                if existing_content and "NotImplementedError" in existing_content:
                    merged_content = self._merge_partial_python_update(
                        existing_content=existing_content,
                        emitted_content=normalized_content,
                    )
                    if merged_content:
                        print(
                            f"[{self.worker_id}] Rehydrated partial Python update for {resolved_path}"
                        )
                        normalized_content = merged_content.strip()
            if resolved_path.endswith(".py"):
                self._validate_python_syntax(resolved_path, normalized_content)
            await self._write_file(repo, resolved_path, normalized_content)
            changed_files.append(resolved_path)

        # Extract notes (text after last code block)
        parts = content.split("```")
        notes = parts[-1].strip() if len(parts) > 1 else "Implementation complete."

        if not changed_files:
            raise WorkerFastFailError(
                reason=NackReason.ERR_NO_DIFF.value,
                detail=f"No file blocks produced for task {task.id}",
            )

        return changed_files, notes

    async def _run_acceptance_checks(
        self,
        repo: GitRepo,
        task: TaskContract,
    ) -> list[tuple[str, bool, str]]:
        """Run acceptance check commands while preserving trusted .git metadata."""
        if hasattr(repo, "scrub_workspace_credentials"):
            repo.scrub_workspace_credentials()
        trusted_git_snapshot = (
            repo.snapshot_trusted_git_dir()
            if hasattr(repo, "snapshot_trusted_git_dir")
            else None
        )
        results = []

        try:
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
        finally:
            self._executed_acceptance_checks[task.id] = [
                {
                    "command": cmd,
                    "passed": bool(passed),
                    "summary": summary,
                }
                for cmd, passed, summary in results
            ]
            if hasattr(repo, "restore_trusted_git_dir"):
                repo.restore_trusted_git_dir(trusted_git_snapshot)
            elif hasattr(repo, "scrub_workspace_credentials"):
                repo.scrub_workspace_credentials()

    async def _canonicalize_changed_python_files(
        self,
        repo: GitRepo,
        changed_files: list[str],
    ) -> None:
        """Run deterministic lint-fix/format passes on generated Python files."""
        python_files = [path for path in changed_files if path.endswith(".py")]
        if not python_files:
            return

        quoted_paths = " ".join(shlex.quote(path) for path in python_files[:20])
        commands = [
            f"python -m ruff check --fix {quoted_paths}",
            f"python -m ruff format {quoted_paths}",
        ]

        for command in commands:
            passed, output = await self._run_command(
                command,
                cwd=repo.local_path,
                timeout=60,
            )
            if not passed:
                summary = output.strip().splitlines()
                suffix = summary[-1] if summary else "No output"
                print(f"[{self.worker_id}] Canonicalization warning: {command} -> {suffix[:200]}")

    def _contention_barrier_dir(self) -> str:
        raw = os.environ.get("MX_CONTENTION_BARRIER_DIR", "").strip()
        return raw or os.path.join(".runtime", "barrier")

    def _contention_barrier_group_id(self, task: TaskContract) -> str:
        run_id = self._submission_source_run_id(task) or "default_run"
        repo_id = getattr(self._vcs, "repo_id", None) or "default_repo"
        return f"semantic-canary:{run_id}:{repo_id}:{task.contention_group_id}"

    def _write_barrier_fail_token(self, task: TaskContract, reason: str) -> None:
        if not (task.contention_expected_tasks and task.contention_group_id):
            return
        barrier_dir = self._contention_barrier_dir()
        os.makedirs(barrier_dir, exist_ok=True)
        group_id = self._contention_barrier_group_id(task)
        fail_file = os.path.join(barrier_dir, f"{group_id}_{task.id}_failed.json")
        try:
            with open(fail_file, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "task_id": task.id,
                        "status": "failed",
                        "reason": reason,
                        "timestamp": time.time(),
                    },
                    handle,
                )
            print(f"[{self.worker_id}] Wrote barrier fail-fast token to {fail_file}")
        except Exception as fe:
            print(f"[{self.worker_id}] Failed to write barrier fail-fast token: {fe}")

    async def materialize_aether_base_head(
        self,
        repo: GitRepo,
        actual_head: str,
        workspace_root: str,
        *,
        tar_bytes_override: bytes | None = None,
    ) -> bool:
        """Download and stage an Aether base head snapshot via the single bounded extractor."""
        base_url = str(
            getattr(self._vcs, "base_url", self.config.vcs.aether_base_url)
        ).rstrip("/")
        repo_id = str(getattr(self._vcs, "repo_id", self.config.vcs.aether_repo_id))
        snapshot_id: str | None = actual_head if actual_head.startswith("snap") else None

        if tar_bytes_override is not None:
            tar_bytes = tar_bytes_override
            snapshot_id = snapshot_id or "snap_override"
        else:
            if not snapshot_id:
                head_url = f"{base_url}/v1/repos/{repo_id}/head"
                if urllib.parse.urlparse(head_url).scheme in {"http", "https"}:
                    req = urllib.request.Request(head_url, method="GET")
                    with urllib.request.urlopen(req, timeout=5) as response:  # nosec B310
                        head_data = json.loads(response.read().decode("utf-8"))
                        if head_data.get("head_id") == actual_head:
                            snapshot_id = head_data.get("snapshot_id")

            if not snapshot_id:
                prom_url = f"{base_url}/v1/repos/{repo_id}/promotions"
                if urllib.parse.urlparse(prom_url).scheme in {"http", "https"}:
                    req = urllib.request.Request(prom_url, method="GET")
                    with urllib.request.urlopen(req, timeout=5) as response:  # nosec B310
                        prom_data = json.loads(response.read().decode("utf-8"))
                        for prom in prom_data.get("promotions", []):
                            if prom.get("to_head_id") == actual_head:
                                snapshot_id = prom.get("snapshot_id")
                                break

            if not snapshot_id:
                return False

            dl_url = f"{base_url}/v1/repos/{repo_id}/snapshots/{snapshot_id}/download"
            if urllib.parse.urlparse(dl_url).scheme not in {"http", "https"}:
                return False
            dl_req = urllib.request.Request(dl_url, method="GET")
            with urllib.request.urlopen(dl_req, timeout=10) as dl_resp:  # nosec B310
                tar_bytes = dl_resp.read()

        copied_rel_paths = await self.extract_snapshot_securely(
            tar_bytes,
            workspace_root,
            repo_id=repo_id,
            snapshot_id=snapshot_id,
            require_manifest=False,
            clean_destination=True,
        )
        if hasattr(repo, "scrub_workspace_credentials"):
            repo.scrub_workspace_credentials()
        add_res = await repo.add()
        if not add_res.success:
            raise WorkerFastFailError(
                reason=NackReason.ERR_TASK_FAILED.value,
                detail=f"Failed to stage materialized Aether base head {actual_head}: {add_res.stderr}",
            )
        status_res = await repo.get_status()
        if status_res.stdout.strip():
            commit_res = await repo.commit(
                f"Checkout Aether base head {actual_head}",
                author="MX Swarm <mx-swarm@automated.local>",
            )
            if not commit_res.success:
                raise WorkerFastFailError(
                    reason=NackReason.ERR_TASK_FAILED.value,
                    detail=f"Failed to commit materialized Aether base head {actual_head}: {commit_res.stderr}",
                )
        base_checkout_res = await repo.checkout("HEAD", create=False)
        if not base_checkout_res.success:
            return False

        ws_path = Path(workspace_root).resolve()
        expected_set = set(copied_rel_paths)
        actual_worktree = {
            p.relative_to(ws_path).as_posix()
            for p in ws_path.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(ws_path).parts
        }
        ls_res = await repo._run_local_git(["ls-files", "-z"])
        tracked_set = {entry for entry in ls_res.stdout.split("\0") if entry}
        if actual_worktree != expected_set or tracked_set != expected_set:
            raise WorkerFastFailError(
                reason=NackReason.ERR_TASK_FAILED.value,
                detail=(
                    f"Materialized tree mismatch for {actual_head}: "
                    f"expected={sorted(expected_set)}, worktree={sorted(actual_worktree)}, tracked={sorted(tracked_set)}"
                ),
            )
        return True

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
        if task.contention_expected_tasks and task.contention_group_id:
            barrier_dir = self._contention_barrier_dir()
            os.makedirs(barrier_dir, exist_ok=True)
            group_id = self._contention_barrier_group_id(task)
            ready_file = os.path.join(barrier_dir, f"{group_id}_{task.id}.json")
            with open(ready_file, "w", encoding="utf-8") as handle:
                json.dump(
                    {"task_id": task.id, "status": "ready", "timestamp": time.time()},
                    handle,
                )
            print(f"[{self.worker_id}] Submission Barrier: Wrote ready token to {ready_file}")

            start_time = time.time()
            timeout_seconds = 120.0
            poll_interval = 0.5
            try:
                while True:
                    if time.time() - start_time > timeout_seconds:
                        self._write_barrier_fail_token(task, "timeout")
                        raise WorkerFastFailError(
                            reason=NackReason.ERR_BARRIER_TIMEOUT.value,
                            detail=(
                                f"Barrier timeout exceeded ({timeout_seconds}s) waiting for "
                                f"contention tasks: {task.contention_expected_tasks}"
                            ),
                        )

                    files = os.listdir(barrier_dir)
                    fail_files = [
                        fname
                        for fname in files
                        if fname.startswith(f"{group_id}_") and fname.endswith("_failed.json")
                    ]
                    if fail_files:
                        raise WorkerFastFailError(
                            reason=NackReason.ERR_BARRIER_ABORTED.value,
                            detail=f"Barrier aborted: Another contention task failed: {fail_files}",
                        )

                    ready_tasks = [
                        t_id
                        for t_id in task.contention_expected_tasks
                        if os.path.exists(os.path.join(barrier_dir, f"{group_id}_{t_id}.json"))
                    ]
                    if len(ready_tasks) == len(task.contention_expected_tasks):
                        print(
                            f"[{self.worker_id}] Submission Barrier: All tasks ready. "
                            f"Proceeding to submit cohort: {task.contention_expected_tasks}"
                        )
                        break

                    await asyncio.sleep(poll_interval)
            except Exception as exc:
                self._write_barrier_fail_token(task, str(exc))
                raise

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
            service = "aether" if self.config.vcs.provider == "aether" else "github"
            append_trace_span(
                trace_id=task.trace_id,
                service=service,
                operation="submit_change",
                actor_id=self.worker_id,
                task_id=task.id,
                attempt_id=task.attempt_id,
                status="started",
                attributes={
                    "draft": draft,
                    "affected_paths": len(changed_files),
                    "provider": self.config.vcs.provider,
                },
            )
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
                source="mx_swarm",
                source_run_id=self._submission_source_run_id(task),
                source_task_id=task.id,
                source_agent_id=self.worker_id,
                source_attempt_id=task.attempt_id,
                intent_type=self._submission_intent_type(task),
                contention_mode=task.contention_mode,
                contention_group_id=(
                    self._contention_barrier_group_id(task)
                    if task.contention_group_id
                    else None
                ),
                contention_policy=task.contention_policy,
                target_paths=task.contention_target_paths,
                base_head_id=task.contention_base_head_id,
                defer_evaluation=task.defer_evaluation,
            )

            if result.success:
                self.metrics.prs_opened += 1
                print(f"[{self.worker_id}] VCS submission: {result.submission_type} {result.submission_id} / {result.status}")
                append_trace_span(
                    trace_id=task.trace_id,
                    service=service,
                    operation="submit_change",
                    actor_id=self.worker_id,
                    task_id=task.id,
                    attempt_id=task.attempt_id,
                    attributes={
                        "submission_id": result.submission_id,
                        "submission_type": result.submission_type,
                        "status": result.status,
                        "draft": draft,
                        "affected_paths": len(changed_files),
                    },
                )
                append_trace_span(
                    trace_id=task.trace_id,
                    service=service,
                    operation="proposal_id_received",
                    actor_id=self.worker_id,
                    task_id=task.id,
                    attempt_id=task.attempt_id,
                    attributes={
                        "submission_id": result.submission_id,
                        "submission_type": result.submission_type,
                        "status": result.status,
                    },
                )
                self._record_submission_event(
                    task,
                    submission_id=result.submission_id,
                    submission_type=result.submission_type,
                    status=result.status,
                    url=result.url,
                    affected_paths=changed_files,
                    expected_diff=diff_content,
                )
                return {
                    "number": result.submission_id,
                    "type": result.submission_type,
                    "url": result.url,
                    "status": result.status,
                }
            else:
                print(f"[{self.worker_id}] VCS submission failed: {result.message}")
                append_trace_span(
                    trace_id=task.trace_id,
                    service=service,
                    operation="submit_change",
                    actor_id=self.worker_id,
                    task_id=task.id,
                    attempt_id=task.attempt_id,
                    status="error",
                    attributes={
                        "message": result.message,
                        "http_status": result.http_status,
                    },
                )
                raise RuntimeError(result.message)

        except Exception as e:
            print(f"[{self.worker_id}] Failed to submit to VCS: {self._describe_exception(e)}")
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
        task = TaskContract.from_json(message.body)
        if task.previous_failure_reason is None:
            task.previous_failure_reason = self._normalize_retry_reason(
                message.attributes.get("last_nack_reason")
            )
        if task.previous_failure_detail is None:
            previous_detail = message.attributes.get("last_nack_detail")
            task.previous_failure_detail = previous_detail if isinstance(previous_detail, str) else None
        if task.previous_attempt_id is None:
            previous_attempt_id = message.attributes.get("previous_attempt_id")
            task.previous_attempt_id = previous_attempt_id if isinstance(previous_attempt_id, str) else None
        if task.previous_attempt_number is None:
            previous_attempt_number = message.attributes.get("previous_attempt_number")
            if isinstance(previous_attempt_number, int):
                task.previous_attempt_number = previous_attempt_number
        task.attempt_count = message.receive_count
        task.attempt_id = f"attempt-{message.receive_count}"
        task.assigned_worker = self.worker_id
        task.estimated_cost_bucket = task.estimated_cost_bucket or self._cost_bucket_for_task(task)
        if task.model_tier == ModelTier.FRONTIER:
            task.frontier_attempt_count += 1
            task.escalation_reason = task.escalation_reason or task.routing_reason or "frontier_claim"

        print(f"[{self.worker_id}] Processing task {task.id}: {task.goal}")
        self._current_task = task
        self._set_state(task, TaskState.CLAIMED)
        single_flight_acquired = self._try_acquire_task_single_flight(task.id)
        if not single_flight_acquired:
            if await self._register_settlement(message.receipt_handle):
                task.last_error = "Duplicate claim prevented by single-flight guard."
                self._attach_retry_context(task, message, NackReason.ERR_QUEUE_LEASE, task.last_error)
                self._set_state(task, TaskState.SETTLED_NACK, reason=NackReason.ERR_QUEUE_LEASE.value)
                await self.queue.nack(
                    message.receipt_handle,
                    reason=NackReason.ERR_QUEUE_LEASE.value,
                    delay=5,
                )
            self.metrics.tasks_failed += 1
            self.metrics.tasks_processed += 1
            self._current_task = None
            return False

        append_trace_span(
            trace_id=task.trace_id,
            service="worker",
            operation="claim_task",
            actor_id=self.worker_id,
            task_id=task.id,
            attempt_id=task.attempt_id,
            attributes={
                "queue_message_id": message.id,
                "receipt_handle": message.receipt_handle,
                "queue_provider": message.provider,
                "receive_count": message.receive_count,
            },
        )
        append_trace_span(
            trace_id=task.trace_id,
            service="worker",
            operation="task_selected",
            actor_id=self.worker_id,
            task_id=task.id,
            attempt_id=task.attempt_id,
            attributes={
                "queue_message_id": message.id,
                "routing_target": task.routing_target.value,
                "model_tier": task.model_tier.value,
            },
        )

        budget_block_reason = self._frontier_claim_block_reason(task)
        if budget_block_reason:
            task.last_error = f"Claim blocked by budget policy: {budget_block_reason}"
            self._emit_budget_freeze(task, budget_block_reason)
            await self._settle_failure(task, message, NackReason.ERR_RATE_LIMIT)
            self.metrics.tasks_failed += 1
            self.metrics.tasks_processed += 1
            self._current_task = None
            return False

        token = self._effective_github_token()
        is_local_mode = not token
        workspace_root = self._create_workspace_root()
        heartbeat_stop = asyncio.Event()
        heartbeat_task = asyncio.create_task(self._lease_heartbeat_loop(message, heartbeat_stop))
        frontier_claim_started = False
        frontier_finish_status = "abandoned"
        frontier_finish_reason: str | None = None
        if task.model_tier == ModelTier.FRONTIER:
            self._record_frontier_claim_start(task)
            frontier_claim_started = True
        source_root = self._get_repo_url()
        clone_branch = None if os.path.isdir(source_root) else self.config.github.default_branch
        print(
            f"[{self.worker_id}] Clone plan: "
            f"repo_url={source_root!r} "
            f"is_dir={os.path.isdir(source_root)} "
            f"branch={clone_branch!r}"
        )

        repo = GitRepo(
            repo_url=source_root,
            local_path=workspace_root,
            token=token,
        )

        try:
            clone_result = await repo.clone(
                branch=clone_branch,
                depth=1,
            )
            if not clone_result.success:
                if is_local_mode:
                    print(f"[{self.worker_id}] Local clone failed: {clone_result.stderr}")
                raise WorkerFastFailError(
                    reason=NackReason.ERR_TASK_FAILED.value,
                    detail=f"Clone failed: {clone_result.stderr}",
                )

            if os.path.isdir(source_root):
                copied_overlay_files = self._overlay_local_workspace_files(
                    source_root=source_root,
                    workspace_root=workspace_root,
                    rel_paths=[
                        *task.context.files_hint,
                        *task.context.do_not_touch,
                        "AGENTS.md",
                        "PRD.md",
                    ],
                )
                if copied_overlay_files:
                    print(
                        f"[{self.worker_id}] Overlayed local workspace files: {copied_overlay_files}"
                    )

            if task.contention_base_head_id:
                actual_head = task.contention_base_head_id
                print(f"[{self.worker_id}] Contention Mode: Checking out base head {actual_head}")
                base_checkout_res = await repo.checkout(actual_head, create=False)

                if not base_checkout_res.success and self.config.vcs.provider == "aether":
                    try:
                        materialized = await self.materialize_aether_base_head(
                            repo=repo,
                            actual_head=actual_head,
                            workspace_root=workspace_root,
                        )
                        if materialized:
                            base_checkout_res = await repo.checkout("HEAD", create=False)
                    except WorkerFastFailError:
                        raise
                    except Exception as exc:
                        print(f"[{self.worker_id}] Error materializing Aether base head {actual_head}: {exc}")

                if not base_checkout_res.success:
                    raise WorkerFastFailError(
                        reason=NackReason.ERR_TASK_FAILED.value,
                        detail=f"Base head checkout failed: {base_checkout_res.stderr}",
                    )

            branch_name = task.branch_name()
            checkout_result = await repo.checkout(branch_name, create=True)
            if not checkout_result.success:
                raise WorkerFastFailError(
                    reason=NackReason.ERR_TASK_FAILED.value,
                    detail=f"Checkout failed: {checkout_result.stderr}",
                )

            self._set_state(task, TaskState.EXECUTING)
            append_trace_span(
                trace_id=task.trace_id,
                service="worker",
                operation="execute_task",
                actor_id=self.worker_id,
                task_id=task.id,
                attempt_id=task.attempt_id,
                attributes={"workspace_root": workspace_root},
            )

            changed_files, notes = await self._implement_task(repo, task)
            await self._canonicalize_changed_python_files(repo, changed_files)

            # OP-2: Redefine 'No Change' worker outcome from ACK to NACK.
            # This prevents worker loops from silently succeeding without producing value.
            if not await self._repo_has_changes(repo, changed_files):
                raise WorkerFastFailError(
                    reason=NackReason.ERR_NO_DIFF.value,
                    detail=(
                        f"Implementation produced no functional changes for task {task.id}. "
                        "The model may have returned content identical to the existing state."
                    ),
                )

            self._set_state(task, TaskState.ARTIFACT_READY)

            check_results = await self._run_acceptance_checks(repo, task)
            all_passed = all(passed for _, passed, _ in check_results)
            if not all_passed:
                failed_checks = [
                    f"{cmd} -> {summary}"
                    for cmd, passed, summary in check_results
                    if not passed
                ]
                failure_detail = (
                    "Acceptance checks failed: "
                    + " | ".join(failed_checks[:3])
                )
                raise WorkerFastFailError(
                    reason=NackReason.ERR_CHECK_FAIL.value,
                    detail=failure_detail,
                )

            risks = []
            if task.constraints.risk == RiskLevel.HIGH:
                risks.append("High-risk task - requires careful review")
            if any("TODO" in f for f in changed_files):
                risks.append("Contains TODO markers")

            evidence = EvidencePack(
                task_id=task.id,
                task_goal=task.goal,
                commands_run=check_results,
                files_changed=changed_files,
                verification_method=notes,
                known_risks=risks,
                execution_trace={
                    "worker_id": self.worker_id,
                    "trace_id": task.trace_id,
                    "attempt_id": task.attempt_id,
                    "queue_message_id": message.id,
                    "queue_provider": message.provider,
                    "planned_settlement": "ACK",
                },
            )

            add_result = await repo.add(paths=changed_files)
            if not add_result.success:
                raise WorkerFastFailError(
                    reason=NackReason.ERR_TASK_FAILED.value,
                    detail=f"Git add failed: {add_result.stderr}",
                )

            commit_msg = f"[{task.id}] {task.goal}\n\n{notes[:500]}"
            commit_result = await repo.commit(
                message=commit_msg,
                author="MX Swarm <mx-swarm@automated.local>",
            )
            if not commit_result.success:
                raise WorkerFastFailError(
                    reason=NackReason.ERR_TASK_FAILED.value,
                    detail=f"Git commit failed: {commit_result.stderr}",
                )

            if self.config.vcs.provider != "aether":
                push_result = await repo.push(branch_name)
                if not push_result.success:
                    raise WorkerFastFailError(
                        reason=NackReason.ERR_TASK_FAILED.value,
                        detail=f"Push failed: {push_result.stderr}",
                    )
                if is_local_mode:
                    print(
                        f"[{self.worker_id}] Local Mode: Pushed branch {branch_name} "
                        f"to {self._get_repo_url()}"
                    )
            else:
                print(f"[{self.worker_id}] Aether mode: Skipping GitHub push, submitting diff directly")

            self._set_state(task, TaskState.VERIFIED)

            submission = await self._submit_to_vcs(
                repo=repo,
                task=task,
                evidence=evidence,
                changed_files=changed_files,
                all_passed=all_passed,
                draft=False,
            )
            if submission:
                print(
                    f"[{self.worker_id}] Submitted {submission.get('type')} "
                    f"#{submission.get('number')} for task {task.id}"
                )
                task.submission_ledger_id = str(submission.get("number", ""))

            if is_local_mode and self.config.vcs.provider != "aether":
                self._materialize_local_landing(
                    workspace_root=workspace_root,
                    source_root=source_root,
                    changed_files=changed_files,
                )
                if submission:
                    self._close_local_mock_submission(str(submission.get("number", "")))

            if (
                self.config.vcs.provider == "aether"
                and self.config.vcs.canonical_enforcement
                and task.submission_ledger_id
            ):
                self._set_state(task, TaskState.SUBMITTED_PENDING_LEDGER)
                canonical_status = await self._await_canonical_acceptance(task.submission_ledger_id)
                self._update_submission_record(task, canonical_status=canonical_status)
                if canonical_status == "accepted":
                    if self.config.vcs.post_landing_verification_required:
                        await self._verify_post_landing_submission(
                            task,
                            submission_id=task.submission_ledger_id,
                            affected_paths=changed_files,
                        )
                elif canonical_status == "rejected":
                    raise WorkerFastFailError(
                        reason=NackReason.ERR_TASK_FAILED.value,
                        detail=f"Canonical ledger rejected submission {task.submission_ledger_id}",
                    )
                else:
                    await self._enqueue_reconciliation_item(
                        task=task,
                        submission_id=task.submission_ledger_id,
                        detail=f"Canonical settlement timed out for {task.submission_ledger_id}",
                        affected_paths=changed_files,
                    )
                    raise WorkerFastFailError(
                        reason=NackReason.ERR_LEDGER_UNAVAILABLE.value,
                        detail=f"Canonical ledger unavailable for submission {task.submission_ledger_id}",
                    )

            if not await self._register_settlement(message.receipt_handle):
                return False

            self._set_state(task, TaskState.SETTLED_ACK)
            await self.queue.ack(message.receipt_handle)
            self.metrics.tasks_succeeded += 1
            frontier_finish_status = "settled_ack"
            append_trace_span(
                trace_id=task.trace_id,
                service="worker",
                operation="settle_success",
                actor_id=self.worker_id,
                task_id=task.id,
                attempt_id=task.attempt_id,
                attributes={
                    "queue_provider": message.provider,
                    "settlement": "ack",
                    "submission_ledger_id": task.submission_ledger_id,
                },
            )
            return True

        except WorkerFastFailError as exc:
            self._write_barrier_fail_token(task, str(exc))
            print(f"[{self.worker_id}] Task {task.id} failed ({exc.reason}): {exc}")
            task.last_error = str(exc)
            await self._settle_failure(task, message, exc.reason)
            self.metrics.tasks_failed += 1
            frontier_finish_status = "settled_nack"
            frontier_finish_reason = str(exc.reason)
            return False
        except Exception as e:
            self._write_barrier_fail_token(task, str(e))
            detail = self._describe_exception(e, context=f"task {task.id} runtime failure")
            print(f"[{self.worker_id}] Task {task.id} failed: {detail}")
            task.last_error = detail
            await self._settle_failure(task, message, NackReason.ERR_TASK_FAILED)
            self.metrics.tasks_failed += 1
            frontier_finish_status = "settled_nack"
            frontier_finish_reason = NackReason.ERR_TASK_FAILED.value
            return False
        finally:
            heartbeat_stop.set()
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            if frontier_claim_started:
                self._record_frontier_claim_finish(
                    task,
                    status=frontier_finish_status,
                    reason=frontier_finish_reason,
                )
            repo.cleanup()
            self._destroy_workspace_root(workspace_root)
            if single_flight_acquired:
                self._release_task_single_flight(task.id)
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
                    print(f"[{self.worker_id}] Paused, stopping worker loop")
                    self._running = False
                    break
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
