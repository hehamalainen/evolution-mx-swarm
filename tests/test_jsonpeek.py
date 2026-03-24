from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "tools" / "jsonpeek.py"


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        capture_output=True,
        check=False,
        cwd=PROJECT_ROOT,
        text=True,
    )


def test_plain_text_output(tmp_path: Path) -> None:
    sample = tmp_path / "sample.json"
    sample.write_text('{"zeta": 1, "alpha": 2, "mid": 3}', encoding="utf-8")

    result = run_cli(str(sample))

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == [
        "key_count: 3",
        "keys: alpha, mid, zeta",
    ]


def test_json_output(tmp_path: Path) -> None:
    sample = tmp_path / "sample.json"
    sample.write_text('{"beta": true, "alpha": false}', encoding="utf-8")

    result = run_cli(str(sample), "--json")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "key_count": 2,
        "keys": ["alpha", "beta"],
    }


def test_missing_file_exits_non_zero(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"

    result = run_cli(str(missing))

    assert result.returncode != 0
    error_output = (result.stderr or result.stdout).lower()
    assert "error" in error_output
    assert "missing.json" in error_output


def test_invalid_json_exits_non_zero(tmp_path: Path) -> None:
    sample = tmp_path / "broken.json"
    sample.write_text('{"alpha": ', encoding="utf-8")

    result = run_cli(str(sample))

    assert result.returncode != 0
    error_output = (result.stderr or result.stdout).lower()
    assert "invalid json" in error_output


def test_empty_file_exits_non_zero(tmp_path: Path) -> None:
    sample = tmp_path / "empty.json"
    sample.write_text("", encoding="utf-8")

    result = run_cli(str(sample))

    assert result.returncode != 0
    error_output = (result.stderr or result.stdout).lower()
    assert "invalid json" in error_output


def test_non_object_json_exits_non_zero(tmp_path: Path) -> None:
    sample = tmp_path / "array.json"
    sample.write_text('["alpha", "beta"]', encoding="utf-8")

    result = run_cli(str(sample))

    assert result.returncode != 0
    error_output = (result.stderr or result.stdout).lower()
    assert "invalid json" in error_output
