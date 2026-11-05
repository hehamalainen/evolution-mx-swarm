# MX Swarm × Project Aether Bounded Semantic-Merge Canary Reproduction

## Provenance & Run Lineage

### 1. Historical Pre-Public Run (July 21, 2026)

- **Label:** `historical_pre_public_run`
- **Run ID:** `canary-run-20260721t060534z`
- **Historical MX Swarm Commit (pre-public):** `f1a2ea38f21fe8c50f3c19134ce9e2b636a03e00`
- **Historical Runner Reference Blob:** `2eb95c70298b1731f5f7cccd9e712b2c5ee056ae`
- **Historical Project Aether Commit (pre-public):** `af7ebe85087fc815153ed545e406be199cc71f84`
- **Scope Note:** The July 21 run executed on pre-public commits before repository history sanitization for the public research release. It is preserved as historical reference provenance.

### 2. Public Clean-Checkout Reproduction

- **Base Public MX Swarm Commit:** `f12fb568b083f8a28b982d68cd662c38718ab2ea`
- **Pinned Public Project Aether Commit:** `fbc995fa36ed0df29189aa5ebedf161bac78c6df`
- **Database Backend:** SQLite (`database_profile="sqlite"`)
- **Completion Mode:** Deterministic canned completions (`MX_CANNED_COMPLETION_FIXTURE`)
- **Merge Scope:** Bounded Class-1 structural AST merge on `src/loglens/runtime.py` (`class CommandResult` methods `alpha_method` and `beta_method`)

---

## Reporting Separation

Each execution of `scripts/run_aether_semantic_canary.py` reports proposal counts separately from independent runs:

| Metric | Value per Run | Description |
| :--- | :---: | :--- |
| `independent_runs_executed` | `1` | Number of independent end-to-end canary executions |
| `cohort_proposals_count` | `2` | Contending Stage-2 cohort proposals (`task-b`, `task-c`) |
| `control_proposals_count` | `1` | Stage-1 baseline control proposal (`task-a`) |
| `total_run_proposals_count` | `3` | Total proposals under `source_run_id=<run_id>` (`task-a`, `task-b`, `task-c`) |
| `seed_proposals_count` | `9` | Initial repository seed proposals isolated under `source_run_id="seed-run"` |

---

## Fail-Closed Verification Gates

`scripts/run_aether_semantic_canary.py` fails closed (non-zero exit code) if any of the following conditions are violated:

1. **Pinned Aether Commit SHA (`preflight_build_sha_and_flags`):** `/v1/system/build` `commit` must equal `fbc995fa36ed0df29189aa5ebedf161bac78c6df`, `dirty` must be `false`, `database_profile` must be `"sqlite"`, and `semantic_merge_enabled` must be `true`.
2. **Non-Zero & Non-Skipped Checks (`verify_checks_executed`):** Fails if zero checks execute, if any required canary check is missing or skipped, if any expected promotion (`seed_promotion_id`, `control_promotion_id`, `cohort_promotion_id`) is missing or fails detail retrieval, or if any Aether promotion gate (`syntax`, `build`, `unit_tests`, `lint`) is missing, skipped (`SKIP`), fails, or lacks a matching passing `unit_tests` row with `tests_passed > 0` for that promotion's `snapshot_id` in `aether_gate_results`.
3. **Same-Promotion Cohort Membership (`verify_promotion_membership`):** Both contending proposals (`task-b` and `task-c`) must be promoted together in the exact same promotion record (`included_proposals == [prop_b, prop_c]`, `excluded_proposals == []`).
4. **Zero Semantic Fallbacks (`verify_canary_events`):** Both cohort proposals must emit `structural_merge` (`synthesis_engine=None`) and zero `semantic_fallback` events may occur.
5. **Order-Sensitive AST + OS-Sandboxed Materialized Snapshot Behavioral Verification (`verify_snapshot_markers`):** Downloads and materializes the promoted snapshot archive from Aether, parses and validates the Python AST (`ast.parse`) of all snapshot files in-process (`_verify_snapshot_files_and_methods`) to verify package initializers contain zero executable statements, `src/loglens/shared/formatting.py` matches its trusted seed AST, and `src/loglens/runtime.py` matches the trusted seed AST **in exact statement order** (preserving the line-1 module docstring at statement 0 so `__doc__` is intact) plus only the two trailing constant-return methods (`alpha_method` and `beta_method`) on `class CommandResult`, and imports `loglens.runtime` solely from the materialized snapshot inside a macOS Seatbelt-sandboxed (`/usr/bin/sandbox-exec` denying `file-read*`, `file-write*`, `network*`, and `process-fork*` outside the snapshot and Python runtime; full behavioral execution requires macOS) and C-audit-hook-guarded (`sys.addaudithook`) subprocess (`python -I -S -B`) with stdout redirected to `/dev/null` and an authenticated per-invocation nonce on a dedicated result file descriptor to verify `.alpha_method() == "alpha"` and `.beta_method() == "beta"`.
6. **Clean-Checkout & Bundle Release Hygiene (`_is_git_dirty`, `scan_evidence_bundle_dir`):** `--require-clean-mx` fails closed if `git status --porcelain` reports any uncommitted tracked edits or untracked files; the runner rejects non-empty `--output-dir` destinations prior to execution, stages the bundle in a fresh temporary directory, exports only canary-scoped rows (`repo_id` and `run_id` / `"seed-run"`) across allowed Aether SQLite tables (omitting `aether_agent_metrics` so shared agent IDs never retain private scopes), and verifies zero unlisted files, unrelated repositories/tables, local machine paths, or credentials exist.

