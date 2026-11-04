#!/usr/bin/env python3
"""
Bounded Semantic Canary Runner for MX Swarm + Project Aether.

Executes a 2-worker contention scenario against a live Aether server backed by SQLite:
- Seeds a fresh repository with 5 files from qualification/seed_repos/loglens (under source_run_id="seed-run")
- Task A: Control change on src/loglens/parsing.py (promoted in Stage 1)
- Task B: Contending change 1 on src/loglens/runtime.py (adds CommandResult.alpha_method)
- Task C: Contending change 2 on src/loglens/runtime.py (adds CommandResult.beta_method)

Verifies fail-closed preflight build SHA pinning, non-zero and non-skipped check execution,
atomic single-promotion cohort membership, zero semantic fallbacks, and both Python AST
and runtime behavioral verification of the promoted merged snapshot.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import copy
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess  # nosec B404
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Ensure project root is in path
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from shared.adapters import (  # noqa: E402
    create_aether_vcs,
    create_queue_adapter,
    create_secrets_adapter,
)
from shared.schemas import (  # noqa: E402
    AcceptanceCriteria,
    MergePolicy,
    RiskLevel,
    RiskTier,
    TaskConstraints,
    TaskContext,
    TaskContract,
    TaskStream,
    TaskType,
)
from shared.utils.config import MXConfig  # noqa: E402
from worker.service import Worker  # noqa: E402

PINNED_AETHER_COMMIT = "fbc995fa36ed0df29189aa5ebedf161bac78c6df"

HISTORICAL_JULY21_PROVENANCE = {
    "label": "historical_pre_public_run",
    "run_id": "canary-run-20260721t060534z",
    "recorded_at_utc": "2026-07-21T06:06:06Z",
    "mx_commit": "f1a2ea38f21fe8c50f3c19134ce9e2b636a03e00",
    "aether_commit": "af7ebe85087fc815153ed545e406be199cc71f84",
    "repo_id": "repo_bec2fd40c5a5",
    "cohort_promotion_id": "prom_4b36293d9881",
    "cohort_snapshot_id": "snap_885a4d118150",
    "note": (
        "July 21, 2026 semantic-merge canary executed on pre-public commits prior to "
        "public repository history sanitization. Preserved as historical reference provenance."
    ),
}

RECORDED_FAILED_ATTEMPTS: list[dict[str, Any]] = [
    {
        "attempt_number": 1,
        "run_id": "canary-run-20261101t225905z",
        "timestamp_utc": "2026-11-01T22:59:05Z",
        "stage": "preflight_build_sha_and_flags",
        "outcome": "FAIL_CLOSED",
        "error": "Preflight failed: Unable to reach /v1/system/build: {'error': '<urlopen error [Errno 61] Connection refused>'}",
        "root_cause": (
            "Aether server was initially invoked as `uvicorn aether.api.main:app` with "
            "`PYTHONPATH=$AETHER_CLEAN/packages`, causing dual SQLAlchemy table registration "
            "between `aether.models.entities` and `packages.aether.models.entities`."
        ),
        "resolution": "Switched server invocation to canonical `PYTHONPATH=$AETHER_CLEAN uvicorn apps.aether_server.main:app`.",
    },
    {
        "attempt_number": 2,
        "run_id": "canary-run-20261101t230006z",
        "timestamp_utc": "2026-11-01T23:00:06Z",
        "repo_id": "repo_4261942d92bb",
        "snapshot_id": "snap_1f40b8f2fc9f",
        "stage": "seed_promotion",
        "outcome": "FAIL_CLOSED",
        "error": (
            "Promotion rejected: gates did not pass (gate `lint` failed with exit_code=1: "
            "E501 Line too long 96 > 88 in src/loglens/cli.py:25 and 90 > 88 in src/loglens/runtime.py:39)"
        ),
        "root_cause": (
            "Aether materializes only the 5 seeded `src/loglens/*.py` files into the temporary "
            "gate workspace without `pyproject.toml`, so `ruff check --select F,E src` defaulted "
            "to 88-char line length instead of the repository's 100-char line length."
        ),
        "resolution": "Passed explicit `--line-length 100` to the `lint` gate (`ruff check --select F,E --line-length 100 src`).",
    },
    {
        "attempt_number": 3,
        "run_id": "canary-run-20261101t230318z",
        "timestamp_utc": "2026-11-01T23:03:18Z",
        "repo_id": "repo_45606854fb00",
        "stage": "worker_initialization",
        "outcome": "FAIL_CLOSED",
        "error": "AttributeError: 'str' object has no attribute 'get' in create_queue_adapter('memory')",
        "root_cause": (
            "`create_queue_adapter` and `create_secrets_adapter` expect a configuration dict "
            "(`config.queue.to_dict()`, `config.secrets.to_dict()`) rather than a provider string."
        ),
        "resolution": "Passed `config.queue.to_dict()` and `config.secrets.to_dict()` to adapter factories.",
    },
    {
        "attempt_number": 4,
        "run_id": "canary-run-20261101t230430z",
        "timestamp_utc": "2026-11-01T23:04:30Z",
        "repo_id": "repo_476a1265abca",
        "stage": "worker_initialization",
        "outcome": "FAIL_CLOSED",
        "error": "ValueError: Unknown LLM provider: mock in create_llm_client(config.llm.to_dict())",
        "root_cause": (
            "`config.llm.provider` was overridden to `'mock'`, which is not a registered provider "
            "in `create_llm_client` (deterministic completions use `MX_CANNED_COMPLETION_FIXTURE`)."
        ),
        "resolution": "Kept default `MXConfig().llm` configuration while deterministic completions run via `MX_CANNED_COMPLETION_FIXTURE`.",
    },
    {
        "attempt_number": 5,
        "run_id": "canary-run-20261101t230533z",
        "timestamp_utc": "2026-11-01T23:05:33Z",
        "repo_id": "repo_7d970a1ca0b5",
        "stage": "stage1_queue_publish",
        "outcome": "FAIL_CLOSED",
        "error": "TypeError: InMemoryQueue.publish() got an unexpected keyword argument 'attributes'",
        "root_cause": (
            "`InMemoryQueue.publish` accepts `(task, priority=...)` rather than SQS-style "
            "`attributes={...}`."
        ),
        "resolution": (
            "Updated `queue.publish` calls to `await queue.publish(task, priority=task.priority)` "
            "and aligned `/cohort/evaluate` and `/promotions` response-field extraction with "
            "`CohortEvaluateResponse` (`success`, `promotion_id`, `proposal_outcomes`)."
        ),
    },
    {
        "attempt_number": 6,
        "run_id": "canary-run-20261101t230741z",
        "timestamp_utc": "2026-11-01T23:07:41Z",
        "repo_id": "repo_c0e6c585681e",
        "stage": "cohort_evaluation_and_promotion",
        "outcome": "FAIL_CLOSED",
        "error": (
            "Cohort evaluation returned success=False with proposal_outcomes "
            "SUPERSEDED_SEMANTIC_CONFLICT (SEMANTIC_FALLBACK: NON_WHITELISTED_MODIFICATION)"
        ),
        "root_cause": (
            "`task_b_content` and `task_c_content` placed the module docstring "
            "`\"\"\"Shared command result and rendering helpers.\"\"\"` after `from loglens.shared import formatting` "
            "instead of line 1 as in `qualification/seed_repos/loglens/src/loglens/runtime.py`, causing both "
            "diffs to touch top-level module statements outside `class CommandResult`."
        ),
        "resolution": (
            "Restored the module docstring to line 1 in `task_b_content` and `task_c_content` so both "
            "proposals modify only `class CommandResult`."
        ),
    },
    {
        "attempt_number": 7,
        "run_id": "canary-run-20261101t231017z",
        "timestamp_utc": "2026-11-01T23:10:17Z",
        "repo_id": "repo_e8b691cff260",
        "stage": "cohort_evaluation_and_promotion",
        "outcome": "FAIL_CLOSED",
        "error": (
            "Cohort evaluation returned success=False with proposal_outcomes "
            "SUPERSEDED_SEMANTIC_CONFLICT (SEMANTIC_FALLBACK: NON_WHITELISTED_MODIFICATION)"
        ),
        "root_cause": (
            "Worker post-implementation formatting runs `python -m ruff format src/loglens/runtime.py`. "
            "Because `run_dir` did not include `pyproject.toml` with `[tool.ruff] line-length = 100`, "
            "`ruff format` defaulted to 88 chars and wrapped line 39 inside `render_result` in both "
            "`task-b` and `task-c`, causing both proposals to modify `FUNC:render_result`."
        ),
        "resolution": (
            "Added `[tool.ruff] line-length = 100` to `qualification/seed_repos/loglens/pyproject.toml` "
            "and copied `pyproject.toml` into `run_dir` so `ruff format` preserves 100-char lines."
        ),
    },
    {
        "attempt_number": 8,
        "run_id": "canary-run-20261101t231254z",
        "timestamp_utc": "2026-11-01T23:12:54Z",
        "repo_id": "repo_4ab615a4689b",
        "promotion_id": "prom_b9c908b5cb9c",
        "snapshot_id": "snap_d37d7786cb2d",
        "stage": "aether_promotion_gates_non_skipped",
        "outcome": "FAIL_CLOSED",
        "error": (
            "Promotion prom_b9c908b5cb9c gate 'merged_paths' had status '[\"src/loglens/runtime.py\"]'"
        ),
        "root_cause": (
            "Cohort promotions succeed and record gate outcomes in `_replay_gate_vector` "
            "(`build:PASS|lint:PASS|syntax:PASS|unit_tests:PASS`) while storing `merged_paths` "
            "as a non-underscore metadata key in `evidence`. `verify_checks_executed` treated "
            "`merged_paths` as a gate name."
        ),
        "resolution": (
            "Updated `verify_checks_executed` to exclude `merged_paths` and parse `_replay_gate_vector` "
            "entries when verifying promotion gate statuses."
        ),
    },
    {
        "attempt_number": 9,
        "run_id": "canary-run-20261101t231444z",
        "timestamp_utc": "2026-11-01T23:14:44Z",
        "repo_id": "repo_1af26739886b",
        "promotion_id": "prom_d34e534c8c52",
        "snapshot_id": "snap_e4c85cab1af6",
        "stage": "sqlite_evidence_export",
        "outcome": "FAIL_CLOSED",
        "error": "sqlite3.OperationalError: no such column: c.created_at in _dump_sqlite_evidence",
        "root_cause": (
            "All 10 canary verification checks passed, but `_dump_sqlite_evidence` ordered "
            "`aether_proposal_contentions` by `c.created_at`, `aether_system_metric_events` by "
            "`timestamp`, and `aether_gate_results` by `executed_at` instead of `p.submitted_at`, "
            "`created_at`, and `started_at`."
        ),
        "resolution": (
            "Aligned `_dump_sqlite_evidence` ORDER BY clauses with Aether's SQLite table schemas."
        ),
    },
    {
        "attempt_number": 10,
        "run_id": "canary-run-20261101t231602z",
        "timestamp_utc": "2026-11-01T23:16:02Z",
        "reviewed_mx_commit": "b7d5187566ed50b0bf2a6cf1d6711fccbb645751",
        "repo_id": "repo_3e8878f153e2",
        "promotion_id": "prom_ef2f9fb28612",
        "snapshot_id": "snap_882eb797a265",
        "stage": "independent_codex_security_and_gate_review",
        "outcome": "NOT_READY_REMEDIATED",
        "error": (
            "Independent review reproduced the bounded Class-1 structural merge and byte-identical "
            "merged_runtime.py (SHA-256 7c53f332f80d653a60bfa017986495f50cf1c88992812368fe9db208e750cadc), "
            "but identified 4 findings: (1) live worker snapshot path used _extract_tar_bytes_safely "
            "which accepted .git entries; (2) inspect_merged_runtime_ast_and_behavior used in-process "
            "exec(); (3) verify_checks_executed did not enforce all 4 required gates/promotion IDs, "
            "unit_tests ran compileall with tests_passed=null, and worker check counts used static list "
            "lengths; (4) raw aether.db and unlisted aether_server.log in the output bundle contained "
            "local machine paths."
        ),
        "root_cause": (
            "Separate helper for live tar extraction; in-process exec() in snapshot verifier; "
            "non-exhaustive gate set check and sys.executable paths in gate_configs; raw SQLite copy "
            "without path scrubbing/VACUUM and server log written inside output_dir."
        ),
        "resolution": (
            "Unified live snapshot extraction onto extract_snapshot_securely with staging directory "
            "and .git/alias rejection; replaced in-process exec() with strict AST validation plus "
            "isolated credential-free subprocess verification; enforced all 4 required gates and "
            "expected promotion IDs, switched unit_tests gate to execute real unit assertions with "
            "tests_passed=2, and counted recorded worker check executions; scrubbed and vacuumed "
            "exported aether.db, removed unlisted files, and added automated bundle hygiene scanning."
        ),
    },
    {
        "attempt_number": 11,
        "run_id": "canary-run-20261102t072605z",
        "timestamp_utc": "2026-11-02T07:26:05Z",
        "repo_id": "repo_1dd2e9b3c80d",
        "promotion_id": "prom_90294f50029c",
        "snapshot_id": "snap_3dea61fe7e0a",
        "stage": "promoted_snapshot_ast_and_behavior",
        "outcome": "FAIL_CLOSED",
        "error": (
            "AttributeError: 'NoneType' object has no attribute '__dict__' inside dataclasses._is_type "
            "when executing <merged_runtime.py> in isolated subprocess"
        ),
        "root_cause": (
            "In Python 3.14, @dataclass on a module with `from __future__ import annotations` inspects "
            "`sys.modules.get(cls.__module__).__dict__`. The isolated subprocess created "
            "`mod = types.ModuleType('_isolated_merged_runtime')` without registering "
            "`sys.modules[mod.__name__] = mod`."
        ),
        "resolution": (
            "Pre-imported `dataclasses` before locking down file/socket/subprocess APIs and registered "
            "`sys.modules[mod.__name__] = mod` in `_run_isolated_snapshot_behavior_check`."
        ),
    },
    {
        "attempt_number": 12,
        "run_id": "canary-run-20261102t072907z",
        "timestamp_utc": "2026-11-02T07:29:07Z",
        "reviewed_mx_commit": "b62d9818ebd2741d246c44f8ddb5dcf14885cc62",
        "repo_id": "repo_2df075c94451",
        "promotion_id": "prom_1108749e195e",
        "snapshot_id": "snap_5669732dded8",
        "stage": "independent_codex_round2_review",
        "outcome": "NOT_READY_REMEDIATED",
        "error": (
            "Independent review replayed canary-run-20261102t072907z and identified 4 P1 and 4 P2 findings: "
            "(P1-1) _io.FileIO inside a snapshot method could write outside the disposable directory; "
            "(P1-2) behavior verification put the MX seed checkout on sys.path while the promoted snapshot "
            "omitted loglens/shared/formatting.py; "
            "(P1-3) --output-dir cleanup deleted subdirectories and unlisted files; "
            "(P1-4) SQLite exporter copied unrelated repositories/tables when present; "
            "(P2-1) materialize_aether_base_head retained pre-existing files absent from the snapshot; "
            "(P2-2) GitRepo.scrub_workspace_credentials removed tracked .gitattributes and .gitmodules on routine Git ops; "
            "(P2-3) verify_checks_executed accepted a single unit_tests row across 3 promotions; "
            "(P2-4) omitting --aether-db-path could skip gate-row verification and reuse an old output DB."
        ),
        "root_cause": (
            "Python-level monkeypatching without OS kernel sandboxing + C audit hook; 5-file seed list "
            "omitting loglens/shared/formatting.py and sys.path fallback to MX seed repo; post-run "
            "directory pruning instead of pre-run empty destination check + staging dir; unpruned SQLite "
            "table copy; non-cleaning snapshot extraction; .gitattributes/.gitmodules in routine git scrub; "
            "unmatched snapshot_id in unit_tests gate verification."
        ),
        "resolution": (
            "Enforced OS Seatbelt sandbox (/usr/bin/sandbox-exec denying file-write*, network*, and process-fork) "
            "plus C-level sys.addaudithook and strict AST import allowlist; seeded all 9 loglens package files "
            "including loglens/shared/formatting.py and executed behavior verification solely from the materialized "
            "snapshot; rejected non-empty --output-dir before execution and built bundle in a fresh staging dir; "
            "pruned SQLite export to only (repo_id, run_id, 'seed-run') across allowed Aether tables; cleaned "
            "worktree and verified git ls-files on base head materialization; preserved tracked .gitattributes and "
            ".gitmodules on routine Git ops; matched every promotion's snapshot_id to a passing unit_tests row."
        ),
    },
    {
        "attempt_number": 13,
        "run_id": "canary-run-20261102t134837z",
        "timestamp_utc": "2026-11-02T13:48:37Z",
        "repo_id": "repo_30e0a2afbb07",
        "snapshot_id": "snap_13b8b25ca25e",
        "stage": "seed_promotion",
        "outcome": "FAIL_CLOSED",
        "error": (
            "Promotion rejected: gates did not pass (unit_tests gate failed with "
            "TypeError: render_result() got an unexpected keyword argument 'as_json')"
        ),
        "root_cause": (
            "When expanding the unit_tests promotion gate to exercise `loglens.runtime.render_result` "
            "(which imports `loglens.shared.formatting`), the assertion passed `as_json=False` instead "
            "of the keyword argument `output_format='plain'` defined in `src/loglens/runtime.py`."
        ),
        "resolution": (
            "Updated the `unit_tests` promotion gate command to call `render_result(r, output_format='plain') == 'ok'`."
        ),
    },
    {
        "attempt_number": 14,
        "run_id": "canary-run-20261102t135316z",
        "timestamp_utc": "2026-11-02T13:53:16Z",
        "repo_id": "repo_0d4662366159",
        "promotion_id": "prom_b4713f81d372",
        "snapshot_id": "snap_f4a8cf0116e2",
        "stage": "promoted_snapshot_ast_and_behavior",
        "outcome": "FAIL_CLOSED",
        "error": "Forbidden import 'argparse' in snapshot module",
        "root_cause": (
            "`src/loglens/runtime.py` imports `argparse` for `add_output_format_option(parser: argparse.ArgumentParser)`, "
            "and `_ALLOWED_SNAPSHOT_AST_IMPORTS` did not include `'argparse'`."
        ),
        "resolution": (
            "Added `'argparse'` to `_ALLOWED_SNAPSHOT_AST_IMPORTS` and pre-imported `argparse` in the isolated verifier."
        ),
    },
    {
        "attempt_number": 15,
        "run_id": "canary-run-20261102t135628z",
        "timestamp_utc": "2026-11-02T13:56:28Z",
        "repo_id": "repo_a783b75ca849",
        "promotion_id": "prom_b86bd725e7da",
        "snapshot_id": "snap_ee0479c3fdab",
        "stage": "promoted_snapshot_ast_and_behavior",
        "outcome": "FAIL_CLOSED",
        "error": "Forbidden call to 'getattr' in snapshot module",
        "root_cause": (
            "`src/loglens/runtime.py` calls `getattr(formatting, f'render_{output_format}', None)` in "
            "`render_result`, and `_FORBIDDEN_SNAPSHOT_AST_CALLS` included `'getattr'`."
        ),
        "resolution": (
            "Removed `'getattr'` from `_FORBIDDEN_SNAPSHOT_AST_CALLS` while keeping OS Seatbelt and "
            "C-level `sys.addaudithook` confinement active."
        ),
    },
    {
        "attempt_number": 16,
        "run_id": "canary-run-20261102t135803z",
        "timestamp_utc": "2026-11-02T13:58:03Z",
        "reviewed_mx_commit": "e9ce3f9f76427bdf9b519aa597f76211053e7aaa",
        "repo_id": "repo_cd03e2b2360b",
        "promotion_id": "prom_99dbc1be8c74",
        "snapshot_id": "snap_4bcb0157c685",
        "stage": "independent_codex_round3_review",
        "outcome": "NOT_READY_REMEDIATED",
        "error": (
            "Independent review closed 6 of 8 previous findings and identified 3 P1 and 3 P2 findings: "
            "(P1-1) Seatbelt allowed reads by default (`sqlite3.connect` on an external DB succeeded) "
            "and non-Darwin had an unsandboxed fallback path; "
            "(P1-2) a snapshot package initializer could print forged JSON to stdout and exit 0 while "
            "`alpha_method` raised; "
            "(P1-3) `aether_agent_metrics` filtered by `agent_id` rather than `(agent_id, scope)`, "
            "retaining unrelated scopes for shared agents; "
            "(P2-1) `extract_snapshot_securely` resolved `extract_dir` before testing `is_symlink()`; "
            "(P2-2) `git ls-files` without `-z` quoted UTF-8 filenames such as `café.py`; "
            "(P2-3) `verify_promotion_membership` did not verify `excluded_proposals == []`."
        ),
        "root_cause": (
            "Default-allow `file-read*` in Seatbelt profile and non-Darwin fallback command path; "
            "lack of trusted parent verification of all snapshot files against the seed plus the two "
            "permitted method additions and unguarded child fd 1 during module import; cross-scope "
            "`aether_agent_metrics` table included in public DB export; `Path(extract_dir).resolve()` "
            "called prior to `is_symlink()`; `git ls-files` called without `-z`; `excluded_proposals` "
            "not checked in `verify_promotion_membership`."
        ),
        "resolution": (
            "Enforced default-deny `file-read*` and `file-write*` in Seatbelt (allowing reads only on "
            "system paths, `sys.base_prefix`, `sys.executable`, and the materialized snapshot root), "
            "blocked `sqlite3.*` audit events, and failed closed when `/usr/bin/sandbox-exec` is unavailable; "
            "verified all snapshot files against the `loglens` seed plus the two permitted constant-return "
            "method additions in the trusted parent and isolated child stdout behind a duplicated fd and "
            "per-run nonce; omitted `aether_agent_metrics` from the public DB export and hygiene allowlist; "
            "validated `raw_extract_path.is_symlink()` before resolution and restricted `clean_destination` "
            "to worker-owned directories; switched `materialize_aether_base_head` to `git ls-files -z`; "
            "and verified `excluded_proposals == []` in `verify_promotion_membership`."
        ),
    },
    {
        "attempt_number": 17,
        "run_id": "canary-run-20261102t192516z",
        "timestamp_utc": "2026-11-02T19:25:16Z",
        "repo_id": "repo_1442b2e38022",
        "promotion_id": "prom_c9f86a1ff296",
        "snapshot_id": "snap_040bd1ca727f",
        "failed_stage": "promoted_snapshot_ast_and_behavior",
        "failed_check": "promoted_snapshot_ast_and_behavior",
        "error": "src/loglens/cli.py: Disallowed top-level AST statement in snapshot module: If",
        "root_cause": (
            "`_verify_snapshot_files_and_methods` applied `_validate_snapshot_ast_safety` (designed for "
            "`runtime.py` and its import chain) to the unmodified seed file `src/loglens/cli.py`, which "
            "contains `if __name__ == \"__main__\": raise SystemExit(main())`."
        ),
        "resolution": (
            "Verified unmodified seed files (`pyproject.toml`, `src/loglens/cli.py`, `src/loglens/registry.py`) "
            "and Stage-1 `src/loglens/parsing.py` directly against `qualification/seed_repos/loglens` while "
            "enforcing `_validate_snapshot_ast_safety` and strict AST body rules on all package initializers "
            "and the `loglens.runtime` import chain (`src/loglens/shared/formatting.py`, `src/loglens/runtime.py`)."
        ),
    },
    {
        "attempt_number": 18,
        "run_id": "canary-run-20261102t192804z",
        "timestamp_utc": "2026-11-02T19:28:04Z",
        "repo_id": "repo_355bf770446b",
        "promotion_id": "prom_0c1e9ee15f53",
        "snapshot_id": "snap_c599242ca230",
        "failed_stage": "sqlite_evidence_export",
        "failed_check": None,
        "error": "Unable to export scoped Aether SQLite database: no such column: winning_proposal_id",
        "root_cause": (
            "`_sanitize_and_export_sqlite_db` queried `winning_proposal_id` instead of `winner_proposal_id` "
            "when pruning `aether_contention_winners`."
        ),
        "resolution": (
            "Updated `_sanitize_and_export_sqlite_db` to check `PRAGMA table_info(\"aether_contention_winners\")` "
            "and filter on `winner_proposal_id`."
        ),
    },
    {
        "attempt_number": 19,
        "run_id": "canary-run-20261102t192936z",
        "timestamp_utc": "2026-11-02T19:29:36Z",
        "reviewed_mx_commit": "61d3192935977e33d5473e56627fadef165fe38b",
        "repo_id": "repo_5a397f4a4213",
        "promotion_id": "prom_726fc615f8bf",
        "snapshot_id": "snap_e9c450d318f6",
        "failed_stage": "independent_codex_round4_review",
        "failed_check": "promoted_snapshot_ast_and_behavior",
        "error": (
            "Round 4 review confirmed 5 of 6 Round 3 findings were closed and 10/10 clean-checkout checks "
            "and 290/290 tests passed, and reproduced 1 remaining P1 blocker: a synthetic runtime.py "
            "declared alpha_method and beta_method, then reassigned CommandResult = None, accessed "
            "getattr(argparse, '_sys').modules['__main__'].__dict__ to read payload['nonce'] and out_fd, "
            "wrote a forged result, and exited 0 before either method ran."
        ),
        "root_cause": (
            "`_verify_snapshot_files_and_methods` validated `alpha_method` and `beta_method` bodies on the "
            "first `CommandResult` ClassDef without comparing the full `runtime.py` AST against the trusted "
            "`loglens` seed, and `_run_isolated_snapshot_behavior_check` ran `_verify_snapshot_files_and_methods` "
            "after the child subprocess while leaving `payload`, `out_fd`, `os`, and `json` in `__main__.__dict__`."
        ),
        "resolution": (
            "Compared the entire `runtime.py` AST against the trusted `qualification/seed_repos/loglens/src/loglens/runtime.py` "
            "seed plus the two permitted constant-return methods (`alpha_method` and `beta_method`), compared "
            "`src/loglens/shared/formatting.py` directly against its trusted seed AST, rejected `getattr`/`hasattr` "
            "with underscore-prefixed attribute names in `_validate_snapshot_ast_safety`, executed "
            "`_verify_snapshot_files_and_methods` before spawning the child subprocess in both "
            "`inspect_merged_runtime_ast_and_behavior` and `_run_isolated_snapshot_behavior_check`, and cleared "
            "`__main__.__dict__` inside a local closure before importing `loglens.runtime`."
        ),
    },
    {
        "attempt_number": 20,
        "run_id": "canary-run-20261102t203713z",
        "timestamp_utc": "2026-11-02T20:37:27Z",
        "reviewed_mx_commit": "03283fb3a56476e220da921a56e2174cfb16f6a1",
        "repo_id": "repo_bab0316eb617",
        "promotion_id": "prom_30094d0c3176",
        "snapshot_id": "snap_ef2f5c1335aa",
        "failed_stage": "independent_cross_platform_round5_review",
        "failed_check": "promoted_snapshot_ast_and_behavior",
        "error": (
            "Independent Linux replay confirmed atomic cohort promotion, zero semantic fallbacks, and "
            "byte-identical merged_runtime.py (SHA-256 7c53f332f80d653a60bfa017986495f50cf1c88992812368fe9db208e750cadc), "
            "and identified: (1) two macOS sandbox-exec canary tests failed on Linux CI instead of skipping "
            "while keeping AST checks cross-platform; (2) reproduction doc section title claimed an unpublished "
            "final pass; (3) Aether bc924b8's structural merge moved runtime.py's module docstring from line 1 "
            "to line 8 after imports (__doc__ = None), and MX's _strip_runtime_docstring_and_expected_methods "
            "masked that bug by stripping matching docstrings from any position; plus minor fixes for "
            "dashboard browser test isolation, --require-clean-mx untracked files, and *.egg-info gitignore."
        ),
        "root_cause": (
            "Position-insensitive docstring stripping in `_strip_runtime_docstring_and_expected_methods` "
            "and non-initial string-expression allowance in `_validate_snapshot_ast_safety`; missing "
            "`skipif(sys.platform != 'darwin')` on macOS Seatbelt unit tests; `--untracked-files=no` in "
            "`_is_git_dirty`; and shared `repo_root` cwd in the real-browser dashboard test."
        ),
        "resolution": (
            "Made `runtime.py` AST comparison strictly order-sensitive (`_strip_expected_methods_from_class` "
            "and `ast.get_docstring` equality, rejecting displaced docstrings so the canary intentionally "
            "fails closed against Aether `bc924b8` until Aether fixes docstring preservation); marked the "
            "two macOS `sandbox-exec` behavioral tests `skipif` on non-Darwin while running AST tests on all "
            "platforms; checked untracked files in `_is_git_dirty`; isolated the dashboard browser test "
            "working directory; gitignored `*.egg-info/`; and updated the reproduction doc history."
        ),
    },
]

REQUIRED_CANARY_CHECKS = (
    "preflight_build_sha_and_flags",
    "seed_promotion",
    "stage1_control_promotion",
    "worker_task_acceptance_checks",
    "cohort_evaluation_and_promotion",
    "cohort_proposal_count_check",
    "same_promotion_membership",
    "zero_semantic_fallback_events",
    "promoted_snapshot_ast_and_behavior",
    "aether_promotion_gates_non_skipped",
)

REQUIRED_PROMOTION_GATES: tuple[str, ...] = (
    "syntax",
    "build",
    "unit_tests",
    "lint",
)

PUBLIC_BUNDLE_ARTIFACTS: tuple[str, ...] = (
    "merged_runtime.py",
    "final_canary_evidence.json",
    "manifest.json",
    "telemetry.log",
    "aether.db",
)


def _assert_http_url(url: str) -> None:
    if urllib.parse.urlparse(url).scheme not in {"http", "https"}:
        raise ValueError(f"Unsupported URL scheme in {url!r}")


def _http_request(url: str, method: str = "GET", data: dict | None = None, timeout: int = 15) -> dict[str, Any]:
    """Helper for synchronous HTTP JSON requests."""
    _assert_http_url(url)
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("AETHER_API_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req_data = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=req_data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw_err = e.read().decode("utf-8")
        try:
            err_json = json.loads(raw_err)
        except Exception:
            err_json = {"detail": raw_err}
        return {"error": f"HTTP {e.code}", "status_code": e.code, "detail": err_json}
    except Exception as e:
        return {"error": str(e)}


def _get_git_head_commit(repo_dir: Path) -> str:
    """Fetch current git HEAD commit hash."""
    res = subprocess.run(  # nosec B603 B607
        ["git", "rev-parse", "HEAD"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def _get_git_branch(repo_dir: Path) -> str:
    """Fetch current git branch name."""
    res = subprocess.run(  # nosec B603 B607
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def _is_git_dirty(repo_dir: Path) -> bool:
    """Check whether a git working tree has uncommitted changes or untracked files."""
    res = subprocess.run(  # nosec B603 B607
        ["git", "status", "--porcelain"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    return bool(res.stdout.strip())


async def verify_preflight_remediation(
    args: Any,
    build_info_out: dict[str, Any] | None = None,
) -> int:
    """MX-1: Verify preflight server flags, SQLite profile, and exact pinned Aether commit SHA."""
    base_url = getattr(args, "aether_base_url", "http://localhost:8100").rstrip("/")
    build_info = _http_request(f"{base_url}/v1/system/build")
    if "error" in build_info:
        print(f"Preflight failed: Unable to reach /v1/system/build: {build_info}")
        return 1

    if build_info_out is not None:
        build_info_out.update(build_info)

    branch = build_info.get("branch")
    commit = build_info.get("commit")
    if not branch or branch == "unknown" or not commit or commit == "unknown":
        print(f"Preflight failed: Build metadata branch/commit is unknown: {build_info}")
        return 1

    if build_info.get("dirty") is True:
        print(f"Preflight failed: Aether server checkout is dirty: {build_info}")
        return 1

    expected_sha = getattr(args, "expected_aether_sha", None)
    if isinstance(expected_sha, str) and expected_sha.strip():
        if commit != expected_sha.strip():
            print(
                f"Preflight failed: Running Aether build SHA {commit!r} differs from "
                f"pinned SHA {expected_sha.strip()!r}"
            )
            return 1

    require_sqlite = getattr(args, "require_sqlite", False)
    if require_sqlite is True and build_info.get("database_profile") != "sqlite":
        print(
            f"Preflight failed: Expected database_profile='sqlite', "
            f"got {build_info.get('database_profile')!r}"
        )
        return 1

    if not build_info.get("semantic_merge_enabled"):
        print("Preflight failed: semantic_merge_enabled must be True")
        return 1

    structural_only = getattr(args, "structural_only", False)
    if structural_only is False and not build_info.get("synthesis_worker_enabled"):
        print("Preflight failed: synthesis_worker_enabled must be True when not structural_only")
        return 1

    return 0


async def verify_canary_events(
    args: Any,
    repo_id: str,
    run_id: str,
    expected_proposals: list[str],
    structural_only: bool,
    events_out: list[dict[str, Any]] | None = None,
) -> int:
    """MX-2, MX-3, MX-7, MX-8, MX-9, MX-10: Verify telemetry events for the canary run."""
    base_url = getattr(args, "aether_base_url", "http://localhost:8100").rstrip("/")
    res = _http_request(f"{base_url}/v1/repos/{repo_id}/telemetry/events?source_run_id={run_id}")
    if "error" in res:
        print(f"Telemetry query failed: {res}")
        return 1

    events = res.get("events", [])
    if events_out is not None:
        events_out.extend(events)

    for ev in events:
        if ev.get("event_type") == "semantic_fallback":
            print(f"FAILED: Found semantic_fallback event: {ev}")
            return 1

    by_prop: dict[str, list[dict[str, Any]]] = {}
    for ev in events:
        pid = ev.get("proposal_id")
        if pid:
            by_prop.setdefault(pid, []).append(ev)

    for pid in expected_proposals:
        p_events = by_prop.get(pid, [])
        merge_events = [
            e for e in p_events if e.get("event_type") in ("structural_merge", "synthesis_merge")
        ]
        if not merge_events:
            print(f"FAILED: Missing merge event for expected proposal {pid}")
            return 1

        types = {e.get("event_type") for e in merge_events}
        if len(types) > 1:
            print(f"FAILED: Contradictory merge events for proposal {pid}: {types}")
            return 1

        for e in merge_events:
            etype = e.get("event_type")
            engine = e.get("synthesis_engine")
            if etype == "structural_merge":
                if engine is not None:
                    print(f"FAILED: structural_merge must have synthesis_engine=None, got {engine}")
                    return 1
            elif etype == "synthesis_merge":
                if structural_only:
                    print(f"FAILED: synthesis_merge occurred during structural_only mode for {pid}")
                    return 1
                if engine != "deterministic_simulation":
                    print(
                        f"FAILED: synthesis_merge must have "
                        f"synthesis_engine='deterministic_simulation', got {engine}"
                    )
                    return 1

    return 0


async def verify_promotion_membership(
    args: Any,
    repo_id: str,
    promo_id: str,
    expected_proposals: list[str],
    promo_out: dict[str, Any] | None = None,
) -> int:
    """MX-4: Verify promotion record cohort membership matches expected proposals."""
    base_url = getattr(args, "aether_base_url", "http://localhost:8100").rstrip("/")
    res = _http_request(f"{base_url}/v1/repos/{repo_id}/promotions/{promo_id}")
    if "error" in res:
        print(f"Failed to fetch promotion {promo_id}: {res}")
        return 1

    if promo_out is not None:
        promo_out.update(res)

    inc_raw = res.get("included_proposals", "[]")
    try:
        included = json.loads(inc_raw) if isinstance(inc_raw, str) else inc_raw
    except Exception as exc:
        print(f"FAILED: Promotion {promo_id} invalid included_proposals: {exc}")
        return 1
    if not isinstance(included, list):
        print(f"FAILED: Promotion {promo_id} included_proposals is not a list: {included!r}")
        return 1

    exc_raw = res.get("excluded_proposals", "[]")
    try:
        excluded = (
            json.loads(exc_raw)
            if isinstance(exc_raw, str)
            else ([] if exc_raw is None else exc_raw)
        )
    except Exception as exc:
        print(f"FAILED: Promotion {promo_id} invalid excluded_proposals: {exc}")
        return 1
    if not isinstance(excluded, list) or len(excluded) != 0:
        print(f"FAILED: Promotion {promo_id} excluded_proposals must be empty, got {excluded!r}")
        return 1

    if len(expected_proposals) < 2:
        print(f"FAILED: Cohort must contain at least 2 expected proposals, got {expected_proposals}")
        return 1

    if set(included) != set(expected_proposals) or len(included) != len(expected_proposals):
        print(f"FAILED: Promotion {promo_id} included_proposals {included} != expected {expected_proposals}")
        return 1

    return 0


_ALLOWED_SNAPSHOT_AST_IMPORTS: frozenset[str] = frozenset(
    {
        "__future__",
        "argparse",
        "dataclasses",
        "typing",
        "collections",
        "enum",
        "loglens",
    }
)

_FORBIDDEN_SNAPSHOT_AST_IMPORTS: frozenset[str] = frozenset(
    {
        "os",
        "sys",
        "io",
        "_io",
        "posix",
        "nt",
        "subprocess",
        "socket",
        "shutil",
        "pathlib",
        "urllib",
        "http",
        "builtins",
        "importlib",
        "ctypes",
        "pty",
        "fcntl",
        "signal",
        "tempfile",
        "codecs",
        "marshal",
        "pickle",
        "sqlite3",
        "tarfile",
        "zipfile",
    }
)

_FORBIDDEN_SNAPSHOT_AST_CALLS: frozenset[str] = frozenset(
    {
        "open",
        "FileIO",
        "BufferedWriter",
        "BufferedRandom",
        "TextIOWrapper",
        "exec",
        "eval",
        "compile",
        "__import__",
        "setattr",
        "delattr",
        "globals",
        "locals",
        "vars",
        "input",
        "breakpoint",
        "system",
        "popen",
        "spawn",
        "fork",
        "unlink",
        "remove",
        "rmdir",
        "mkdir",
        "makedirs",
        "write_text",
        "write_bytes",
    }
)


def _validate_snapshot_ast_safety(tree: ast.Module) -> str | None:
    """Reject top-level executable statements, displaced docstrings, non-allowlisted imports, private attributes, or dangerous calls."""
    for idx, stmt in enumerate(tree.body):
        if isinstance(stmt, ast.Expr):
            if idx == 0 and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                continue
            return (
                f"Disallowed top-level expression statement at index {idx} in snapshot module: "
                f"{type(stmt.value).__name__}"
            )
        if not isinstance(
            stmt,
            (
                ast.Import,
                ast.ImportFrom,
                ast.Assign,
                ast.AnnAssign,
                ast.ClassDef,
                ast.FunctionDef,
                ast.AsyncFunctionDef,
            ),
        ):
            return f"Disallowed top-level AST statement in snapshot module: {type(stmt).__name__}"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_mod = alias.name.split(".")[0]
                if (
                    root_mod.startswith("_")
                    or root_mod in _FORBIDDEN_SNAPSHOT_AST_IMPORTS
                    or root_mod not in _ALLOWED_SNAPSHOT_AST_IMPORTS
                ):
                    return f"Forbidden import {alias.name!r} in snapshot module"
        elif isinstance(node, ast.ImportFrom):
            mod_name = node.module or ""
            root_mod = mod_name.split(".")[0]
            if (
                not root_mod
                or (root_mod.startswith("_") and root_mod != "__future__")
                or root_mod in _FORBIDDEN_SNAPSHOT_AST_IMPORTS
                or root_mod not in _ALLOWED_SNAPSHOT_AST_IMPORTS
            ):
                return f"Forbidden import from {node.module!r} in snapshot module"
            for alias in node.names:
                if alias.name.startswith("_") or alias.name in _FORBIDDEN_SNAPSHOT_AST_CALLS:
                    return f"Forbidden imported symbol {alias.name!r} in snapshot module"
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                return f"Forbidden private/dunder attribute access {node.attr!r} in snapshot module"
        elif isinstance(node, ast.Name):
            if node.id.startswith("__"):
                return f"Forbidden dunder identifier {node.id!r} in snapshot module"
        elif isinstance(node, ast.Call):
            func_name: str | None = None
            if isinstance(node.func, ast.Name):
                func_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                func_name = node.func.attr
            if func_name in _FORBIDDEN_SNAPSHOT_AST_CALLS:
                return f"Forbidden call to {func_name!r} in snapshot module"
            if func_name in {"getattr", "hasattr"} and len(node.args) >= 2:
                attr_arg = node.args[1]
                if isinstance(attr_arg, ast.Constant) and isinstance(attr_arg.value, str):
                    if attr_arg.value.startswith("_"):
                        return (
                            f"Forbidden private/dunder attribute access {attr_arg.value!r} "
                            f"via {func_name} in snapshot module"
                        )
    return None


_ALLOWED_LOGLENS_SNAPSHOT_PATHS: frozenset[str] = frozenset(
    {
        "pyproject.toml",
        "src/loglens/__init__.py",
        "src/loglens/cli.py",
        "src/loglens/commands/__init__.py",
        "src/loglens/parsing.py",
        "src/loglens/registry.py",
        "src/loglens/runtime.py",
        "src/loglens/shared/__init__.py",
        "src/loglens/shared/formatting.py",
    }
)

_LOGLENS_PACKAGE_INIT_PATHS: frozenset[str] = frozenset(
    {
        "src/loglens/__init__.py",
        "src/loglens/commands/__init__.py",
        "src/loglens/shared/__init__.py",
    }
)

_EXPECTED_METHOD_RETURN_VALUES: dict[str, str] = {
    "alpha_method": "alpha",
    "beta_method": "beta",
}


def _strip_expected_methods_from_class(
    tree: ast.Module,
    *,
    class_name: str,
    expected_methods: tuple[str, ...],
) -> tuple[ast.Module | None, str | None]:
    """Return an order-preserving copy of runtime.py AST with only expected_methods removed from class_name."""
    cloned = copy.deepcopy(tree)
    class_seen = 0
    for stmt in cloned.body:
        if isinstance(stmt, ast.ClassDef) and stmt.name == class_name:
            class_seen += 1
            seen_expected: list[str] = []
            trailing_phase = False
            kept_body: list[ast.stmt] = []
            for sub in stmt.body:
                if isinstance(sub, ast.FunctionDef) and sub.name in expected_methods:
                    trailing_phase = True
                    if sub.name in seen_expected:
                        return None, f"Duplicate method {sub.name!r} in {class_name!r}"
                    seen_expected.append(sub.name)
                else:
                    if trailing_phase and expected_methods:
                        return (
                            None,
                            f"Unexpected class statement {type(sub).__name__} after expected methods in {class_name!r}",
                        )
                    kept_body.append(sub)
            if set(seen_expected) != set(expected_methods):
                missing = sorted(set(expected_methods) - set(seen_expected))
                return None, f"Missing expected methods {missing} in {class_name!r}"
            stmt.body = kept_body
    if class_seen != 1:
        return None, f"Expected exactly 1 ClassDef {class_name!r}, found {class_seen}"
    return cloned, None


def _verify_snapshot_files_and_methods(
    content: str,
    snapshot_files: dict[str, str] | None,
    *,
    class_name: str = "CommandResult",
    expected_methods: tuple[str, str] = ("alpha_method", "beta_method"),
) -> str | None:
    """
    Trusted parent verification of all snapshot files and the full runtime.py AST:
    - All snapshot file paths must belong to the known loglens seed file set.
    - Every .py file in the import chain must pass _validate_snapshot_ast_safety.
    - Package __init__.py files must contain zero executable statements (optional docstring only).
    - src/loglens/shared/formatting.py must match its trusted seed AST in qualification/seed_repos/loglens.
    - src/loglens/runtime.py must match the entire trusted seed AST in qualification/seed_repos/loglens
      plus the two permitted constant-return methods on CommandResult.
    """
    files_to_check: dict[str, str] = dict(snapshot_files) if snapshot_files is not None else {}
    files_to_check["src/loglens/runtime.py"] = content
    seed_root = project_root / "qualification" / "seed_repos" / "loglens"

    for raw_path, file_text in files_to_check.items():
        norm_path = Path(raw_path).as_posix().lstrip("/")
        if norm_path not in _ALLOWED_LOGLENS_SNAPSHOT_PATHS:
            return f"Unexpected file path in snapshot: {raw_path!r}"
        if norm_path in {"pyproject.toml", "src/loglens/cli.py", "src/loglens/registry.py"}:
            seed_file = seed_root / norm_path
            if seed_file.is_file() and file_text.strip() != seed_file.read_text(encoding="utf-8").strip():
                return f"Snapshot file {norm_path!r} differs from expected loglens seed"
            continue
        if norm_path == "src/loglens/parsing.py":
            seed_parsing_file = seed_root / norm_path
            if seed_parsing_file.is_file():
                seed_parsing_text = seed_parsing_file.read_text(encoding="utf-8")
                stage1_parsing_text = (
                    seed_parsing_text + "\n\ndef control_helper() -> str:\n    return 'control_ok'\n"
                )
                try:
                    actual_unparsed = ast.unparse(ast.parse(file_text))
                    allowed_parsing = {
                        ast.unparse(ast.parse(seed_parsing_text)),
                        ast.unparse(ast.parse(stage1_parsing_text)),
                    }
                except SyntaxError as exc:
                    return f"SyntaxError in snapshot file {norm_path!r}: {exc}"
                if actual_unparsed not in allowed_parsing:
                    return f"Snapshot file {norm_path!r} differs from expected seed or Stage-1 control AST"
            continue
        if not norm_path.endswith(".py"):
            continue
        try:
            mod_tree = ast.parse(file_text)
        except SyntaxError as exc:
            return f"SyntaxError in snapshot file {norm_path!r}: {exc}"

        safety_err = _validate_snapshot_ast_safety(mod_tree)
        if safety_err is not None:
            return f"{norm_path}: {safety_err}"

        if norm_path in _LOGLENS_PACKAGE_INIT_PATHS:
            for stmt in mod_tree.body:
                if (
                    isinstance(stmt, ast.Expr)
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)
                ):
                    continue
                return f"Disallowed statement {type(stmt).__name__} in package initializer {norm_path!r}"

        elif norm_path == "src/loglens/shared/formatting.py":
            seed_fmt_file = seed_root / norm_path
            if not seed_fmt_file.is_file():
                return f"Missing trusted seed file for {norm_path!r}"
            seed_fmt_tree = ast.parse(seed_fmt_file.read_text(encoding="utf-8"))
            if ast.dump(mod_tree, include_attributes=False) != ast.dump(
                seed_fmt_tree, include_attributes=False
            ):
                return f"Snapshot file {norm_path!r} AST differs from trusted loglens seed"

    try:
        runtime_tree = ast.parse(content)
    except SyntaxError as exc:
        return f"SyntaxError parsing runtime.py: {exc}"

    target_class: ast.ClassDef | None = None
    for node in runtime_tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            target_class = node
            break
    if target_class is None:
        return f"Class {class_name!r} not found as top-level AST ClassDef"

    method_nodes = {
        stmt.name: stmt
        for stmt in target_class.body
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for m_name in expected_methods:
        if m_name not in method_nodes:
            return f"Class {class_name!r} is missing method {m_name!r}"
        m_node = method_nodes[m_name]
        if isinstance(m_node, ast.AsyncFunctionDef) or m_node.decorator_list:
            return f"Method {m_name!r} has unexpected async/decorator modifiers"
        args_obj = m_node.args
        if (
            len(args_obj.args) != 1
            or args_obj.args[0].arg != "self"
            or args_obj.posonlyargs
            or args_obj.kwonlyargs
            or args_obj.vararg is not None
            or args_obj.kwarg is not None
            or args_obj.defaults
            or any(d is not None for d in args_obj.kw_defaults)
        ):
            return f"Method {m_name!r} must accept only (self)"
        non_doc_stmts = [
            stmt
            for idx, stmt in enumerate(m_node.body)
            if not (
                idx == 0
                and isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)
            )
        ]
        if len(non_doc_stmts) != 1 or not isinstance(non_doc_stmts[0], ast.Return):
            return (
                f"Method {m_name!r} must consist solely of a constant return statement; "
                f"found {[type(s).__name__ for s in non_doc_stmts]}"
            )
        ret_val = non_doc_stmts[0].value
        if not isinstance(ret_val, ast.Constant) or not isinstance(ret_val.value, str):
            return f"Method {m_name!r} must return a string constant"
        expected_const = _EXPECTED_METHOD_RETURN_VALUES.get(m_name)
        if expected_const is not None and ret_val.value != expected_const:
            return (
                f"Method {m_name!r} returns {ret_val.value!r}, expected {expected_const!r}"
            )

    seed_runtime_file = seed_root / "src/loglens/runtime.py"
    if not seed_runtime_file.is_file():
        return "Missing trusted seed file qualification/seed_repos/loglens/src/loglens/runtime.py"
    seed_runtime_tree = ast.parse(seed_runtime_file.read_text(encoding="utf-8"))
    expected_doc = ast.get_docstring(seed_runtime_tree, clean=False)
    actual_doc = ast.get_docstring(runtime_tree, clean=False)
    if actual_doc != expected_doc:
        return (
            "Snapshot runtime.py module docstring missing or displaced from statement 0: "
            f"expected {expected_doc!r}, got {actual_doc!r}"
        )

    stripped_actual, actual_err = _strip_expected_methods_from_class(
        runtime_tree,
        class_name=class_name,
        expected_methods=expected_methods,
    )
    if actual_err is not None or stripped_actual is None:
        return f"Snapshot runtime.py failed structural seed comparison: {actual_err}"

    if ast.dump(stripped_actual, include_attributes=False) != ast.dump(
        seed_runtime_tree, include_attributes=False
    ):
        return (
            "Snapshot runtime.py AST differs from trusted loglens seed in statement order or "
            f"content outside permitted methods {list(expected_methods)}"
        )

    return None


def _run_isolated_snapshot_behavior_check(
    content: str,
    *,
    class_name: str = "CommandResult",
    expected_methods: tuple[str, str] = ("alpha_method", "beta_method"),
    snapshot_files: dict[str, str] | None = None,
) -> tuple[bool, dict[str, Any], str | None]:
    """
    Execute behavioral check behind an OS-enforced boundary (macOS Seatbelt / sandbox-exec)
    with both file-read* and file-write* restricted at the OS boundary, C-level sys.addaudithook,
    and an authenticated non-stdout result channel using only the materialized snapshot.
    """
    if sys.platform != "darwin" or not os.path.isfile("/usr/bin/sandbox-exec"):
        return (
            False,
            {},
            f"Required OS sandbox /usr/bin/sandbox-exec is unavailable on platform {sys.platform!r}",
        )

    static_err = _verify_snapshot_files_and_methods(
        content,
        snapshot_files,
        class_name=class_name,
        expected_methods=expected_methods,
    )
    if static_err is not None:
        return False, {}, static_err

    driver_code = """
