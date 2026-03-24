from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "tools" / "tomlpeek.py"


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        capture_output=True,
        check=False,
        cwd=PROJECT_ROOT,
        text=True,
    )


def test_plain_text_output(tmp_path: Path) -> None:
    sample = tmp_path / "sample.toml"
    sample.write_text('zeta = 1\nalpha = 2\nmid = 3\n', encoding="utf-8")

    result = run_cli(str(sample))

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == [
        "key_count: 3",
        "keys: alpha, mid, zeta",
    ]


def test_json_output(tmp_path: Path) -> None:
    sample = tmp_path / "sample.toml"
    sample.write_text('beta = true\nalpha = false\n', encoding="utf-8")

    result = run_cli(str(sample), "--json")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "key_count": 2,
        "keys": ["alpha", "beta"],
    }


def test_missing_file_exits_non_zero(tmp_path: Path) -> None:
    missing = tmp_path / "missing.toml"

    result = run_cli(str(missing))

    assert result.returncode != 0
    error_output = (result.stderr or result.stdout).lower()
    assert "error" in error_output
    assert "missing.toml" in error_output


def test_invalid_toml_exits_non_zero(tmp_path: Path) -> None:
    sample = tmp_path / "broken.toml"
    sample.write_text("alpha = \n", encoding="utf-8")

    result = run_cli(str(sample))

    assert result.returncode != 0
    error_output = (result.stderr or result.stdout).lower()
    assert "invalid toml" in error_output