---

## Clean-Checkout Execution Commands

```bash
# 1. Start pinned Project Aether (fbc995fa36ed0df29189aa5ebedf161bac78c6df) with SQLite
cd /tmp/aether-canary-clean-checkout
export PATH="$(dirname "$(command -v python)"):$PATH"
export PYTHONPATH="/tmp/aether-canary-clean-checkout"
export DATABASE_URL="sqlite:////tmp/aether-canary-run/aether.db"
export AETHER_STORAGE_DIR="/tmp/aether-canary-run/storage"
export AETHER_SERVER_PROFILE="mx_proof"
export AETHER_SEMANTIC_MERGE_ENABLED="true"
export AETHER_SYNTHESIS_WORKER_ENABLED="false"
export AETHER_ALLOW_CUSTOM_GATE_COMMANDS="true"
python -m uvicorn apps.aether_server.main:app --host 127.0.0.1 --port 8105 >/tmp/aether-canary-run/aether_server.log 2>&1 &

# 2. Run bounded Class-1 structural canary from clean MX Swarm checkout
cd /tmp/mx-canary-clean-checkout
python scripts/run_aether_semantic_canary.py \
  --aether-base-url http://127.0.0.1:8105 \
  --expected-aether-sha fbc995fa36ed0df29189aa5ebedf161bac78c6df \
  --structural-only \
  --require-clean-mx \
  --aether-db-path /tmp/aether-canary-run/aether.db \
  --output-dir /tmp/mx-aether-canary-evidence
```

---

## Recorded Canary Attempts & Review History

1. **Attempt 1 (`canary-run-20261101t225905z`, `2026-11-01T22:59:05Z`):**
   - **Failed Check:** `preflight_build_sha_and_flags`
   - **Error:** `Preflight failed: Unable to reach /v1/system/build: {'error': '<urlopen error [Errno 61] Connection refused>'}`
   - **Root Cause:** Invoking `uvicorn aether.api.main:app` with `PYTHONPATH=$AETHER_CLEAN/packages` caused dual SQLAlchemy table registration (`aether.models.entities` and `packages.aether.models.entities`).
   - **Resolution:** Switched server startup to `PYTHONPATH=$AETHER_CLEAN uvicorn apps.aether_server.main:app`.
2. **Attempt 2 (`canary-run-20261101t230006z`, `2026-11-01T23:00:06Z`, `repo_4261942d92bb`, `snap_1f40b8f2fc9f`):**
   - **Failed Check:** `seed_promotion` (`lint` gate on `snap_1f40b8f2fc9f`)
   - **Error:** `Promotion rejected: gates did not pass` (`E501 Line too long (96 > 88)` on `src/loglens/cli.py:25` and `90 > 88` on `src/loglens/runtime.py:39`).
   - **Root Cause:** Snapshot materialization writes only the 5 seeded `src/loglens/*.py` files into the temporary gate workspace without `pyproject.toml`, so `ruff check --select F,E src` defaulted to 88-char line length instead of 100 chars.
   - **Resolution:** Added `--line-length 100` to the `lint` gate command (`ruff check --select F,E --line-length 100 src`).
