"""Pytest configuration separating qualification benchmark scaffolds from default regression tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# These test files target intentional NotImplementedError qualification benchmark scaffolds
# in tools/*.py that MX workers implement during live or canned proof runs.
# They are excluded from default regression runs (`pytest` / `pytest tests/`) unless
# explicitly targeted on the CLI (e.g. `pytest tests/test_jsonpeek.py`) or opted into
# via `--run-benchmark-scaffolds` / `MX_RUN_BENCHMARK_SCAFFOLDS=1`.
BENCHMARK_SCAFFOLD_TEST_FILES = {
    "test_jsonpeek.py",
    "test_envpeek.py",
    "test_tomlpeek.py",
    "test_namecore.py",
    "test_namecli.py",
    "test_slugcore.py",
    "test_slugcli.py",
}


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-benchmark-scaffolds",
        action="store_true",
        default=False,
        help="Include unimplemented qualification benchmark scaffold tests in collection.",
    )


def _explicitly_targets_file(config: pytest.Config, filename: str) -> bool:
    for raw_arg in config.invocation_params.args:
        arg_path = str(raw_arg).split("::", 1)[0]
        if Path(arg_path).name == filename:
            return True
    return False


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool:
    name = collection_path.name
    if name == "test_run_suite.py" and os.environ.get("MX_ALLOW_DESTRUCTIVE_DB_TESTS") != "1":
        return True
    if name in BENCHMARK_SCAFFOLD_TEST_FILES:
        if config.getoption("--run-benchmark-scaffolds"):
            return False
        if os.environ.get("MX_RUN_BENCHMARK_SCAFFOLDS") == "1":
            return False
        if _explicitly_targets_file(config, name):
            return False
        return True
    return False