def _isolated_main():
    import argparse
    import dataclasses
    import json
    import os
    import sys

    payload = json.loads(sys.stdin.read())
    out_fd = os.dup(1)
    devnull_fd = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull_fd, 1)
    os.close(devnull_fd)
    sys.stdout = open(os.devnull, "w", encoding="utf-8")

    snap_src = os.path.realpath(payload["snap_src"])
    stdlib_prefix = os.path.realpath(sys.base_prefix) + os.sep
    sys.path[:] = [snap_src] + [
        p
        for p in sys.path
        if "site-packages" not in p
        and os.path.realpath(p).startswith(os.path.realpath(sys.base_prefix))
    ]

    allowed_read_prefixes = (snap_src + os.sep, stdlib_prefix)
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
    blocked_events = {
        "os.mkdir",
        "os.remove",
        "os.unlink",
        "os.rmdir",
        "os.rename",
        "os.replace",
        "os.symlink",
        "os.link",
        "os.chmod",
        "os.chown",
        "os.truncate",
        "os.system",
        "os.fork",
        "os.posix_spawn",
        "os.exec",
        "os.spawn",
        "subprocess.Popen",
        "socket.__new__",
        "socket.connect",
        "socket.bind",
        "ctypes.dlopen",
        "shutil.rmtree",
        "shutil.copyfile",
        "shutil.move",
        "sqlite3.connect",
        "sqlite3.connect/handle",
    }
    os_exit = os._exit
    os_write = os.write
    os_close = os.close
    os_realpath = os.path.realpath
    json_dumps = json.dumps
    stderr_write = sys.stderr.write
    class_name = payload["class_name"]
    expected_methods = tuple(payload["expected_methods"])
    nonce = payload["nonce"]
    del payload

    def _audit_hook(event, args):
        if event == "open":
            path, mode, flags = args[0], args[1], args[2]
            if mode and any(c in str(mode) for c in ("w", "a", "+", "x")):
                stderr_write(f"Audit blocked write open: {path}\\n")
                os_exit(99)
            if isinstance(flags, int) and (flags & write_flags):
                stderr_write(f"Audit blocked write flags open: {path}\\n")
                os_exit(99)
            if isinstance(path, (str, bytes)):
                p_str = path.decode("utf-8", "ignore") if isinstance(path, bytes) else path
                real_p = os_realpath(p_str)
                if not real_p.startswith(allowed_read_prefixes):
                    stderr_write(f"Audit blocked read outside snapshot: {real_p}\\n")
                    os_exit(99)
            else:
                stderr_write(f"Audit blocked fd open: {path}\\n")
                os_exit(99)
        elif event in blocked_events or event.startswith("sqlite3."):
            stderr_write(f"Audit blocked event: {event}\\n")
            os_exit(99)

    sys.modules["__main__"].__dict__.clear()
    sys.addaudithook(_audit_hook)

    try:
        import loglens.runtime as mod
        cls_obj = getattr(mod, class_name, None)
        if not isinstance(cls_obj, type):
            raise RuntimeError(f"Class {class_name!r} missing or not a class in executed namespace")

        instance = cls_obj(plain_lines=["ok"], columns=["col"], rows=[{"col": "val"}])
        outputs = {}
        for m_name in expected_methods:
            fn = getattr(instance, m_name, None)
            if not callable(fn):
                raise RuntimeError(f"Method {m_name!r} is not callable")
            outputs[m_name] = fn()
    except BaseException as exc:
        stderr_write(f"{type(exc).__name__}: {exc}\\n")
        os_exit(1)

    res_bytes = json_dumps(
        {"ok": True, "nonce": nonce, "outputs": outputs}
    ).encode("utf-8")
    os_write(out_fd, res_bytes)
    os_close(out_fd)