3. **Attempt 3 (`canary-run-20261101t230318z`, `2026-11-01T23:03:18Z`, `repo_45606854fb00`):**
   - **Failed Stage:** `worker_initialization` (after `preflight_build_sha_and_flags` and `seed_promotion` passed)
   - **Error:** `AttributeError: 'str' object has no attribute 'get'` in `create_queue_adapter("memory")`
   - **Root Cause:** `create_queue_adapter` and `create_secrets_adapter` expect config dicts (`config.queue.to_dict()`, `config.secrets.to_dict()`) rather than provider strings.
   - **Resolution:** Passed `config.queue.to_dict()` and `config.secrets.to_dict()` to the adapter factories.
4. **Attempt 4 (`canary-run-20261101t230430z`, `2026-11-01T23:04:30Z`, `repo_476a1265abca`):**
   - **Failed Stage:** `worker_initialization`
   - **Error:** `ValueError: Unknown LLM provider: mock` in `create_llm_client(config.llm.to_dict())`
   - **Root Cause:** `config.llm.provider` was set to `"mock"`, which is not a registered provider in `create_llm_client` (deterministic completions use `MX_CANNED_COMPLETION_FIXTURE`).
   - **Resolution:** Retained default `MXConfig().llm` settings while deterministic completions bypass LLM calls via `MX_CANNED_COMPLETION_FIXTURE`.
5. **Attempt 5 (`canary-run-20261101t230533z`, `2026-11-01T23:05:33Z`, `repo_7d970a1ca0b5`):**
   - **Failed Stage:** `stage1_queue_publish`
   - **Error:** `TypeError: InMemoryQueue.publish() got an unexpected keyword argument 'attributes'`
   - **Root Cause:** `InMemoryQueue.publish` accepts `(task, priority=...)` rather than SQS-style `attributes={...}`.
   - **Resolution:** Updated `queue.publish` calls to `await queue.publish(task, priority=task.priority)` and aligned `/cohort/evaluate` and `/promotions` response-field extraction with `CohortEvaluateResponse`.
6. **Attempt 6 (`canary-run-20261101t230741z`, `2026-11-01T23:07:41Z`, `repo_c0e6c585681e`):**
   - **Failed Check:** `cohort_evaluation_and_promotion`
   - **Error:** `Cohort evaluation returned success=False` with `SUPERSEDED_SEMANTIC_CONFLICT` (`SEMANTIC_FALLBACK: NON_WHITELISTED_MODIFICATION`)
   - **Root Cause:** `task_b_content` and `task_c_content` placed the module docstring `"""Shared command result and rendering helpers."""` on line 7 (after `from loglens.shared import formatting`) instead of line 1 as in `qualification/seed_repos/loglens/src/loglens/runtime.py`, causing both diffs to touch top-level module statements outside `class CommandResult` and triggering Aether's fail-closed `NON_WHITELISTED_MODIFICATION` guard.
   - **Resolution:** Restored the module docstring to line 1 in `task_b_content` and `task_c_content` so both proposals modify only `class CommandResult`.
7. **Attempt 7 (`canary-run-20261101t231017z`, `2026-11-01T23:10:17Z`, `repo_e8b691cff260`):**
   - **Failed Check:** `cohort_evaluation_and_promotion`
   - **Error:** `Cohort evaluation returned success=False` with `SUPERSEDED_SEMANTIC_CONFLICT` (`SEMANTIC_FALLBACK: NON_WHITELISTED_MODIFICATION`)
   - **Root Cause:** Worker post-implementation formatting runs `python -m ruff format src/loglens/runtime.py`. Because `run_dir` did not include `pyproject.toml` with `[tool.ruff] line-length = 100`, `ruff format` defaulted to 88 chars and wrapped line 39 (`raise NotImplementedError(...)`, 90 chars) inside `render_result` in both `task-b` and `task-c`, causing both proposals to modify `FUNC:render_result`.
   - **Resolution:** Added `[tool.ruff] line-length = 100` to `qualification/seed_repos/loglens/pyproject.toml` and copied `pyproject.toml` into `run_dir` so `ruff format` preserves 100-char lines.
8. **Attempt 8 (`canary-run-20261101t231254z`, `2026-11-01T23:12:54Z`, `repo_4ab615a4689b`, `prom_b9c908b5cb9c`, `snap_d37d7786cb2d`):**
   - **Failed Check:** `aether_promotion_gates_non_skipped` (after cohort promotion, AST check, and runtime behavioral execution all passed)
   - **Error:** `Promotion prom_b9c908b5cb9c gate 'merged_paths' had status '["src/loglens/runtime.py"]'`
   - **Root Cause:** Cohort promotions record gate statuses in `_replay_gate_vector` (`build:PASS|lint:PASS|syntax:PASS|unit_tests:PASS`) while storing `merged_paths` as a non-underscore metadata key in `evidence`. `verify_checks_executed` treated `merged_paths` as a gate name.
   - **Resolution:** Updated `verify_checks_executed` to exclude `merged_paths` and parse `_replay_gate_vector` entries when verifying promotion gate statuses.
