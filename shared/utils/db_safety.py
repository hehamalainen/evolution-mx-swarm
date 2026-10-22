"""Fail-closed database safety guards for destructive test harnesses."""

from __future__ import annotations

import os
from collections.abc import Mapping
from urllib.parse import urlparse

PROTECTED_DATABASE_NAMES: frozenset[str] = frozenset(
    {
        "",
        "postgres",
        "template0",
        "template1",
        "aether",
        "defaultdb",
        "main",
        "master",
        "prod",
        "production",
        "dev",
        "development",
    }
)

ALLOWED_DISPOSABLE_SUFFIXES: tuple[str, ...] = (
    "_test",
    "_disposable",
    "_ephemeral",
    "_scratch",
)


def assert_disposable_test_database(
    db_url: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """Validate that a database URL is explicitly opted-in and safe for destructive operations.

    Raises RuntimeError if:
    - MX_ALLOW_DESTRUCTIVE_DB_TESTS is not set to "1"
    - No explicit database URL is provided via argument or MX_DISPOSABLE_POSTGRES_URL
    - The target database is a default/protected database or lacks a disposable suffix
    """
    environ = env if env is not None else os.environ
    if environ.get("MX_ALLOW_DESTRUCTIVE_DB_TESTS") != "1":
        raise RuntimeError(
            "Destructive database tests are disabled by default. "
            "Set MX_ALLOW_DESTRUCTIVE_DB_TESTS=1 and MX_DISPOSABLE_POSTGRES_URL to an "
            "explicitly provisioned disposable database."
        )

    candidate_url = (db_url or environ.get("MX_DISPOSABLE_POSTGRES_URL") or "").strip()
    if not candidate_url:
        raise RuntimeError(
            "Missing explicit disposable database URL. "
            "Provide MX_DISPOSABLE_POSTGRES_URL; implicit local default databases are forbidden."
        )

    if candidate_url == "sqlite:///:memory:":
        return candidate_url

    parsed = urlparse(candidate_url)
    scheme = (parsed.scheme or "").lower()
    if not scheme.startswith(("postgresql", "postgres", "sqlite")):
        raise RuntimeError(f"Unsupported database scheme for disposable test check: {scheme!r}")

    db_name = parsed.path.lstrip("/").strip().lower()
    if db_name in PROTECTED_DATABASE_NAMES:
        raise RuntimeError(
            f"Refusing destructive operation on protected or default database: {db_name or '<empty>'}"
        )

    if not db_name.endswith(ALLOWED_DISPOSABLE_SUFFIXES):
        raise RuntimeError(
            f"Database name {db_name!r} must end with one of {ALLOWED_DISPOSABLE_SUFFIXES} "
            "to confirm disposable provisioning."
        )

    return candidate_url
