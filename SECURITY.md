# Security Policy & Trusted-Code Boundary

## Status Warning

MX Swarm is an **experimental research prototype** and is **not certified for untrusted multi-tenant production deployment**.

## Private Vulnerability Reporting

Please **do not** report security vulnerabilities or exposed credentials in public GitHub issues or pull requests.

- **Preferred Private Channel:** Use [GitHub Private Vulnerability Reporting (Security Advisories)](https://github.com/hehamalainen/evolution-mx-swarm/security/advisories/new) on the repository.
- Include:
  1. Affected commit SHA and file/line references
  2. Minimal reproduction steps or proof-of-concept
  3. Impact assessment (credential exposure, path containment bypass, remote control surface, etc.)

## Trusted-Code Execution Boundary

MX Swarm workers execute LLM-generated code and acceptance commands (`pytest`, `ruff`, etc.) in local subprocesses. Operators must understand what is enforced in-process versus what requires external sandboxing:

### Enforced by MX Swarm (`worker/service.py` & `dashboard/api.py`)
1. **Canonical Workspace Path Containment:**
   - Worker file reads, writes, overlays, and local landing materialization strip quote/backtick wrappers before validation, reject `.git` and traversal segments, reject any symlink component along the path (`os.path.islink`), and verify that `os.path.realpath(target)` stays strictly inside `os.path.realpath(workspace_root)`.
2. **Sanitized Acceptance Command Environment & Ephemeral Git Authentication:**
   - Acceptance commands (`_run_command`) run with a sanitized environment (`_build_sanitized_command_env`) that strips host API keys and tokens (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `GITHUB_TOKEN`, `AETHER_TOKEN`, `MX_DASHBOARD_TOKEN`, etc.) and isolates `HOME` and `TMPDIR` inside the workspace.
   - `GitRepo` (`shared/utils/git.py`) never embeds `GITHUB_TOKEN` in repository URLs, command-line arguments, or `<workspace>/.git/config`. Credentials are confined to trusted remote Git operations (`clone`, `fetch`, `pull`, `push`), where authentication material is temporarily present in the Git subprocess environment (including any inherited host environment variables and ephemeral `GIT_CONFIG_VALUE_*` Basic-auth headers) and a temporary `0o600` token file inside a `0o700` `GIT_ASKPASS` helper directory that is removed in `finally:`. Out-of-workspace `.git` snapshot creation and restoration (`snapshot_trusted_git_dir` / `restore_trusted_git_dir`) surround acceptance checks (`_run_acceptance_checks`), while ordinary `GitRepo` operations perform metadata scrubbing (`scrub_workspace_credentials`, clearing `.git/hooks` and rewriting `.git/config` to a canonical minimal config) and invoke Git with `--no-verify`, an empty external `core.hooksPath`, and `core.fsmonitor`/`core.sshCommand`/`credential.helper`/`protocol.ext.allow` disabled.
3. **Process-Group Termination on Timeout:**
   - Acceptance commands launch in a dedicated session/process group (`start_new_session=True`). On timeout or cancellation, the worker sends `SIGTERM` followed by `SIGKILL` to the entire process group (`os.killpg`) and reaps surviving descendants even if the direct parent shell exited early or a child ignores `SIGTERM`.
4. **Loopback-Default Dashboard, API & WebSocket Protection:**
   - The FastAPI dashboard binds to `127.0.0.1` by default (`MX_DASHBOARD_HOST=127.0.0.1`). Binding to a non-loopback interface fails closed unless `MX_DASHBOARD_TOKEN` is set. All `/api/*` endpoints (both reads and mutations) and `/ws` WebSocket connections reject cross-origin requests from non-loopback `Origin` headers, reject unauthenticated non-loopback or proxied requests, and require a valid `MX_DASHBOARD_TOKEN` when configured.

### Not Enforced by Python Alone (Requires OS/Container Sandboxing)
- **Arbitrary Code Execution Inside Acceptance Checks:** When a worker runs `pytest` on LLM-generated Python files, the generated code executes with the OS privileges of the worker process. While environment variables are sanitized and Worker file APIs enforce workspace containment, arbitrary Python code inside a test process can still make syscalls permitted by the OS user.
- **Recommended Isolation for Untrusted Tasks:** Run workers inside ephemeral containers (`Dockerfile.worker`) or VMs with:
  - Read-only root filesystems outside the workspace mount
  - Outbound network egress restricted to required endpoints
  - Non-root user execution and per-container resource limits