9. **Attempt 9 (`canary-run-20261101t231444z`, `2026-11-01T23:14:44Z`, `repo_1af26739886b`, `prom_d34e534c8c52`, `snap_e4c85cab1af6`):**
   - **Failed Stage:** `sqlite_evidence_export` (after all 10 canary verification checks passed)
   - **Error:** `sqlite3.OperationalError: no such column: c.created_at` in `_dump_sqlite_evidence`
   - **Root Cause:** `_dump_sqlite_evidence` ordered `aether_proposal_contentions` by `c.created_at`, `aether_system_metric_events` by `timestamp`, and `aether_gate_results` by `executed_at` instead of `p.submitted_at`, `created_at`, and `started_at`.
   - **Resolution:** Aligned `_dump_sqlite_evidence` `ORDER BY` clauses with Aether's SQLite table schemas.
10. **Attempt 10 (`canary-run-20261101t231602z`, `2026-11-01T23:16:02Z`, reviewed MX commit `b7d5187566ed50b0bf2a6cf1d6711fccbb645751`, `repo_3e8878f153e2`, `prom_ef2f9fb28612`, `snap_882eb797a265`):**
   - **Failed Stage:** `independent_codex_security_and_gate_review`
   - **Findings:** Independent review reproduced the bounded Class-1 structural merge and byte-identical `merged_runtime.py` (`SHA-256: 7c53f332f80d653a60bfa017986495f50cf1c88992812368fe9db208e750cadc`), and identified 4 issues: (1) live worker snapshot download path used `_extract_tar_bytes_safely`, which accepted `.git` entries; (2) `inspect_merged_runtime_ast_and_behavior` executed the downloaded module in-process via `exec()`; (3) `verify_checks_executed` did not require all 4 configured gates (`syntax`, `build`, `unit_tests`, `lint`) and all 3 expected promotion IDs, `unit_tests` ran `compileall` (`tests_passed=null`), and worker check counts used static list lengths; (4) raw `aether.db` and unlisted `aether_server.log` in the output bundle contained local machine paths.
   - **Resolution:** Unified live snapshot extraction onto `extract_snapshot_securely` with an empty staging directory and `.git`/alias rejection; replaced in-process `exec()` with AST safety checks plus an isolated, credential-free subprocess (`python -I -B`); required all 4 gates and all 3 promotion IDs, ran real unit assertions in `unit_tests` (`tests_passed=2`), and counted recorded worker check results; scrubbed and vacuumed exported `aether.db`, kept server logs outside the bundle, and added automated bundle hygiene scanning.
11. **Attempt 11 (`canary-run-20261102t072605z`, `2026-11-02T07:26:05Z`, `repo_1dd2e9b3c80d`, `prom_90294f50029c`, `snap_3dea61fe7e0a`):**
   - **Failed Check:** `promoted_snapshot_ast_and_behavior`
   - **Error:** `AttributeError: 'NoneType' object has no attribute '__dict__'` inside `dataclasses._is_type` in isolated subprocess.
   - **Root Cause:** In Python 3.14, `@dataclass` on a module with `from __future__ import annotations` looks up `sys.modules.get(cls.__module__).__dict__`, and `_isolated_merged_runtime` was not registered in `sys.modules`.
   - **Resolution:** Pre-imported `dataclasses` and registered `sys.modules[mod.__name__] = mod` in `_run_isolated_snapshot_behavior_check`.
