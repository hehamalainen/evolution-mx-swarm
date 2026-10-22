"""Regression tests ensuring CI workflow gates fail closed on lint, type, test, and security errors."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def test_ci_workflow_has_no_failure_swallowing_fallbacks() -> None:
    """No CI gate step may swallow failures with '|| true' or '|| echo'."""
    content = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "|| true" not in content
    assert "|| echo" not in content

    parsed = yaml.safe_load(content)
    jobs = parsed["jobs"]
    for required_job in ("lint", "test", "security"):
        assert required_job in jobs
        for step in jobs[required_job]["steps"]:
            if "run" in step:
                assert not step.get("continue-on-error", False), (
                    f"Gate step {step.get('name')} in job {required_job} must not set continue-on-error"
                )


def test_deliberately_failing_checks_exit_nonzero(tmp_path: Path) -> None:
    """Deliberately failing pytest and bandit invocations must fail with non-zero exit codes."""
    bad_test = tmp_path / "test_deliberate_fail.py"
    bad_test.write_text("def test_fail():\n    assert False\n", encoding="utf-8")
    pytest_proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(bad_test)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert pytest_proc.returncode != 0

    bad_sec = tmp_path / "insecure_sample.py"
    bad_sec.write_text(
        "import urllib.request\nurllib.request.urlopen('file:///etc/passwd')\n",
        encoding="utf-8",
    )
    bandit_proc = subprocess.run(
        [sys.executable, "-m", "bandit", "-ll", "-ii", str(bad_sec)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert bandit_proc.returncode != 0


def test_ci_pip_audit_command_validates_against_supplied_requirements() -> None:
    """pip-audit step must not use --disable-pip or --no-deps against unpinned/unhashed requirements.txt."""
    import shlex

    parsed = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    security_steps = parsed["jobs"]["security"]["steps"]
    audit_steps = [
        step
        for step in security_steps
        if str(step.get("run", "")).strip().startswith("pip-audit")
    ]
    assert len(audit_steps) == 1
    tokens = shlex.split(audit_steps[0]["run"])
    assert tokens[0] == "pip-audit"

    if "--requirement" in tokens or "-r" in tokens:
        flag_idx = tokens.index("--requirement") if "--requirement" in tokens else tokens.index("-r")
        req_file = REPO_ROOT / tokens[flag_idx + 1]
        assert req_file.exists()
        req_lines = [
            line.strip()
            for line in req_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        all_hashed = all("--hash=" in line for line in req_lines)
        all_exact_pins = all("==" in line for line in req_lines)
        if "--disable-pip" in tokens:
            assert all_hashed or ("--no-deps" in tokens and all_exact_pins), (
                "pip-audit --disable-pip requires hashed requirements or --no-deps with exact pins"
            )
        if "--no-deps" in tokens:
            assert all_exact_pins, "pip-audit --no-deps requires exact '==' version pins"