_isolated_main()
"""
    with tempfile.TemporaryDirectory(prefix="mx_verifier_iso_") as iso_root:
        iso_root_path = Path(iso_root).resolve()
        iso_cwd = iso_root_path / "cwd"
        iso_home = iso_root_path / "home"
        iso_tmp = iso_root_path / "tmp"
        snap_root = iso_root_path / "snapshot"
        iso_cwd.mkdir(parents=True, exist_ok=True)
        iso_home.mkdir(parents=True, exist_ok=True)
        iso_tmp.mkdir(parents=True, exist_ok=True)
        snap_root.mkdir(parents=True, exist_ok=True)

        seed_root = project_root / "qualification" / "seed_repos" / "loglens"
        files_to_materialize: dict[str, str]
        if snapshot_files is not None:
            files_to_materialize = dict(snapshot_files)
            files_to_materialize.setdefault("src/loglens/runtime.py", content)
        else:
            files_to_materialize = {
                "src/loglens/__init__.py": (seed_root / "src/loglens/__init__.py").read_text(
                    encoding="utf-8"
                ),
                "src/loglens/shared/__init__.py": (
                    seed_root / "src/loglens/shared/__init__.py"
                ).read_text(encoding="utf-8"),
                "src/loglens/shared/formatting.py": (
                    seed_root / "src/loglens/shared/formatting.py"
                ).read_text(encoding="utf-8"),
                "src/loglens/runtime.py": content,
            }

        expected_snapshot_files: set[str] = set()
        for rel_path, file_text in files_to_materialize.items():
            norm_rel = Path(rel_path).as_posix().lstrip("/")
            if not norm_rel or ".." in Path(norm_rel).parts:
                return False, {}, f"Invalid snapshot file path: {rel_path!r}"
            target_file = (snap_root / norm_rel).resolve()
            try:
                target_file.relative_to(snap_root)
            except ValueError:
                return False, {}, f"Snapshot file path escapes root: {rel_path!r}"
            target_file.parent.mkdir(parents=True, exist_ok=True)
            target_file.write_text(file_text, encoding="utf-8")
            expected_snapshot_files.add(target_file.relative_to(iso_root_path).as_posix())

        snap_src = snap_root / "src"
        snap_src.mkdir(parents=True, exist_ok=True)

        sandbox_exec = "/usr/bin/sandbox-exec"
        real_base = os.path.realpath(sys.base_prefix).replace('"', '\\"')
        real_exec = os.path.realpath(sys.executable).replace('"', '\\"')
        real_snap = os.path.realpath(str(snap_root)).replace('"', '\\"')
        sandbox_profile = (
            "(version 1) "
            "(allow default) "
            "(deny network*) "
            "(deny process-fork) "
            '(deny file-write* (subpath "/")) '
            '(allow file-write* (literal "/dev/null")) '
            '(deny file-read* (subpath "/")) '
            '(allow file-read-metadata (subpath "/")) '
            '(allow file-read* (literal "/")) '
            '(allow file-read* (subpath "/System")) '
            '(allow file-read* (subpath "/usr")) '
            '(allow file-read* (subpath "/bin")) '
            '(allow file-read* (subpath "/sbin")) '
            '(allow file-read* (subpath "/dev")) '
            '(allow file-read* (subpath "/Library")) '
            '(allow file-read* (subpath "/opt/homebrew")) '
            '(allow file-read* (subpath "/private/etc")) '
            '(allow file-read* (subpath "/private/var/db")) '
            f'(allow file-read* (subpath "{real_base}")) '
            f'(allow file-read* (literal "{real_exec}")) '
            f'(allow file-read* (subpath "{real_snap}"))'
        )
        cmd = [
            sandbox_exec,
            "-p",
            sandbox_profile,
            os.path.realpath(sys.executable),
            "-I",
            "-S",
            "-B",
            "-c",
            driver_code,
        ]

        minimal_env = {
            "PATH": "/usr/bin:/bin",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "HOME": str(iso_home),
            "TMPDIR": str(iso_tmp),
            "TMP": str(iso_tmp),
            "TEMP": str(iso_tmp),
        }
        expected_nonce = secrets.token_hex(16)
        stdin_payload = json.dumps(
            {
                "snap_src": str(snap_src),
                "class_name": class_name,
                "expected_methods": list(expected_methods),
                "nonce": expected_nonce,
            }
        )
        proc = subprocess.run(  # nosec B603
            cmd,
            input=stdin_payload,
            cwd=str(iso_cwd),
            env=minimal_env,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        actual_files = {
            p.relative_to(iso_root_path).as_posix()
            for p in iso_root_path.rglob("*")
            if p.is_file()
        }
        unexpected_files = sorted(actual_files - expected_snapshot_files)
        if unexpected_files:
            return False, {}, f"Isolated verifier detected unexpected file creation: {unexpected_files}"
        if proc.returncode != 0:
            err_msg = (proc.stderr or proc.stdout or f"exit code {proc.returncode}").strip()
            return False, {}, err_msg
        try:
            parsed = json.loads(proc.stdout.strip())
        except Exception as exc:
            return False, {}, f"Invalid JSON from isolated verifier: {exc}"
        if parsed.get("ok") is not True or parsed.get("nonce") != expected_nonce:
            return False, {}, "Unauthenticated or forged output from isolated verifier"
        outputs = parsed.get("outputs")
        if not isinstance(outputs, dict):
            return False, {}, "Missing outputs dict from isolated verifier"
        static_err = _verify_snapshot_files_and_methods(
            content,
            files_to_materialize,
            class_name=class_name,
            expected_methods=expected_methods,
        )
        if static_err is not None:
            return False, {}, static_err
        return True, outputs, None


def inspect_merged_runtime_ast_and_behavior(
    content: str,
    *,
    class_name: str = "CommandResult",
    expected_methods: tuple[str, str] = ("alpha_method", "beta_method"),
    require_behavior: bool = False,
    snapshot_files: dict[str, str] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """
    Inspect Python AST in-process and execute behavior only inside an OS-sandboxed,
    credential-free subprocess using only the materialized snapshot files.
    """
    details: dict[str, Any] = {
        "class_name": class_name,
        "expected_methods": list(expected_methods),
        "ast_parsed": False,
        "ast_safety_verified": False,
        "class_found": False,
        "class_methods_found": [],
        "ast_verified": False,
        "behavior_executed": False,
        "behavior_verified": False,
        "behavior_outputs": {},
        "isolated_subprocess": True,
        "os_sandbox_enforced": sys.platform == "darwin" and os.path.isfile("/usr/bin/sandbox-exec"),
    }
    try:
        tree = ast.parse(content)
        details["ast_parsed"] = True
    except SyntaxError as exc:
        details["error"] = f"SyntaxError parsing snapshot content: {exc}"
        return False, details

    safety_error = _validate_snapshot_ast_safety(tree)
    if safety_error is not None:
        details["error"] = safety_error
        return False, details

    target_class: ast.ClassDef | None = None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            target_class = node
            break

    if target_class is None:
        details["error"] = f"Class {class_name!r} not found as top-level AST ClassDef"
        return False, details

    details["class_found"] = True
    method_nodes = {
        stmt.name: stmt
        for stmt in target_class.body
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    details["class_methods_found"] = sorted(method_nodes.keys())

    missing_methods = [m for m in expected_methods if m not in method_nodes]
    if missing_methods:
        details["error"] = (
            f"Class {class_name!r} is missing AST method definitions: {missing_methods}; "
            f"found methods: {sorted(method_nodes.keys())}"
        )
        return False, details

    files_err = _verify_snapshot_files_and_methods(
        content,
        snapshot_files,
        class_name=class_name,
        expected_methods=expected_methods,
    )
    if files_err is not None:
        details["error"] = files_err
        return False, details
    details["ast_safety_verified"] = True

    ast_return_constants: dict[str, Any] = {}
    for m_name in expected_methods:
        m_node = method_nodes[m_name]
        for sub in ast.walk(m_node):
            if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Constant):
                ast_return_constants[m_name] = sub.value.value
    details["ast_return_constants"] = ast_return_constants
    details["ast_verified"] = True

    sub_ok, outputs, sub_err = _run_isolated_snapshot_behavior_check(
        content,
        class_name=class_name,
        expected_methods=expected_methods,
        snapshot_files=snapshot_files,
    )
    if sub_ok:
        details["behavior_executed"] = True
        details["behavior_outputs"] = outputs
        if outputs.get(expected_methods[0]) == "alpha" and outputs.get(expected_methods[1]) == "beta":
            details["behavior_verified"] = True
        elif require_behavior:
            details["error"] = f"Unexpected method return values: {outputs}"
            return False, details
    else:
        if require_behavior:
            details["error"] = f"Behavioral execution failed in isolated subprocess: {sub_err}"
            return False, details
        details["behavior_note"] = f"AST passed; isolated behavior execution skipped: {sub_err}"

    return True, details


async def _download_and_materialize_snapshot_files(
    base_url: str,
    repo_id: str,
    snapshot_id: str,
) -> tuple[dict[str, str] | None, str | None]:
    """Download a snapshot tar.gz from Aether and extract it securely into a file map."""
    dl_url = f"{base_url}/v1/repos/{repo_id}/snapshots/{snapshot_id}/download"
    if urllib.parse.urlparse(dl_url).scheme not in {"http", "https"}:
        return None, f"Invalid URL scheme for snapshot download: {dl_url}"
    try:
        req = urllib.request.Request(dl_url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310
            tar_bytes = resp.read()
    except Exception as exc:
        return None, f"Failed to download snapshot archive {snapshot_id}: {exc}"

    helper_worker = Worker(
        config=MXConfig(),
        worker_id="snapshot-verifier",
        queue=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        secrets=None,  # type: ignore[arg-type]
        vcs=None,
    )
    with tempfile.TemporaryDirectory(prefix="mx_snap_verify_") as extract_dir:
        try:
            copied_paths = await helper_worker.extract_snapshot_securely(
                tar_bytes,
                extract_dir,
                repo_id=repo_id,
                snapshot_id=snapshot_id,
                require_manifest=False,
                clean_destination=False,
            )
        except Exception as exc:
            return None, f"Failed to extract snapshot archive {snapshot_id}: {exc}"

        root = Path(extract_dir).resolve()
        file_map: dict[str, str] = {}
        for rel in copied_paths:
            f_path = root / rel
            if f_path.is_file():
                file_map[rel] = f_path.read_text(encoding="utf-8")
        return file_map, None


async def verify_snapshot_markers(
    args: Any,
    repo_id: str,
    snapshot_id: str,
    file_path: str,
    snapshot_verification_out: dict[str, Any] | None = None,
) -> int:
    """MX-5: Verify promoted snapshot contains both expected methods on CommandResult via AST and behavior."""
    raw_base_url = getattr(args, "aether_base_url", None)
    base_url = (raw_base_url if isinstance(raw_base_url, str) else "http://localhost:8100").rstrip("/")
    res = _http_request(f"{base_url}/v1/repos/{repo_id}/snapshots/{snapshot_id}/files/{file_path}")
    if "error" in res:
        print(f"Failed to fetch file {file_path} from snapshot {snapshot_id}: {res}")
        return 1

    content = res.get("content", "")
    require_behavior = getattr(args, "require_behavior_check", False) is True
    require_archive = getattr(args, "require_snapshot_archive", False) is True
    explicit_snapshot_files = getattr(args, "snapshot_files", None)
    snapshot_files: dict[str, str] | None = (
        dict(explicit_snapshot_files) if isinstance(explicit_snapshot_files, dict) else None
    )

    if require_archive and snapshot_files is None:
        dl_files, dl_err = await _download_and_materialize_snapshot_files(
            base_url, repo_id, snapshot_id
        )
        if dl_err is not None or not dl_files:
            err_msg = dl_err or f"Materialized snapshot {snapshot_id} contained zero files"
            if snapshot_verification_out is not None:
                snapshot_verification_out["content"] = content
                snapshot_verification_out["verification"] = {"error": err_msg}
            print(f"FAILED: Snapshot archive materialization failed: {err_msg}")
            return 1
        if file_path not in dl_files:
            err_msg = f"Target file {file_path!r} missing from materialized snapshot {snapshot_id}"
            if snapshot_verification_out is not None:
                snapshot_verification_out["content"] = content
                snapshot_verification_out["verification"] = {"error": err_msg}
            print(f"FAILED: {err_msg}")
            return 1
        if dl_files[file_path] != content:
            err_msg = f"Mismatch between single-file endpoint and materialized archive for {file_path!r}"
            if snapshot_verification_out is not None:
                snapshot_verification_out["content"] = content
                snapshot_verification_out["verification"] = {"error": err_msg}
            print(f"FAILED: {err_msg}")
            return 1
        snapshot_files = dl_files

    ok, details = inspect_merged_runtime_ast_and_behavior(
        content,
        class_name="CommandResult",
        expected_methods=("alpha_method", "beta_method"),
        require_behavior=require_behavior,
        snapshot_files=snapshot_files,
    )
    if snapshot_files is not None:
        details["materialized_snapshot_file_count"] = len(snapshot_files)
        details["materialized_snapshot_files"] = sorted(snapshot_files.keys())
    if snapshot_verification_out is not None:
        snapshot_verification_out["content"] = content
        snapshot_verification_out["verification"] = details

    if not ok:
        print(f"FAILED: Snapshot AST/behavior verification failed: {details.get('error')}")
        return 1

    return 0


def verify_checks_executed(
    checks_ledger: list[dict[str, Any]],
    *,
    required_check_names: tuple[str, ...] = REQUIRED_CANARY_CHECKS,
    promotion_records: list[dict[str, Any]] | None = None,
    expected_promotion_ids: list[str] | tuple[str, ...] | None = None,
    required_gate_names: tuple[str, ...] = REQUIRED_PROMOTION_GATES,
    gate_results_rows: list[dict[str, Any]] | None = None,
    require_gate_results_rows: bool = False,
) -> tuple[bool, str]:
    """Fail closed if zero checks execute, if any check fails/skips, or if any required check/gate/promotion is missing."""
    if not checks_ledger:
        return False, "Zero checks executed in canary ledger"

    by_name: dict[str, dict[str, Any]] = {}
    for entry in checks_ledger:
        name = str(entry.get("name") or "").strip()
        status = str(entry.get("status") or "").strip().upper()
        if not name:
            return False, f"Unnamed check entry in ledger: {entry}"
        if status in {"SKIP", "SKIPPED"}:
            return False, f"Check {name!r} was skipped"
        if status != "PASS" or entry.get("passed") is not True:
            return False, f"Check {name!r} did not pass: status={status!r}"
        by_name[name] = entry

    for req_name in required_check_names:
        if req_name not in by_name:
            return False, f"Required check {req_name!r} was not executed"

    promo_to_snapshot: dict[str, str] = {}
    if promotion_records is not None:
        if not promotion_records:
            return False, "Zero promotion records provided for gate check verification"
        seen_promo_ids: set[str] = set()
        for promo in promotion_records:
            if not isinstance(promo, dict) or "error" in promo:
                return False, f"Failed or invalid promotion detail record: {promo}"
            pid = str(promo.get("id") or "").strip()
            if not pid:
                return False, f"Promotion record missing 'id': {promo}"
            seen_promo_ids.add(pid)
            ev_raw = promo.get("evidence", "{}")
            ev = json.loads(ev_raw) if isinstance(ev_raw, str) else (ev_raw or {})
            snap_id = str(promo.get("snapshot_id") or ev.get("_replay_snapshot_id") or "").strip()
            if snap_id:
                promo_to_snapshot[pid] = snap_id
            gate_entries: dict[str, str] = {
                str(k): str(v)
                for k, v in ev.items()
                if not str(k).startswith("_")
                and str(k) not in {"merged_paths"}
                and isinstance(v, str)
            }
            replay_vector = ev.get("_replay_gate_vector")
            if isinstance(replay_vector, str) and replay_vector.strip():
                for token in replay_vector.split("|"):
                    if ":" in token:
                        g_name, g_status = token.split(":", 1)
                        gate_entries.setdefault(g_name.strip(), g_status.strip())
            if not gate_entries:
                return False, f"Promotion {pid} executed zero gates"
            for gate_name, gate_status in gate_entries.items():
                norm_status = gate_status.strip().upper()
                if norm_status in {"SKIP", "SKIPPED"}:
                    return False, f"Promotion {pid} skipped gate {gate_name!r}"
                if norm_status != "PASS":
                    return (
                        False,
                        f"Promotion {pid} gate {gate_name!r} had status {norm_status!r}",
                    )
            missing_gates = [g for g in required_gate_names if g not in gate_entries]
            if missing_gates:
                return (
                    False,
                    f"Promotion {pid} is missing required gates {missing_gates}; found {sorted(gate_entries.keys())}",
                )

        if expected_promotion_ids is not None:
            if not expected_promotion_ids or any(not str(x or "").strip() for x in expected_promotion_ids):
                return False, f"Invalid expected_promotion_ids: {expected_promotion_ids}"
            missing_promos = [str(x) for x in expected_promotion_ids if str(x) not in seen_promo_ids]
            if missing_promos:
                return (
                    False,
                    f"Missing expected promotion records {missing_promos}; checked {sorted(seen_promo_ids)}",
                )

    if require_gate_results_rows and gate_results_rows is None:
        return False, "Missing required gate_results_rows from Aether SQLite database"

    if gate_results_rows is not None:
        unit_test_rows = [r for r in gate_results_rows if r.get("gate_name") == "unit_tests"]
        if not unit_test_rows:
            return False, "No unit_tests gate rows recorded in aether_gate_results"
        unit_by_snapshot: dict[str, list[dict[str, Any]]] = {}
        for row in unit_test_rows:
            if str(row.get("status") or "").upper() != "PASS":
                return False, f"unit_tests gate row {row.get('id')} did not pass"
            if int(row.get("tests_passed") or 0) <= 0:
                return (
                    False,
                    f"unit_tests gate row {row.get('id')} recorded tests_passed={row.get('tests_passed')!r} (expected > 0)",
                )
            row_snap = str(row.get("snapshot_id") or "").strip()
            if row_snap:
                unit_by_snapshot.setdefault(row_snap, []).append(row)

        if promotion_records is not None:
            target_pids = (
                [str(x) for x in expected_promotion_ids]
                if expected_promotion_ids is not None
                else [str(p.get("id") or "").strip() for p in promotion_records if isinstance(p, dict)]
            )
            for pid in target_pids:
                snap_id = promo_to_snapshot.get(pid, "")
                if not snap_id:
                    return (
                        False,
                        f"Promotion {pid} is missing snapshot_id required to match unit_tests gate row",
                    )
                if snap_id not in unit_by_snapshot:
                    return (
                        False,
                        f"Promotion {pid} (snapshot {snap_id}) is missing a passing unit_tests row in aether_gate_results",
                    )

    return True, f"All {len(checks_ledger)} executed checks and promotion gates passed with 0 skipped"


def archive_telemetry_raw(args: Any, data: dict[str, Any], log_path: Path) -> bool:
    """MX-6: Archive raw telemetry response to log file."""
    _ = args
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2) + "\n")
        return True
    except Exception as e:
        print(f"Failed to archive telemetry: {e}")
        return False


_LOCAL_PATH_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?:/private)?/var/folders/[A-Za-z0-9._/-]+/T"), "/tmp"),  # nosec B108
    (re.compile(r"/private/tmp"), "/tmp"),  # nosec B108
    (re.compile(r"/Users/[A-Za-z0-9._-]+"), "$HOME"),
)


def _sanitize_text_value(val: str) -> str:
    cleaned = val
    home_prefix = str(Path.home())
    if home_prefix and home_prefix in cleaned:
        cleaned = cleaned.replace(home_prefix, "$HOME")
    for pattern, replacement in _LOCAL_PATH_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


ALLOWED_AETHER_DB_TABLES: frozenset[str] = frozenset(
    {
        "aether_conflict_sets",
        "aether_contention_winners",
        "aether_gate_results",
        "aether_heads",
        "aether_operation_graph_nodes",
        "aether_promotions",
        "aether_proposal_contentions",
        "aether_proposals",
        "aether_repositories",
        "aether_snapshots",
        "aether_system_metric_events",
    }
)


def _validate_output_dir(output_dir: Path) -> tuple[bool, str]:
    """Reject symlink, non-directory, overlapping checkout/home/root, or non-empty output destination."""
    if output_dir.is_symlink():
        return False, f"Refusing symlink output directory: {output_dir}"
    resolved = output_dir.resolve()
    forbidden_roots = {Path("/").resolve(), Path.home().resolve(), project_root.resolve()}
    if resolved in forbidden_roots:
        return False, f"Refusing unsafe output directory root: {resolved}"
    try:
        project_root.resolve().relative_to(resolved)
        return False, f"Refusing output directory that is an ancestor of project_root: {resolved}"
    except ValueError:
        pass

    if resolved.exists():
        if not resolved.is_dir():
            return False, f"Output path exists and is not a directory: {resolved}"
        existing_entries = [p.name for p in resolved.iterdir()]
        if existing_entries:
            return (
                False,
                f"Refusing non-empty output directory {resolved} (contains {sorted(existing_entries)[:5]})",
            )
    return True, "OK"


def _sanitize_and_export_sqlite_db(
    source_db: Path,
    dest_db: Path,
    *,
    repo_id: str | None = None,
    run_id: str | None = None,
) -> None:
    """
    Export only canary-scoped rows (repo_id, run_id, 'seed-run') across allowed Aether tables,
    scrub local machine paths across all text columns, and VACUUM.
    """
    if not source_db.is_file():
        raise ValueError(f"Source Aether SQLite database does not exist: {source_db}")
    src_conn = sqlite3.connect(str(source_db))
    try:
        src_conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        src_conn.commit()
    except Exception:
        pass
    finally:
        src_conn.close()

    with tempfile.TemporaryDirectory(prefix="mx_sqlite_export_") as tmp_dir:
        staged_db = Path(tmp_dir) / "aether.db"
        shutil.copy2(source_db, staged_db)
        conn = sqlite3.connect(str(staged_db))
        try:
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            ]
            for table in list(tables):
                if table not in ALLOWED_AETHER_DB_TABLES:
                    conn.execute(f'DROP TABLE IF EXISTS "{table}"')  # nosec B608
                    tables.remove(table)

            if repo_id is not None:
                if "aether_repositories" in tables:
                    conn.execute(
                        'DELETE FROM "aether_repositories" WHERE id IS NULL OR id != ?',
                        (repo_id,),
                    )
                    rem_repos = conn.execute(
                        'SELECT count(*) FROM "aether_repositories" WHERE id = ?',
                        (repo_id,),
                    ).fetchone()[0]
                    if rem_repos != 1:
                        raise ValueError(
                            f"Source database {source_db} does not contain expected repo_id={repo_id!r}"
                        )
                for table in tables:
                    cols = [
                        str(r[1])
                        for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
                    ]
                    if "repo_id" in cols:
                        conn.execute(
                            f'DELETE FROM "{table}" WHERE repo_id IS NULL OR repo_id != ?',  # nosec B608
                            (repo_id,),
                        )

            if run_id is not None:
                if "aether_proposals" in tables:
                    conn.execute(
                        'DELETE FROM "aether_proposals" WHERE source_run_id IS NULL OR source_run_id NOT IN (?, ?)',
                        (run_id, "seed-run"),
                    )
                    run_prop_count = conn.execute(
                        'SELECT count(*) FROM "aether_proposals" WHERE source_run_id = ?',
                        (run_id,),
                    ).fetchone()[0]
                    if run_prop_count <= 0:
                        raise ValueError(
                            f"Source database {source_db} has zero proposals for run_id={run_id!r}"
                        )
                if "aether_system_metric_events" in tables:
                    conn.execute(
                        'DELETE FROM "aether_system_metric_events" WHERE source_run_id IS NOT NULL AND source_run_id NOT IN (?, ?)',
                        (run_id, "seed-run"),
                    )

            if "aether_proposal_contentions" in tables and "aether_proposals" in tables:
                conn.execute(
                    'DELETE FROM "aether_proposal_contentions" WHERE proposal_id NOT IN (SELECT id FROM "aether_proposals")'
                )
            if "aether_contention_winners" in tables and "aether_proposals" in tables:
                winner_cols = [
                    str(r[1])
                    for r in conn.execute('PRAGMA table_info("aether_contention_winners")').fetchall()
                ]
                if "winner_proposal_id" in winner_cols:
                    conn.execute(
                        'DELETE FROM "aether_contention_winners" WHERE winner_proposal_id IS NULL OR winner_proposal_id NOT IN (SELECT id FROM "aether_proposals")'
                    )

            for table in tables:
                cur = conn.execute(f'SELECT rowid, * FROM "{table}"')  # nosec B608
                col_names = [desc[0] for desc in cur.description][1:]
                rows = cur.fetchall()
                for row in rows:
                    rowid = row[0]
                    updates: dict[str, str] = {}
                    for col_name, cell_val in zip(col_names, row[1:], strict=True):
                        if isinstance(cell_val, str):
                            sanitized = _sanitize_text_value(cell_val)
                            if sanitized != cell_val:
                                updates[col_name] = sanitized
                    if updates:
                        set_clause = ", ".join(f'"{col}" = ?' for col in updates)
                        conn.execute(
                            f'UPDATE "{table}" SET {set_clause} WHERE rowid = ?',  # nosec B608
                            (*updates.values(), rowid),
                        )
            conn.commit()
            conn.execute("PRAGMA journal_mode=DELETE;")
            conn.execute("VACUUM;")
            conn.commit()
        finally:
            conn.close()

        dest_db.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(staged_db, dest_db)


def _dump_sqlite_evidence(db_path: Path, repo_id: str, run_id: str) -> dict[str, Any]:
    """Extract run-scoped and seed-isolated SQLite rows from Aether's database."""
    if not db_path.exists():
        return {"error": f"SQLite database not found at {db_path}"}

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        conn.commit()
    except Exception:
        pass

    def _sanitize_row(row_dict: dict[str, Any]) -> dict[str, Any]:
        cleaned: dict[str, Any] = {}
        for k, v in row_dict.items():
            if isinstance(v, str):
                cleaned[k] = _sanitize_text_value(v)
            else:
                cleaned[k] = v
        return cleaned

    def _rows(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        cur = conn.execute(query, params)
        return [_sanitize_row(dict(r)) for r in cur.fetchall()]

    run_proposals = _rows(
        "SELECT * FROM aether_proposals WHERE repo_id = ? AND source_run_id = ? ORDER BY submitted_at ASC",
        (repo_id, run_id),
    )
    seed_proposals = _rows(
        "SELECT * FROM aether_proposals WHERE repo_id = ? AND source_run_id = 'seed-run' ORDER BY submitted_at ASC",
        (repo_id,),
    )
    all_promotions = _rows(
        "SELECT * FROM aether_promotions WHERE repo_id = ? ORDER BY to_head_sequence ASC",
        (repo_id,),
    )
    run_proposal_ids = {p["id"] for p in run_proposals}
    seed_proposal_ids = {p["id"] for p in seed_proposals}

    run_promotions = []
    seed_promotions = []
    for promo in all_promotions:
        inc = set(json.loads(promo.get("included_proposals") or "[]"))
        if inc & run_proposal_ids:
            run_promotions.append(promo)
        elif inc & seed_proposal_ids:
            seed_promotions.append(promo)

    contentions = _rows(
        """
        SELECT c.* FROM aether_proposal_contentions c
        JOIN aether_proposals p ON p.id = c.proposal_id
        WHERE p.repo_id = ? AND p.source_run_id = ?
        ORDER BY p.submitted_at ASC
        """,
        (repo_id, run_id),
    )
    metric_events = _rows(
        "SELECT * FROM aether_system_metric_events WHERE repo_id = ? AND source_run_id = ? ORDER BY created_at ASC",
        (repo_id, run_id),
    )
    heads = _rows(
        "SELECT * FROM aether_heads WHERE repo_id = ? ORDER BY sequence ASC",
        (repo_id,),
    )
    snapshots = _rows(
        "SELECT * FROM aether_snapshots WHERE repo_id = ? ORDER BY head_sequence ASC",
        (repo_id,),
    )
    gate_results = _rows(
        "SELECT * FROM aether_gate_results WHERE repo_id = ? ORDER BY started_at ASC",
        (repo_id,),
    )
    conn.close()
    return {
        "run_scoped_proposals": run_proposals,
        "run_scoped_promotions": run_promotions,
        "run_scoped_contentions": contentions,
        "run_scoped_metric_events": metric_events,
        "repo_heads": heads,
        "repo_snapshots": snapshots,
        "repo_gate_results": gate_results,
        "seed_activity_isolated": {
            "seed_proposals": seed_proposals,
            "seed_promotions": seed_promotions,
        },
    }


async def run_canary(args: argparse.Namespace) -> int:
    started_at_utc = datetime.now(UTC).isoformat()
    run_id = args.run_id or f"canary-run-{datetime.now(UTC).strftime('%Y%m%dt%H%M%Sz')}"
    base_url = args.aether_base_url.rstrip("/")

    checks_ledger: list[dict[str, Any]] = []

    def record_check(name: str, passed: bool, detail: str, metadata: dict[str, Any] | None = None) -> None:
        checks_ledger.append(
            {
                "name": name,
                "status": "PASS" if passed else "FAIL",
                "passed": bool(passed),
                "detail": detail,
                "metadata": metadata or {},
                "recorded_at_utc": datetime.now(UTC).isoformat(),
            }
        )

    print(f"=== Starting Aether Bounded Semantic Canary (Run ID: {run_id}) ===")
    print(f"Target Aether Server: {base_url}")
    print(f"Expected Aether Commit Pin: {args.expected_aether_sha}")

    log_dir = (
        Path(args.output_dir)
        if args.output_dir
        else (project_root / ".runtime" / "canary_logs" / run_id)
    )
    out_ok, out_err = _validate_output_dir(log_dir)
    if not out_ok:
        print(f"Preflight failed: {out_err}")
        return 1

    if not args.aether_db_path:
        print("Preflight failed: --aether-db-path is required to verify SQLite gate rows and export bundle DB")
        return 1
    db_file = Path(args.aether_db_path).resolve()
    if not db_file.parent.is_dir():
        print(f"Preflight failed: --aether-db-path parent directory does not exist: {db_file.parent}")
        return 1

    # 1. Preflight check: verify build info, SHA pin, SQLite dialect, and feature flags
    aether_build_info: dict[str, Any] = {}
    args.require_sqlite = True
    args.require_behavior_check = True
    args.require_snapshot_archive = True
    if await verify_preflight_remediation(args, build_info_out=aether_build_info) != 0:
        record_check("preflight_build_sha_and_flags", False, "Preflight check failed", aether_build_info)
        return 1

    record_check(
        "preflight_build_sha_and_flags",
        True,
        f"Verified Aether commit={aether_build_info.get('commit')} database_profile={aether_build_info.get('database_profile')}",
        aether_build_info,
    )
    print(f"Preflight OK: Aether build info = {json.dumps(aether_build_info)}")

    mx_commit = _get_git_head_commit(project_root)
    mx_branch = _get_git_branch(project_root)
    mx_dirty = _is_git_dirty(project_root)
    if args.require_clean_mx and mx_dirty:
        print("Preflight failed: MX Swarm checkout has uncommitted tracked modifications")
        return 1

    # Ensure current Python environment's bin directory is first on PATH for PATH-relative commands
    python_bin_dir = str(Path(sys.executable).resolve().parent)
    current_path = os.environ.get("PATH", "")
    path_parts = [p for p in current_path.split(os.pathsep) if p]
    if python_bin_dir not in path_parts:
        os.environ["PATH"] = os.pathsep.join([python_bin_dir, *path_parts])

    # 2. Setup fresh target repository in Aether with real executable PATH-relative gates (0 skipped gates)
    gate_configs = [
        {
            "name": "syntax",
            "command": "python -m py_compile src/loglens/runtime.py src/loglens/parsing.py",
            "required": True,
            "timeout_seconds": 60,
            "order": 0,
        },
        {
            "name": "build",
            "command": "python -m compileall -q src",
            "required": True,
            "timeout_seconds": 60,
            "order": 1,
        },
        {
            "name": "unit_tests",
            "command": (
                "python -c \"import pathlib, sys; sys.path.insert(0, 'src'); "
                "from loglens.parsing import parse_line; "
                "from loglens.runtime import CommandResult, render_result; "
                "e = parse_line('2026-07-21T00:00:00Z level=INFO component=api duration_ms=12 message=\\\"ok\\\"'); "
                "assert e.level == 'INFO' and e.component == 'api' and e.duration_ms == 12; "
                "r = CommandResult(plain_lines=['ok'], columns=['col'], rows=[{'col': 'val'}]); "
                "assert render_result(r, output_format='plain') == 'ok'; "
                "print('2 passed')\""
            ),
            "required": True,
            "timeout_seconds": 60,
            "order": 2,
        },
        {
            "name": "lint",
            "command": "python -m ruff check --select F,E --line-length 100 src",
            "required": True,
            "timeout_seconds": 60,
            "order": 3,
        },
    ]
    repo_name = f"loglens-canary-{int(time.time())}"
    repo_res = _http_request(
        f"{base_url}/v1/repos",
        method="POST",
        data={
            "name": repo_name,
            "description": "Bounded semantic merge canary repo",
            "config": {
                "auto_promote": True,
                "gates": gate_configs,
            },
        },
    )
    if "error" in repo_res or "id" not in repo_res:
        print(f"Failed to create repository in Aether: {repo_res}")
        return 1

    repo_id = repo_res["id"]
    print(f"Created Aether repository: {repo_id} ({repo_name})")
    if not db_file.is_file():
        print(f"Preflight failed: Aether SQLite database not found at {db_file} after repo creation")
        return 1

    # 3. Seed repository with loglens base files (all 9 package and config files)
    seed_repo_path = project_root / "qualification" / "seed_repos" / "loglens"
    if not seed_repo_path.exists():
        print(f"Seed repo path not found: {seed_repo_path}")
        return 1

    seed_files = [
        "pyproject.toml",
        "src/loglens/__init__.py",
        "src/loglens/parsing.py",
        "src/loglens/runtime.py",
        "src/loglens/cli.py",
        "src/loglens/registry.py",
        "src/loglens/commands/__init__.py",
        "src/loglens/shared/__init__.py",
        "src/loglens/shared/formatting.py",
    ]

    seed_proposal_ids: list[str] = []
    for rel_path in seed_files:
        full_path = seed_repo_path / rel_path
        if not full_path.exists():
            print(f"Missing seed file: {full_path}")
            return 1
        content = full_path.read_text(encoding="utf-8")
        seed_task_slug = rel_path.replace("/", "-").replace(".", "-").replace("_", "-")
        p_res = _http_request(
            f"{base_url}/v1/repos/{repo_id}/proposals",
            method="POST",
            data={
                "agent_id": "canary-seeder",
                "task_id": f"seed-{seed_task_slug}",
                "change_type": "full_file",
                "change_content": content,
                "affected_paths": [rel_path],
                "risk_tier": 1,
                "confidence_score": 1.0,
                "model_id": "seeder",
                "rationale": f"Seed {rel_path}",
                "requires_tests": False,
                "requires_security_scan": False,
                "idempotency_key": f"seed-{run_id}-{rel_path}",
                "source": "canary_seed",
                "source_run_id": "seed-run",
            },
        )
        if "error" in p_res or "proposal_id" not in p_res:
            print(f"Failed to submit seed proposal for {rel_path}: {p_res}")
            return 1
        seed_proposal_ids.append(p_res["proposal_id"])

    print(f"Submitted {len(seed_proposal_ids)} seed proposals. Waiting for seed promotion...")
    seed_promoted = False
    for _ in range(30):
        all_promoted = True
        for pid in seed_proposal_ids:
            st = _http_request(f"{base_url}/v1/repos/{repo_id}/proposals/{pid}")
            if st.get("status") != "PROMOTED":
                all_promoted = False
                break
        if all_promoted:
            seed_promoted = True
            break
        await asyncio.sleep(2)

    seed_promotion_id: str | None = None
    seed_snapshot_id: str | None = None
    seed_promotion_ids: list[str] = []
    if not seed_promoted:
        print("Seed proposals were not auto-promoted in time; triggering explicit promotion...")
        prom_res = _http_request(
            f"{base_url}/v1/repos/{repo_id}/promotions",
            method="POST",
            data={"proposal_ids": seed_proposal_ids},
        )
        if "error" in prom_res or not (prom_res.get("success") or prom_res.get("promoted")):
            record_check("seed_promotion", False, f"Failed to promote seed proposals: {prom_res}")
            print(f"Failed to promote seed proposals: {prom_res}")
            return 1
        seed_promotion_id = prom_res.get("promotion_id")
        seed_snapshot_id = prom_res.get("snapshot_id")

    seed_promos_res = _http_request(f"{base_url}/v1/repos/{repo_id}/promotions")
    seed_promos = seed_promos_res.get("promotions", [])
    if seed_promos:
        seed_promotion_ids = [str(p_item.get("id")) for p_item in seed_promos if p_item.get("id")]
        latest_seed_promo = max(seed_promos, key=lambda p_item: int(p_item.get("to_head_sequence") or 0))
        seed_promotion_id = seed_promotion_id or latest_seed_promo.get("id")
        seed_snapshot_id = seed_snapshot_id or latest_seed_promo.get("snapshot_id")

    if seed_promotion_id and str(seed_promotion_id) not in seed_promotion_ids:
        seed_promotion_ids.append(str(seed_promotion_id))

    if not seed_promotion_id:
        record_check("seed_promotion", False, "Missing seed promotion ID after seed promotion")
        return 1

    record_check(
        "seed_promotion",
        True,
        f"Promoted {len(seed_proposal_ids)} seed proposals in {seed_promotion_id} (snapshot {seed_snapshot_id}) under isolated source_run_id='seed-run'",
        {
            "seed_proposal_ids": seed_proposal_ids,
            "seed_promotion_id": seed_promotion_id,
            "seed_promotion_ids": seed_promotion_ids,
            "seed_snapshot_id": seed_snapshot_id,
        },
    )
    print(f"Seed files promoted successfully (promotion={seed_promotion_id}, snapshot={seed_snapshot_id}).")

    # 4. Prepare local workspace & canned completion fixtures for workers
    run_dir = Path(tempfile.mkdtemp(prefix=f"mx_canary_{run_id}_"))
    barrier_dir = run_dir / "barrier"
    barrier_dir.mkdir(parents=True, exist_ok=True)

    for rel_path in seed_files:
        src_file = seed_repo_path / rel_path
        dst_file = run_dir / rel_path
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dst_file)
    subprocess.run(["git", "init"], cwd=run_dir, check=True, capture_output=True)  # nosec B603 B607
    subprocess.run(  # nosec B603 B607
        ["git", "config", "user.email", "canary@mx.local"],
        cwd=run_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(  # nosec B603 B607
        ["git", "config", "user.name", "Canary Runner"],
        cwd=run_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "add", "."], cwd=run_dir, check=True, capture_output=True)  # nosec B603 B607
    subprocess.run(  # nosec B603 B607
        ["git", "commit", "-m", "Initial seed"],
        cwd=run_dir,
        check=True,
        capture_output=True,
    )
    base_commit = _get_git_head_commit(run_dir)
    print(f"Initialized local worker clone source at {run_dir} (base_commit={base_commit})")

    parsing_orig = (seed_repo_path / "src/loglens/parsing.py").read_text(encoding="utf-8")
    task_a_content = parsing_orig + "\n\ndef control_helper() -> str:\n    return 'control_ok'\n"

    task_b_content = '''"""Shared command result and rendering helpers."""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from loglens.shared import formatting

OUTPUT_FORMATS = ("plain", "table", "json", "csv")


@dataclass(frozen=True)
class CommandResult:
    """Normalized command payload for every CLI feature."""

    plain_lines: list[str]
    columns: list[str]
    rows: list[dict[str, object]]

    def alpha_method(self) -> str:
        return "alpha"


def add_output_format_option(parser: argparse.ArgumentParser) -> None:
    """Attach the shared output-format option to a command parser."""
    parser.add_argument(
        "--format",
        choices=OUTPUT_FORMATS,
        default="plain",
        help="Output format for the command result.",
    )


def render_result(result: CommandResult, *, output_format: str) -> str:
    """Render a command result in the requested output format."""
    if output_format == "plain":
        return formatting.join_lines(result.plain_lines)

    formatter = getattr(formatting, f"render_{output_format}", None)
    if formatter is None:
        raise NotImplementedError(f"output format '{output_format}' is not available yet")
    return formatter(result.rows, result.columns)
'''

    task_c_content = '''"""Shared command result and rendering helpers."""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from loglens.shared import formatting

OUTPUT_FORMATS = ("plain", "table", "json", "csv")


@dataclass(frozen=True)
class CommandResult:
    """Normalized command payload for every CLI feature."""

    plain_lines: list[str]
    columns: list[str]
    rows: list[dict[str, object]]

    def beta_method(self) -> str:
        return "beta"


def add_output_format_option(parser: argparse.ArgumentParser) -> None:
    """Attach the shared output-format option to a command parser."""
    parser.add_argument(
        "--format",
        choices=OUTPUT_FORMATS,
        default="plain",
        help="Output format for the command result.",
    )


def render_result(result: CommandResult, *, output_format: str) -> str:
    """Render a command result in the requested output format."""
    if output_format == "plain":
        return formatting.join_lines(result.plain_lines)

    formatter = getattr(formatting, f"render_{output_format}", None)
    if formatter is None:
        raise NotImplementedError(f"output format '{output_format}' is not available yet")
    return formatter(result.rows, result.columns)
'''

    fixtures = {
        "task-a": {
            "files": {"src/loglens/parsing.py": task_a_content},
            "note": "Task A control change applied.",
        },
        "task-b": {
            "files": {"src/loglens/runtime.py": task_b_content},
            "note": "Task B alpha method added.",
        },
        "task-c": {
            "files": {"src/loglens/runtime.py": task_c_content},
            "note": "Task C beta method added.",
        },
    }
    fixture_file = run_dir / "canned_completions.json"
    fixture_file.write_text(json.dumps(fixtures, indent=2), encoding="utf-8")

    os.environ["MX_CANNED_COMPLETION_FIXTURE"] = str(fixture_file)
    os.environ["MX_CONTENTION_BARRIER_DIR"] = str(barrier_dir)
    os.environ["MX_RUN_ID"] = run_id
    os.environ["MX_LOCAL_REPO_PATH"] = str(run_dir)

    # 5. Configure MX Swarm Workers
    config = MXConfig()
    config.vcs.provider = "aether"
    config.vcs.aether_base_url = base_url
    config.vcs.aether_repo_id = repo_id
    config.vcs.canonical_enforcement = False
    config.vcs.reconciliation_enabled = False
    config.queue.provider = "memory"
    config.worker.poll_interval_seconds = 1

    queue = create_queue_adapter(config.queue.to_dict())
    secrets = create_secrets_adapter(config.secrets.to_dict())

    vcs_1 = create_aether_vcs(
        base_url=base_url,
        repo_id=repo_id,
        agent_id="worker-1",
    )
    vcs_2 = create_aether_vcs(
        base_url=base_url,
        repo_id=repo_id,
        agent_id="worker-2",
    )

    worker_1 = Worker(
        config=config,
        worker_id="worker-1",
        queue=queue,
        vcs=vcs_1,
        secrets=secrets,
    )
    worker_2 = Worker(
        config=config,
        worker_id="worker-2",
        queue=queue,
        vcs=vcs_2,
        secrets=secrets,
    )
    await worker_1.initialize()
    await worker_2.initialize()

    # 6. Build Task Contracts with explicit deterministic acceptance checks (non-empty)
    group_id = f"canary-group-{run_id}"
    full_group_id = f"semantic-canary:{run_id}:{repo_id}:{group_id}"

    task_a_checks = [
        "python -m py_compile src/loglens/parsing.py",
        "python -c \"import ast; tree = ast.parse(open('src/loglens/parsing.py').read()); assert any(isinstance(n, ast.FunctionDef) and n.name == 'control_helper' for n in tree.body)\"",
    ]
    task_b_checks = [
        "python -m py_compile src/loglens/runtime.py",
        "python -c \"import ast; tree = ast.parse(open('src/loglens/runtime.py').read()); cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'CommandResult'); assert any(isinstance(m, ast.FunctionDef) and m.name == 'alpha_method' for m in cls.body)\"",
    ]
    task_c_checks = [
        "python -m py_compile src/loglens/runtime.py",
        "python -c \"import ast; tree = ast.parse(open('src/loglens/runtime.py').read()); cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'CommandResult'); assert any(isinstance(m, ast.FunctionDef) and m.name == 'beta_method' for m in cls.body)\"",
    ]

    task_a = TaskContract(
        id="task-a",
        type=TaskType.FEATURE,
        goal="Add control helper to parsing",
        context=TaskContext(files_hint=["src/loglens/parsing.py"]),
        acceptance=AcceptanceCriteria(checks=task_a_checks, criteria=["control_helper exists"]),
        constraints=TaskConstraints(risk=RiskLevel.LOW, merge_policy=MergePolicy.AUTO_ON_PASS),
        stream=TaskStream.FEATURE,
        tier=RiskTier.TIER_0,
        contention_mode=False,
        contention_base_head_id=base_commit,
        defer_evaluation=False,
    )

    task_b = TaskContract(
        id="task-b",
        type=TaskType.FEATURE,
        goal="Add alpha method",
        context=TaskContext(files_hint=["src/loglens/runtime.py"]),
        acceptance=AcceptanceCriteria(checks=task_b_checks, criteria=["alpha_method exists"]),
        constraints=TaskConstraints(risk=RiskLevel.MEDIUM, merge_policy=MergePolicy.REQUIRE_REVIEW),
        stream=TaskStream.FEATURE,
        tier=RiskTier.TIER_1,
        contention_mode=True,
        contention_group_id=group_id,
        contention_policy="semantic_merge",
        contention_target_paths=["src/loglens/runtime.py"],
        contention_base_head_id=base_commit,
        contention_expected_tasks=["task-b", "task-c"],
        defer_evaluation=True,
    )

    task_c = TaskContract(
        id="task-c",
        type=TaskType.FEATURE,
        goal="Add beta method",
        context=TaskContext(files_hint=["src/loglens/runtime.py"]),
        acceptance=AcceptanceCriteria(checks=task_c_checks, criteria=["beta_method exists"]),
        constraints=TaskConstraints(risk=RiskLevel.MEDIUM, merge_policy=MergePolicy.REQUIRE_REVIEW),
        stream=TaskStream.FEATURE,
        tier=RiskTier.TIER_1,
        contention_mode=True,
        contention_group_id=group_id,
        contention_policy="semantic_merge",
        contention_target_paths=["src/loglens/runtime.py"],
        contention_base_head_id=base_commit,
        contention_expected_tasks=["task-b", "task-c"],
        defer_evaluation=True,
    )

    print("Publishing Stage 1 Control Task (task-a)...")
    await queue.publish(task_a, priority=task_a.priority)

    w1_task = asyncio.create_task(worker_1.run_loop())
    w2_task = asyncio.create_task(worker_2.run_loop())

    print("Waiting for Stage 1 (task-a) to be PROMOTED...")
    stage1_promoted = False
    task_a_proposal_id: str | None = None
    for _ in range(30):
        res = _http_request(f"{base_url}/v1/repos/{repo_id}/proposals?source_run_id={run_id}")
        proposals = res.get("proposals", [])
        by_task = {p.get("task_id"): p for p in proposals}
        if "task-a" in by_task and by_task["task-a"].get("status") == "PROMOTED":
            stage1_promoted = True
            task_a_proposal_id = by_task["task-a"].get("id")
            break
        await asyncio.sleep(1)

    if not stage1_promoted:
        await worker_1.shutdown()
        await worker_2.shutdown()
        await asyncio.gather(w1_task, w2_task, return_exceptions=True)
        record_check("stage1_control_promotion", False, "Task A failed to promote within timeout")
        print("FAILED: Stage 1 Control Task (task-a) failed to promote within timeout.")
        return 1

    head_res = _http_request(f"{base_url}/v1/repos/{repo_id}/head")
    new_base_commit = head_res.get("head_id")
    control_snapshot_id = head_res.get("snapshot_id")
    if not new_base_commit:
        await worker_1.shutdown()
        await worker_2.shutdown()
        await asyncio.gather(w1_task, w2_task, return_exceptions=True)
        record_check("stage1_control_promotion", False, f"Missing head_id after Task A: {head_res}")
        print(f"FAILED: Could not retrieve new head_id after Task A promotion: {head_res}")
        return 1

    control_promotion_id: str | None = None
    control_promos_res = _http_request(f"{base_url}/v1/repos/{repo_id}/promotions")
    for p_item in control_promos_res.get("promotions", []):
        if p_item.get("to_head_id") == new_base_commit or str(p_item.get("id")) not in seed_promotion_ids:
            control_promotion_id = p_item.get("id")
            control_snapshot_id = control_snapshot_id or p_item.get("snapshot_id")
            if p_item.get("to_head_id") == new_base_commit:
                break

    if not control_promotion_id:
        await worker_1.shutdown()
        await worker_2.shutdown()
        await asyncio.gather(w1_task, w2_task, return_exceptions=True)
        record_check("stage1_control_promotion", False, "Missing control promotion ID after Task A promotion")
        return 1

    record_check(
        "stage1_control_promotion",
        True,
        f"Task A proposal {task_a_proposal_id} promoted in {control_promotion_id} (snapshot {control_snapshot_id}) to head {new_base_commit}",
        {
            "task_a_proposal_id": task_a_proposal_id,
            "control_promotion_id": control_promotion_id,
            "control_snapshot_id": control_snapshot_id,
            "new_base_head_id": new_base_commit,
        },
    )
    print(
        f"Stage 1 (task-a) PROMOTED in {control_promotion_id} "
        f"(snapshot={control_snapshot_id}, head_id={new_base_commit})"
    )

    task_b.contention_base_head_id = new_base_commit
    task_c.contention_base_head_id = new_base_commit

    print("Publishing Stage 2 Contention Tasks (task-b, task-c)...")
    await queue.publish(task_b, priority=task_b.priority)
    await queue.publish(task_c, priority=task_c.priority)

    print("Waiting for Stage 2 tasks to be ingested...")
    cohort_proposal_ids: list[str] = []
    for _ in range(60):
        res = _http_request(f"{base_url}/v1/repos/{repo_id}/proposals?source_run_id={run_id}")
        proposals = res.get("proposals", [])
        by_task = {p.get("task_id"): p for p in proposals}

        a_done = "task-a" in by_task and by_task["task-a"].get("status") == "PROMOTED"
        b_ingested = "task-b" in by_task and by_task["task-b"].get("status") in ("PENDING", "EVALUATING")
        c_ingested = "task-c" in by_task and by_task["task-c"].get("status") in ("PENDING", "EVALUATING")

        if a_done and b_ingested and c_ingested:
            cohort_proposal_ids = [by_task["task-b"]["id"], by_task["task-c"]["id"]]
            break
        await asyncio.sleep(2)

    await worker_1.shutdown()
    await worker_2.shutdown()
    await asyncio.gather(w1_task, w2_task, return_exceptions=True)

    total_worker_succeeded = worker_1.metrics.tasks_succeeded + worker_2.metrics.tasks_succeeded
    total_worker_failed = worker_1.metrics.tasks_failed + worker_2.metrics.tasks_failed
    recorded_worker_checks: dict[str, list[dict[str, Any]]] = {}
    for w_obj in (worker_1, worker_2):
        for tid, entries in w_obj.get_executed_acceptance_checks().items():
            recorded_worker_checks[tid] = entries

    expected_check_counts = {"task-a": 2, "task-b": 2, "task-c": 2}
    actual_check_counts = {
        tid: len(recorded_worker_checks.get(tid, []))
        for tid in ("task-a", "task-b", "task-c")
    }
    passed_check_counts = {
        tid: sum(1 for item in recorded_worker_checks.get(tid, []) if item.get("passed") is True)
        for tid in ("task-a", "task-b", "task-c")
    }
    total_recorded_checks = sum(actual_check_counts.values())
    total_passed_checks = sum(passed_check_counts.values())
    worker_checks_ok = (
        total_worker_succeeded == 3
        and total_worker_failed == 0
        and actual_check_counts == expected_check_counts
        and passed_check_counts == expected_check_counts
    )
    record_check(
        "worker_task_acceptance_checks",
        worker_checks_ok,
        f"Workers settled {total_worker_succeeded}/3 tasks with {total_passed_checks}/{total_recorded_checks} recorded acceptance checks passed",
        {
            "tasks_succeeded": total_worker_succeeded,
            "tasks_failed": total_worker_failed,
            "task_checks_per_task": actual_check_counts,
            "task_checks_passed_per_task": passed_check_counts,
            "total_task_acceptance_checks_executed": total_recorded_checks,
            "total_task_acceptance_checks_passed": total_passed_checks,
            "recorded_checks_by_task": recorded_worker_checks,
        },
    )
    if not worker_checks_ok:
        print(f"FAILED: Worker acceptance check accounting failed: {actual_check_counts} / {passed_check_counts}")
        return 1

    if len(cohort_proposal_ids) != 2:
        record_check(
            "cohort_proposal_count_check",
            False,
            f"Expected 2 cohort proposals, got {len(cohort_proposal_ids)}: {cohort_proposal_ids}",
        )
        print("FAILED: Tasks did not reach expected pre-evaluation states within timeout.")
        return 1

    record_check(
        "cohort_proposal_count_check",
        True,
        f"Verified 2 contending cohort proposals ({cohort_proposal_ids}) separate from 1 control proposal ({task_a_proposal_id})",
        {
            "independent_runs_executed": 1,
            "cohort_proposals_count": len(cohort_proposal_ids),
            "cohort_proposal_ids": cohort_proposal_ids,
            "control_proposals_count": 1,
            "control_proposal_id": task_a_proposal_id,
            "total_run_proposals_count": 3,
            "seed_proposals_count": len(seed_proposal_ids),
        },
    )

    print(f"Stage 1 (task-a) PROMOTED. Stage 2 cohort ingested: {cohort_proposal_ids}")
    print(f"Triggering atomic cohort evaluation for group {full_group_id}...")

    eval_res = _http_request(
        f"{base_url}/v1/repos/{repo_id}/cohort/evaluate",
        method="POST",
        data={"contention_group_id": full_group_id},
    )
    cohort_promoted = bool(eval_res.get("success") or eval_res.get("promoted"))
    promo_id = eval_res.get("promotion_id")
    if "error" in eval_res or not cohort_promoted or not promo_id:
        record_check("cohort_evaluation_and_promotion", False, f"Cohort evaluation failed: {eval_res}")
        print(f"FAILED: Cohort evaluation failed or did not promote: {eval_res}")
        return 1

    promos_list_res = _http_request(f"{base_url}/v1/repos/{repo_id}/promotions")
    promos_summary_list = promos_list_res.get("promotions", [])
    snapshot_id = eval_res.get("snapshot_id")
    if not snapshot_id:
        for p_item in promos_summary_list:
            if p_item.get("id") == promo_id:
                snapshot_id = p_item.get("snapshot_id")
                break
    if not snapshot_id:
        head_after_cohort = _http_request(f"{base_url}/v1/repos/{repo_id}/head")
        snapshot_id = head_after_cohort.get("snapshot_id")

    record_check(
        "cohort_evaluation_and_promotion",
        True,
        f"Cohort promoted in promotion_id={promo_id}, snapshot_id={snapshot_id}",
        eval_res,
    )
    print(f"Cohort promoted! Promotion ID: {promo_id}, Snapshot ID: {snapshot_id}")

    # 7. Verification Criteria
    promo_record: dict[str, Any] = {}
    if await verify_promotion_membership(args, repo_id, promo_id, cohort_proposal_ids, promo_out=promo_record) != 0:
        record_check("same_promotion_membership", False, f"Promotion {promo_id} membership mismatch")
        return 1
    record_check(
        "same_promotion_membership",
        True,
        f"Both cohort proposals {cohort_proposal_ids} belong to promotion {promo_id}",
        {"promotion_id": promo_id, "included_proposals": cohort_proposal_ids},
    )

    telemetry_events: list[dict[str, Any]] = []
    if await verify_canary_events(
        args,
        repo_id,
        run_id,
        cohort_proposal_ids,
        args.structural_only,
        events_out=telemetry_events,
    ) != 0:
        record_check("zero_semantic_fallback_events", False, "Telemetry event validation failed")
        return 1
    record_check(
        "zero_semantic_fallback_events",
        True,
        f"Verified {len(telemetry_events)} structural_merge events and 0 semantic_fallback events",
        {"events_count": len(telemetry_events)},
    )

    snapshot_check_data: dict[str, Any] = {}
    if await verify_snapshot_markers(
        args,
        repo_id,
        str(snapshot_id),
        "src/loglens/runtime.py",
        snapshot_verification_out=snapshot_check_data,
    ) != 0:
        record_check(
            "promoted_snapshot_ast_and_behavior",
            False,
            "Promoted snapshot AST/behavior check failed",
            snapshot_check_data.get("verification"),
        )
        return 1
    record_check(
        "promoted_snapshot_ast_and_behavior",
        True,
        "Promoted snapshot AST and isolated runtime behavior verified for CommandResult.alpha_method and CommandResult.beta_method",
        snapshot_check_data.get("verification"),
    )

    sqlite_dump = _dump_sqlite_evidence(db_file, repo_id, run_id)
    if "error" in sqlite_dump:
        print(f"FAILED: Unable to read SQLite evidence from {db_file}: {sqlite_dump['error']}")
        return 1

    expected_promotion_ids = list(
        dict.fromkeys([*(seed_promotion_ids or [str(seed_promotion_id)]), str(control_promotion_id), str(promo_id)])
    )
    # Fetch full promotion details for all promotions in the repo; fail closed if any detail fetch fails
    all_promos: list[dict[str, Any]] = []
    promo_fetch_error: str | None = None
    summary_ids = [str(p_item.get("id")) for p_item in promos_summary_list if p_item.get("id")]
    for req_pid in expected_promotion_ids:
        if req_pid not in summary_ids:
            summary_ids.append(req_pid)
    for pid in summary_ids:
        p_detail = _http_request(f"{base_url}/v1/repos/{repo_id}/promotions/{pid}")
        if "error" in p_detail or not p_detail.get("id"):
            promo_fetch_error = f"Failed to fetch promotion detail for {pid}: {p_detail}"
            all_promos.append({"error": promo_fetch_error, "id": pid})
        else:
            all_promos.append(p_detail)

    gate_rows_for_check = sqlite_dump.get("repo_gate_results")
    gates_ok, gates_detail = verify_checks_executed(
        checks_ledger,
        required_check_names=REQUIRED_CANARY_CHECKS[:-1],
        promotion_records=all_promos,
        expected_promotion_ids=expected_promotion_ids,
        required_gate_names=REQUIRED_PROMOTION_GATES,
        gate_results_rows=gate_rows_for_check,
        require_gate_results_rows=True,
    )
    record_check(
        "aether_promotion_gates_non_skipped",
        gates_ok,
        gates_detail,
        {
            "expected_promotion_ids": expected_promotion_ids,
            "promotions_checked": [p.get("id") for p in all_promos],
            "required_gates": list(REQUIRED_PROMOTION_GATES),
        },
    )

    final_ok, final_detail = verify_checks_executed(
        checks_ledger,
        required_check_names=REQUIRED_CANARY_CHECKS,
        promotion_records=all_promos,
        expected_promotion_ids=expected_promotion_ids,
        required_gate_names=REQUIRED_PROMOTION_GATES,
        gate_results_rows=gate_rows_for_check,
        require_gate_results_rows=True,
    )
    if not final_ok:
        print(f"FAILED: Check verification failed: {final_detail}")
        return 1

    completed_at_utc = datetime.now(UTC).isoformat()
    summary_counts = {
        "independent_runs_executed": 1,
        "cohort_proposals_count": len(cohort_proposal_ids),
        "control_proposals_count": 1,
        "total_run_proposals_count": 1 + len(cohort_proposal_ids),
        "seed_proposals_count": len(seed_proposal_ids),
        "checks_executed_count": len(checks_ledger),
        "checks_passed_count": sum(1 for c in checks_ledger if c.get("passed")),
        "checks_skipped_count": sum(
            1 for c in checks_ledger if str(c.get("status", "")).upper() in {"SKIP", "SKIPPED"}
        ),
        "structural_merge_events_count": sum(
            1 for e in telemetry_events if e.get("event_type") == "structural_merge"
        ),
        "semantic_fallback_events_count": sum(
            1 for e in telemetry_events if e.get("event_type") == "semantic_fallback"
        ),
    }

    merged_runtime_content = snapshot_check_data.get("content", "")
    evidence_payload: dict[str, Any] = {
        "schema_version": 2,
        "run_type": "public_clean_checkout_reproduction",
        "run_id": run_id,
        "repo_id": repo_id,
        "started_at_utc": started_at_utc,
        "completed_at_utc": completed_at_utc,
        "pins_and_provenance": {
            "mx_swarm": {
                "commit": mx_commit,
                "branch": mx_branch,
                "dirty": mx_dirty,
                "base_public_commit": "f12fb568b083f8a28b982d68cd662c38718ab2ea",
                "historical_runner_reference_blob": "2eb95c70298b1731f5f7cccd9e712b2c5ee056ae",
            },
            "project_aether": {
                "pinned_commit": args.expected_aether_sha,
                "running_build": aether_build_info,
            },
            "historical_july21_reference": HISTORICAL_JULY21_PROVENANCE,
        },
        "counts_summary": summary_counts,
        "failed_attempts": RECORDED_FAILED_ATTEMPTS,
        "seed_stage": {
            "seed_proposal_ids": seed_proposal_ids,
            "promotion_id": seed_promotion_id,
            "snapshot_id": seed_snapshot_id,
        },
        "stage1_control": {
            "task_id": "task-a",
            "proposal_id": task_a_proposal_id,
            "promotion_id": control_promotion_id,
            "snapshot_id": control_snapshot_id,
            "promoted_head_id": new_base_commit,
        },
        "stage2_cohort": {
            "contention_group_id": full_group_id,
            "cohort_task_ids": ["task-b", "task-c"],
            "cohort_proposal_ids": cohort_proposal_ids,
            "promotion_id": promo_id,
            "snapshot_id": snapshot_id,
            "same_promotion_verified": True,
            "merged_file_path": "src/loglens/runtime.py",
            "merged_file_sha256": hashlib.sha256(merged_runtime_content.encode("utf-8")).hexdigest(),
            "ast_and_behavior_verification": snapshot_check_data.get("verification"),
        },
        "checks_executed": checks_ledger,
        "telemetry_events": telemetry_events,
        **sqlite_dump,
    }

    manifest_payload: dict[str, Any] = {
        "schema_version": 1,
        "title": "MX Swarm x Project Aether Bounded Class-1 Semantic Merge Canary (Public Clean-Checkout Reproduction)",
        "generated_at_utc": completed_at_utc,
        "run_id": run_id,
        "repo_id": repo_id,
        "verdict": "PASS",
        "repositories": {
            "mx_swarm": {
                "base_public_commit": "f12fb568b083f8a28b982d68cd662c38718ab2ea",
                "evaluated_commit": mx_commit,
                "branch": mx_branch,
                "dirty": mx_dirty,
                "historical_runner_blob_reference": "2eb95c70298b1731f5f7cccd9e712b2c5ee056ae",
            },
            "project_aether": {
                "pinned_commit": args.expected_aether_sha,
                "verified_runtime_commit": aether_build_info.get("commit"),
                "branch": aether_build_info.get("branch"),
                "dirty": aether_build_info.get("dirty"),
                "database_profile": aether_build_info.get("database_profile"),
                "server_profile": aether_build_info.get("server_profile"),
            },
        },
        "promotions_and_snapshots": {
            "seed_promotion_id": seed_promotion_id,
            "seed_snapshot_id": seed_snapshot_id,
            "control_promotion_id": control_promotion_id,
            "control_snapshot_id": control_snapshot_id,
            "cohort_promotion_id": promo_id,
            "cohort_snapshot_id": snapshot_id,
        },
        "historical_run_reference": HISTORICAL_JULY21_PROVENANCE,
        "reporting_separation": {
            "independent_runs_executed": 1,
            "cohort_proposals_in_run": len(cohort_proposal_ids),
            "control_proposals_in_run": 1,
            "total_run_proposals": 1 + len(cohort_proposal_ids),
            "isolated_seed_proposals": len(seed_proposal_ids),
        },
        "failed_attempts": RECORDED_FAILED_ATTEMPTS,
        "checks_summary": {
            "total_executed": len(checks_ledger),
            "passed": sum(1 for c in checks_ledger if c.get("passed")),
            "failed": sum(1 for c in checks_ledger if not c.get("passed")),
            "skipped": 0,
            "checks": [
                {
                    "name": c["name"],
                    "status": c["status"],
                    "detail": c["detail"],
                }
                for c in checks_ledger
            ],
        },
        "artifacts": list(PUBLIC_BUNDLE_ARTIFACTS),
    }

    from scripts.check_release_hygiene import scan_evidence_bundle_dir

    # Stage bundle in a fresh temporary directory without deleting anything in log_dir
    with tempfile.TemporaryDirectory(prefix="mx_canary_bundle_stage_") as bundle_stage_raw:
        stage_dir = Path(bundle_stage_raw).resolve()
        archive_telemetry_raw(args, {"events": telemetry_events}, stage_dir / "telemetry.log")
        (stage_dir / "merged_runtime.py").write_text(merged_runtime_content, encoding="utf-8")
        try:
            _sanitize_and_export_sqlite_db(
                db_file,
                stage_dir / "aether.db",
                repo_id=repo_id,
                run_id=run_id,
            )
        except Exception as exc:
            print(f"FAILED: Unable to export scoped Aether SQLite database: {exc}")
            return 1

        (stage_dir / "final_canary_evidence.json").write_text(
            _sanitize_text_value(json.dumps(evidence_payload, indent=2)),
            encoding="utf-8",
        )
        (stage_dir / "manifest.json").write_text(
            _sanitize_text_value(json.dumps(manifest_payload, indent=2)),
            encoding="utf-8",
        )

        sha_lines: list[str] = []
        for artifact_name in sorted(PUBLIC_BUNDLE_ARTIFACTS):
            artifact_file = stage_dir / artifact_name
            if not artifact_file.is_file():
                print(f"FAILED: Missing staged bundle artifact {artifact_name}")
                return 1
            digest = hashlib.sha256(artifact_file.read_bytes()).hexdigest()
            sha_lines.append(f"{digest}  {artifact_name}")
        (stage_dir / "SHA256SUMS").write_text("\n".join(sha_lines) + "\n", encoding="utf-8")

        hygiene_findings = scan_evidence_bundle_dir(
            stage_dir,
            require_sha256sums=True,
        )
        if hygiene_findings:
            print(
                f"FAILED: Staged evidence bundle hygiene scan failed: {json.dumps(hygiene_findings, indent=2)}"
            )
            return 1

        out_ok_final, out_err_final = _validate_output_dir(log_dir)
        if not out_ok_final:
            print(f"FAILED: {out_err_final}")
            return 1
        log_dir.mkdir(parents=True, exist_ok=True)
        for bundle_name in (*PUBLIC_BUNDLE_ARTIFACTS, "SHA256SUMS"):
            shutil.copy2(stage_dir / bundle_name, log_dir / bundle_name)

    final_hygiene_findings = scan_evidence_bundle_dir(
        log_dir,
        require_sha256sums=True,
    )
    if final_hygiene_findings:
        print(
            f"FAILED: Evidence bundle hygiene scan failed: {json.dumps(final_hygiene_findings, indent=2)}"
        )
        return 1

    print(
        f"=== Canary Summary: independent_runs=1 | cohort_proposals={len(cohort_proposal_ids)} "
        f"| control_proposals=1 | seed_proposals={len(seed_proposal_ids)} "
        f"| checks_executed={len(checks_ledger)} (0 skipped) ==="
    )
    print(f"=== All Canary Verification Criteria PASSED! Artifacts written to {log_dir} ===")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Bounded Semantic Canary against Project Aether")
    parser.add_argument("--aether-base-url", default="http://localhost:8100", help="Aether server URL")
    parser.add_argument("--run-id", default=None, help="Explicit run ID")
    parser.add_argument(
        "--expected-aether-sha",
        default=PINNED_AETHER_COMMIT,
        help="Expected Aether git commit SHA exposed by /v1/system/build",
    )
    parser.add_argument(
        "--structural-only",
        action="store_true",
        default=True,
        help="Enforce structural-only merge (default True)",
    )
    parser.add_argument(
        "--allow-synthesis",
        action="store_false",
        dest="structural_only",
        help="Allow deterministic synthesis fallback",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory to write merged_runtime.py, final_canary_evidence.json, manifest.json, and SHA256SUMS",
    )
    parser.add_argument(
        "--aether-db-path",
        default=None,
        help="Optional path to Aether SQLite database file to checkpoint and dump into evidence",
    )
    parser.add_argument(
        "--require-clean-mx",
        action="store_true",
        default=False,
        help="Fail closed if MX Swarm working tree has uncommitted changes or untracked files",
    )
    return parser.parse_args()


if __name__ == "__main__":
    cli_args = parse_args()
    sys.exit(asyncio.run(run_canary(cli_args)))