12. **Attempt 12 (`canary-run-20261102t072907z`, `2026-11-02T07:29:07Z`, reviewed MX commit `b62d9818ebd2741d246c44f8ddb5dcf14885cc62`, `repo_355e0d53fc0b`, `prom_341a1dd27997`, `snap_0e864c97e9b8`):**
   - **Failed Stage:** `independent_codex_round2_review`
   - **Findings:** Independent review identified 4 P1 release blockers and 4 P2 regressions: (1) `_io.FileIO` inside `alpha_method` could write outside the verifier because Python-level monkeypatching did not confine C-level file descriptors; (2) the 5-file seed omitted `src/loglens/shared/formatting.py` while the verifier imported it from the MX checkout (`sys.path.insert(0, str(seed_src))`), causing standalone snapshot imports of `loglens.runtime` to fail with `ModuleNotFoundError: No module named 'loglens.shared'`; (3) post-run cleanup deleted unlisted files/subdirectories inside `--output-dir`; (4) `_sanitize_and_export_sqlite_db` copied all tables and rows from `aether.db` instead of scoping to the canary `repo_id` and `run_id`; (5) `materialize_aether_base_head` overlaid snapshot files without removing obsolete files absent from the snapshot; (6) `GitRepo.scrub_workspace_credentials()` deleted tracked `.gitattributes` and `.gitmodules` during ordinary `GitRepo.add()` calls; (7) `verify_checks_executed` accepted a single `unit_tests` row in `aether_gate_results` across all three promotions instead of matching each promotion's `snapshot_id`; and (8) `--aether-db-path` was optional, allowing gate-row verification to be skipped or an old database to be reused.
   - **Resolution:** Enforced macOS Seatbelt (`/usr/bin/sandbox-exec` denying `file-write*`, `network*`, and `process-fork*`), C-level `sys.addaudithook`, and an explicit AST import allowlist; seeded all 9 `loglens` package files (including `src/loglens/shared/formatting.py` and `src/loglens/shared/__init__.py`) and materialized the promoted snapshot archive as the sole import root; rejected non-empty `--output-dir` before running and staged bundle outputs in a fresh temporary directory; required `--aether-db-path` and pruned exported SQLite tables/rows to the canary `repo_id` and `run_id`; cleaned destination worktrees in `extract_snapshot_securely` and verified exact worktree and `git ls-files` tree equality in `materialize_aether_base_head`; preserved tracked `.gitattributes` and `.gitmodules` in `GitRepo` while restoring them around untrusted hooks; and matched every expected promotion's `snapshot_id` to a passing `unit_tests` row with `tests_passed > 0`.
13. **Attempt 13 (`canary-run-20261102t134837z`, `2026-11-02T13:48:37Z`, `repo_30e0a2afbb07`, `snap_13b8b25ca25e`):**
   - **Failed Check:** `seed_promotion` (`unit_tests` gate on `snap_13b8b25ca25e`)
   - **Error:** `TypeError: render_result() got an unexpected keyword argument 'as_json'`
   - **Root Cause:** When expanding the `unit_tests` promotion gate to exercise `loglens.runtime.render_result` (which imports `loglens.shared.formatting`), the assertion passed `as_json=False` instead of `output_format='plain'`.
   - **Resolution:** Updated the `unit_tests` promotion gate command to call `render_result(r, output_format='plain') == 'ok'`.
14. **Attempt 14 (`canary-run-20261102t135316z`, `2026-11-02T13:53:16Z`, `repo_0d4662366159`, `prom_b4713f81d372`, `snap_f4a8cf0116e2`):**
   - **Failed Check:** `promoted_snapshot_ast_and_behavior`
   - **Error:** `Forbidden import 'argparse' in snapshot module`
   - **Root Cause:** `src/loglens/runtime.py` imports `argparse` for `add_output_format_option(parser: argparse.ArgumentParser)`, and `_ALLOWED_SNAPSHOT_AST_IMPORTS` omitted `'argparse'`.
   - **Resolution:** Added `'argparse'` to `_ALLOWED_SNAPSHOT_AST_IMPORTS` and pre-imported `argparse` in the isolated verifier.
15. **Attempt 15 (`canary-run-20261102t135628z`, `2026-11-02T13:56:28Z`, `repo_a783b75ca849`, `prom_b86bd725e7da`, `snap_ee0479c3fdab`):**
   - **Failed Check:** `promoted_snapshot_ast_and_behavior`
   - **Error:** `Forbidden call to 'getattr' in snapshot module`
   - **Root Cause:** `src/loglens/runtime.py` calls `getattr(formatting, f"render_{output_format}", None)` in `render_result`, and `_FORBIDDEN_SNAPSHOT_AST_CALLS` included `'getattr'`.
   - **Resolution:** Removed `'getattr'` from `_FORBIDDEN_SNAPSHOT_AST_CALLS` while keeping OS Seatbelt and C-level `sys.addaudithook` confinement active.
