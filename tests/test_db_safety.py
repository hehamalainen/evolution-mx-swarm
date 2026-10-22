from __future__ import annotations

import pytest

from scripts.check_release_hygiene import REPO_ROOT
from shared.utils.db_safety import assert_disposable_test_database


def test_destructive_db_runner_files_excluded_from_release_tree() -> None:
    assert not (REPO_ROOT / "run_temp.py").exists()
    assert not (REPO_ROOT / "tests" / "test_run_suite.py").exists()


def test_db_safety_requires_explicit_opt_in() -> None:
    with pytest.raises(RuntimeError, match="MX_ALLOW_DESTRUCTIVE_DB_TESTS=1"):
        assert_disposable_test_database(
            "postgresql://localhost:5432/aether_disposable_test",
            env={},
        )


def test_db_safety_rejects_missing_or_default_databases() -> None:
    env = {"MX_ALLOW_DESTRUCTIVE_DB_TESTS": "1"}
    with pytest.raises(RuntimeError, match="Missing explicit disposable database URL"):
        assert_disposable_test_database(None, env=env)

    for forbidden_url in (
        "postgresql://localhost:5432/postgres",
        "postgresql://localhost:5432/aether",
        "postgresql://127.0.0.1:5432/template1",
        "postgresql://localhost:5432/aether_qual",
    ):
        with pytest.raises(RuntimeError):
            assert_disposable_test_database(forbidden_url, env=env)


def test_db_safety_accepts_explicit_disposable_database_and_memory_sqlite() -> None:
    env = {
        "MX_ALLOW_DESTRUCTIVE_DB_TESTS": "1",
        "MX_DISPOSABLE_POSTGRES_URL": "postgresql://localhost:5432/mx_qualification_test",
    }
    assert (
        assert_disposable_test_database(env=env)
        == "postgresql://localhost:5432/mx_qualification_test"
    )
    assert assert_disposable_test_database("sqlite:///:memory:", env=env) == "sqlite:///:memory:"
