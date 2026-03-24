from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "tools" / "namecli.py"


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        capture_output=True,
        check=False,
        cwd=PROJECT_ROOT,
        text=True,
    )


def test_plain_output(tmp_path: Path) -> None:
    sample = tmp_path / "sample.txt"
    sample.write_text(" Zeta \nAlpha\nAlpha\n\n", encoding="utf-8")

    result = run_cli(str(sample))

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == [
        "name_count: 2",
        "names: Alpha, Zeta",
    ]


def test_json_output(tmp_path: Path) -> None:
    sample = tmp_path / "sample.txt"
    sample.write_text("Beta\n beta \nGamma\n", encoding="utf-8")

    result = run_cli(str(sample), "--json")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "name_count": 3,
        "names": ["Beta", "Gamma", "beta"],
    }


def test_missing_file_exits_non_zero(tmp_path: Path) -> None:
    missing = tmp_path / "missing.txt"

    result = run_cli(str(missing))

    assert result.returncode != 0
    error_output = (result.stderr or result.stdout).lower()
    assert "error" in error_output
    assert "missing.txt" in error_output