16. **Attempt 16 (`canary-run-20261102t135803z`, `2026-11-02T13:58:03Z`, reviewed MX commit `e9ce3f9f76427bdf9b519aa597f76211053e7aaa`, `repo_2f0cc52e5921`, `prom_a1e3e86f30e2`, `snap_a61da4cf7068`):**
   - **Failed Stage:** `independent_codex_round3_review`
   - **Findings:** Independent review confirmed 6 of 8 Round 2 findings were closed and the 9-file cohort snapshot materialized cleanly and returned `alpha` and `beta`, while identifying 3 P1 blockers and 3 P2 issues: (1) Seatbelt allowed `file-read*` by default, permitting a snapshot dependency using `sqlite3` (which bypasses Python's `open` audit event) to read an external SQLite database, and the non-Darwin code path fell back to unsandboxed execution; (2) a snapshot could forge `behavior_verified=true` by having `src/loglens/__init__.py` write the expected JSON to stdout and exit `0` while `alpha_method` raised; (3) `aether_agent_metrics` has primary key `(agent_id, scope)`, so filtering by `agent_id` retained private `scope` rows for `worker-1`; (4) `extract_snapshot_securely` resolved `extract_dir` before testing `is_symlink()` and defaulted `clean_destination=True`; (5) `materialize_aether_base_head` ran `git ls-files` without `-z`, quoting UTF-8 filenames like `café.py`; and (6) `verify_promotion_membership` did not check `excluded_proposals == []`.
   - **Resolution:** Restricted `file-read*` at the OS Seatbelt boundary (`(deny file-read* (subpath "/"))` with explicit allowlist for system paths, Python base prefix, and snapshot root), blocked `sqlite3.*` in `sys.addaudithook`, and failed closed when `/usr/bin/sandbox-exec` is unavailable; verified all snapshot files and constant-return method AST bodies in the parent (`_verify_snapshot_files_and_methods`) and redirected child fd `1` / `sys.stdout` to `/dev/null` before importing `loglens.runtime` while authenticating the result on a duplicated descriptor (`out_fd`) with a per-invocation secret `nonce`; omitted `aether_agent_metrics` from `ALLOWED_AETHER_DB_TABLES` in both export and hygiene scanning; checked `raw_extract_path.is_symlink()` before `.resolve()`, defaulted `clean_destination=False`, and restricted `clean_destination=True` to worker-owned directories; switched `materialize_aether_base_head` to `git ls-files -z`; and required `excluded_proposals == []` in `verify_promotion_membership`.
17. **Attempt 17 (`canary-run-20261102t192516z`, `2026-11-02T19:25:16Z`, `repo_1442b2e38022`, `prom_c9f86a1ff296`, `snap_040bd1ca727f`):**
   - **Failed Check:** `promoted_snapshot_ast_and_behavior`
   - **Error:** `src/loglens/cli.py: Disallowed top-level AST statement in snapshot module: If`
   - **Root Cause:** `_verify_snapshot_files_and_methods` applied `_validate_snapshot_ast_safety` (which forbids top-level `if` statements) to the unmodified seed file `src/loglens/cli.py`, which contains `if __name__ == "__main__": raise SystemExit(main())`.
   - **Resolution:** Verified unmodified seed files (`pyproject.toml`, `src/loglens/cli.py`, `src/loglens/registry.py`) and Stage-1 `src/loglens/parsing.py` directly against `qualification/seed_repos/loglens` while enforcing `_validate_snapshot_ast_safety` and strict AST body rules on all package initializers and the `loglens.runtime` import chain (`src/loglens/shared/formatting.py`, `src/loglens/runtime.py`).
18. **Attempt 18 (`canary-run-20261102t192804z`, `2026-11-02T19:28:04Z`, `repo_355bf770446b`, `prom_0c1e9ee15f53`, `snap_c599242ca230`):**
   - **Failed Stage:** `sqlite_evidence_export` (after all 10 canary verification checks passed)
   - **Error:** `Unable to export scoped Aether SQLite database: no such column: winning_proposal_id`
   - **Root Cause:** `_sanitize_and_export_sqlite_db` referenced `winning_proposal_id` instead of `winner_proposal_id` when pruning `aether_contention_winners`.
   - **Resolution:** Checked `PRAGMA table_info("aether_contention_winners")` and filtered on `winner_proposal_id`.
19. **Attempt 19 (`canary-run-20261102t192936z`, `2026-11-02T19:29:36Z`, reviewed MX commit `61d3192935977e33d5473e56627fadef165fe38b`, `repo_5a397f4a4213`, `prom_726fc615f8bf`, `snap_e9c450d318f6`):**
   - **Failed Stage:** `independent_codex_round4_review`
   - **Findings:** Independent review confirmed 5 of 6 Round 3 findings were closed and 10/10 clean-checkout checks and 290/290 tests passed, and reproduced 1 remaining P1 blocker: a synthetic `runtime.py` declared `alpha_method` and `beta_method`, then reassigned `CommandResult = None`, accessed `getattr(argparse, "_sys").modules["__main__"].__dict__` to read `payload["nonce"]` and `out_fd`, emitted a forged result, and exited `0` before either method ran.
   - **Resolution:** Compared the entire `src/loglens/runtime.py` AST against the trusted `qualification/seed_repos/loglens/src/loglens/runtime.py` seed plus the two permitted constant-return methods (`alpha_method` and `beta_method`), compared `src/loglens/shared/formatting.py` directly against its trusted seed AST, rejected `getattr`/`hasattr` calls with underscore-prefixed attribute names in `_validate_snapshot_ast_safety`, executed `_verify_snapshot_files_and_methods` before spawning the child subprocess in both `inspect_merged_runtime_ast_and_behavior` and `_run_isolated_snapshot_behavior_check`, and cleared `sys.modules["__main__"].__dict__` inside a local closure before importing `loglens.runtime`.
20. **Attempt 20 (`canary-run-20261102t203713z`, `2026-11-02T20:37:27Z`, platform `macOS Darwin` + independent Linux replay, reviewed MX commit `03283fb3a56476e220da921a56e2174cfb16f6a1` against Aether `bc924b8b3b3d653ec01f2cdbcfc1332012fc29c6`, `repo_bab0316eb617`, `prom_30094d0c3176`, `snap_ef2f5c1335aa`, `merged_runtime.py` SHA-256 `7c53f332f80d653a60bfa017986495f50cf1c88992812368fe9db208e750cadc`):**
   - **Stage:** `independent_cross_platform_round5_review`
   - **Findings:** Both the macOS clean-checkout run (`canary-run-20261102t203713z`) and an independent fresh-clone Linux replay reproduced single-promotion cohort membership (`included_proposals == [prop_b, prop_c]`, `excluded_proposals == []`), 2 `structural_merge` events, 0 `semantic_fallback` events, passing promotion gates (`tests_passed=2`), and byte-identical `merged_runtime.py` (`7c53f332…`). Cross-platform review identified: (1) two macOS `sandbox-exec` behavioral unit tests failed on Linux (`ubuntu-latest` CI) instead of skipping on non-Darwin while keeping AST-level tests cross-platform; (2) this section's previous heading claimed a final pass before an evidence tag was published; (3) in `7c53f332…`, Project Aether `bc924b8`'s structural merge moved `runtime.py`'s module docstring from line 1 to line 8 after the imports (where Python sets `__doc__ = None`), and MX's `_strip_runtime_docstring_and_expected_methods` hid that displacement by stripping matching docstrings from any AST body index; plus minor fixes for `test_dashboard_security` working-directory isolation, `--require-clean-mx` untracked-file detection, and gitignoring `*.egg-info/`.
   - **Resolution:** Made `runtime.py` AST comparison strictly order-sensitive (`_strip_expected_methods_from_class` + `ast.get_docstring` equality, rejecting non-initial top-level string expressions), marked the two macOS Seatbelt behavioral tests `skipif(sys.platform != "darwin")` while keeping AST checks running on all platforms, enabled untracked-file detection in `_is_git_dirty`, isolated the browser dashboard test working directory, and gitignored `*.egg-info/`. Against pre-fix Project Aether, `scripts/run_aether_semantic_canary.py` intentionally failed closed at `promoted_snapshot_ast_and_behavior` on the displaced line-8 docstring until Project Aether landed its docstring-preservation merge fix in `fbc995fa36ed0df29189aa5ebedf161bac78c6df`.
21. **Attempt 21 (`canary-run-20261104t204030z`, `2026-11-04T20:40:30Z` – `2026-11-04T20:40:44Z`, platform `macOS 26.7.2 (Darwin 25.6.0 arm64, Python 3.14.3)`, evaluated MX commit `ce5219de1c6a8147cdb54fcab18dd59af0b761b1` against pinned Project Aether `fbc995fa36ed0df29189aa5ebedf161bac78c6df`, `repo_bf072a1a19c9`, seed `prom_afe3bcca0bee` / `snap_f700ea4d07d2`, Stage-1 control `prop_ac01ff7c6472` -> `prom_05141e31f79d` / `snap_3090017348a2` (`head_ed54ff35c167`), Stage-2 cohort `['prop_cdd470194100', 'prop_276dffed13dc']` -> `prom_2886cc0f390f` / `snap_e9e12c25c9e9`, `merged_runtime.py` SHA-256 `7fc9cc753995fe11b4188872430ea2c63e998f0be4b5e86ddcf9e7687fef088b`):**
   - **Stage:** `public_clean_checkout_reproduction`
   - **Outcome:** `PASS` (`exit_code=0`, `10/10` canary verification checks passed, `0` skipped). Executed from fresh GitHub clones into empty `/tmp` directories with `--require-clean-mx`, `--expected-aether-sha fbc995fa36ed0df29189aa5ebedf161bac78c6df`, `--structural-only`, required `--aether-db-path`, and an empty `--output-dir`. Both contending Stage-2 cohort proposals landed together in `prom_2886cc0f390f` (`included_proposals == ["prop_276dffed13dc", "prop_cdd470194100"]`, `excluded_proposals == []`), emitting `2` `structural_merge` events and `0` `semantic_fallback` events under `source_run_id="canary-run-20261104t204030z"`. All four Aether promotion gates (`syntax`, `build`, `unit_tests`, `lint`) recorded `PASS` across all three promotions with `tests_passed=2` on each promotion's `unit_tests` row. Order-sensitive AST verification confirmed `merged_runtime.py` (`7fc9cc753995fe11b4188872430ea2c63e998f0be4b5e86ddcf9e7687fef088b`) preserved the module docstring `"Shared command result and rendering helpers."` at statement `0` alongside `CommandResult.alpha_method` and `CommandResult.beta_method`, and the macOS Seatbelt-sandboxed behavioral check returned `{"alpha_method": "alpha", "beta_method": "beta"}`.

---

## Current Gate Status

- **Status:** Recorded clean-checkout pass (`canary-run-20261104t204030z`, `10/10` checks `PASS`, `0` skipped, exit code `0`), replacing the prior intentional fail-closed status once Project Aether pin `fbc995fa36ed0df29189aa5ebedf161bac78c6df` preserved the line-1 module docstring during structural merge.
- **Evaluated Commits:** MX Swarm `ce5219de1c6a8147cdb54fcab18dd59af0b761b1` × Project Aether `fbc995fa36ed0df29189aa5ebedf161bac78c6df` (`merged_runtime.py` SHA-256 `7fc9cc753995fe11b4188872430ea2c63e998f0be4b5e86ddcf9e7687fef088b`).
- **What Was Executed:**
  - Fresh `git clone` checkouts of both public repositories into empty `/tmp` directories with `--require-clean-mx` enforced.
  - Deterministic frozen completions (`MX_CANNED_COMPLETION_FIXTURE`), SQLite backend (`database_profile="sqlite"`), `server_profile="mx_proof"`, `semantic_merge_enabled=true`, and `synthesis_worker_enabled=false`.
  - `1` independent end-to-end run comprising `9` isolated seed proposals (`prom_afe3bcca0bee` / `snap_f700ea4d07d2`), `1` Stage-1 control proposal (`prop_ac01ff7c6472` -> `prom_05141e31f79d` / `snap_3090017348a2`), and `2` contending Stage-2 cohort proposals (`prop_cdd470194100`, `prop_276dffed13dc`) promoted atomically in `prom_2886cc0f390f` (`snap_e9e12c25c9e9`, `excluded_proposals == []`) with `2` `structural_merge` events and `0` `semantic_fallback` events.
  - All four Aether promotion gates (`syntax`, `build`, `unit_tests`, `lint`) executed and passed across all three promotions (`tests_passed=2` per promotion), plus `6/6` recorded worker task acceptance checks.
  - Order-sensitive Python AST verification against the trusted `loglens` seed and macOS Seatbelt (`/usr/bin/sandbox-exec`) + C-audit-hook (`sys.addaudithook`) isolated behavioral execution returning `"alpha"` and `"beta"`.
  - Automated bundle hygiene verification (`scripts/check_release_hygiene.py --scan-bundle-dir /tmp/mx-aether-canary-evidence` -> `{"ok": true, "findings": []}`).
- **What Was Not Executed:**
  - Live LLM completions (deterministic frozen completion fixtures were used).
  - PostgreSQL database profile (SQLite was used).
  - LLM synthesis fallback merges (`synthesis_worker_enabled=false`; bounded Class-1 structural AST merge only).
  - Linux OS-sandboxed behavioral execution (behavioral snapshot confinement relies on macOS `/usr/bin/sandbox-exec`; on Linux CI, AST-level checks run while the two macOS Seatbelt behavioral unit tests skip).

