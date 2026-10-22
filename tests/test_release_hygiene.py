from __future__ import annotations

import subprocess

from scripts.check_release_hygiene import (
    REPO_ROOT,
    scan_release_boundary,
    scan_text_for_credentials,
    scan_tracked_credentials,
)


def test_reg_output_removed_and_tracked_tree_has_no_credentials() -> None:
    assert not (REPO_ROOT / "reg_output.txt").exists()
    findings = scan_tracked_credentials(REPO_ROOT)
    assert findings == []


def test_credential_scanner_detects_provider_tokens_claim_urls_and_private_keys() -> None:
    synthetic_api_key = "_".join(("moltbook", "sk", "0" * 32))
    synthetic_claim = "_".join(("moltbook", "claim", "1" * 32))
    dashes = "-" * 5
    key_label = " ".join(("BEGIN", "OPENSSH", "PRIVATE", "KEY"))
    private_key_header = f"{dashes}{key_label}{dashes}"
    sample = (
        f'{{"api_key":"{synthetic_api_key}",'
        f'"claim_url":"https://moltbook.com/claim/{synthetic_claim}"}}\n'
        f"{private_key_header}\n"
    )
    findings = scan_text_for_credentials(sample, "sample.txt")
    kinds = {str(item["kind"]) for item in findings}
    assert "moltbook_api_key" in kinds
    assert "moltbook_claim_token" in kinds
    assert "private_key_block" in kinds

    # Also verify split/concatenated literals are detected
    split_prefix = "_".join(("moltbook", "sk")) + "_"
    split_suffix = "9" * 32
    split_sample = f'token = "{split_prefix}" + "{split_suffix}"\n'
    split_findings = scan_text_for_credentials(split_sample, "split.py")
    assert any(item["kind"] == "moltbook_api_key" for item in split_findings)


def test_sanitize_git_history_script_requires_explicit_owner_approval() -> None:
    script = REPO_ROOT / "scripts" / "sanitize_git_history.sh"
    proc = subprocess.run(
        ["bash", str(script)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert "Refusing to rewrite git history without explicit approval" in proc.stderr


def test_license_and_third_party_notices_present_and_brand_logos_removed() -> None:
    license_path = REPO_ROOT / "LICENSE"
    notices_path = REPO_ROOT / "THIRD_PARTY_NOTICES.md"
    assert license_path.is_file()
    assert notices_path.is_file()

    license_text = license_path.read_text(encoding="utf-8")
    assert "MIT License" in license_text
    assert "The above copyright notice and this permission notice shall be included" in license_text

    notices_text = notices_path.read_text(encoding="utf-8")
    assert "caniuse-lite" in notices_text
    assert "CC-BY-4.0" in notices_text
    assert "lightningcss" in notices_text
    assert "MPL-2.0" in notices_text
    assert "dashboard_v2" in notices_text

    for forbidden_asset in ("x-logo.png", "xai-logo.png", "opengraph.jpg"):
        assert not (REPO_ROOT / "dashboard_v2" / "public" / forbidden_asset).exists()


def test_release_boundary_allowlist_and_private_path_hygiene() -> None:
    assert (REPO_ROOT / "RELEASE_ALLOWLIST.json").is_file()
    findings = scan_release_boundary(REPO_ROOT)
    assert findings == []


def test_contributor_and_security_guidance_present_and_readme_reproducible() -> None:
    contributing = REPO_ROOT / "CONTRIBUTING.md"
    security = REPO_ROOT / "SECURITY.md"
    readme = REPO_ROOT / "README.md"
    assert contributing.is_file()
    assert security.is_file()
    assert readme.is_file()

    readme_text = readme.read_text(encoding="utf-8")
    assert "mx-swarm/requirements.txt" not in readme_text
    assert "mx-swarm.cli.main" not in readme_text
    assert 'pip install -e ".[dev]"' in readme_text
    assert "python -m cli.main" in readme_text

    contributing_text = contributing.read_text(encoding="utf-8")
    assert "run_mock_sink_live_proof.py" in contributing_text
    assert "pytest" in contributing_text

    security_text = security.read_text(encoding="utf-8")
    assert "Security Advisories" in security_text
    assert "Trusted-Code Execution Boundary" in security_text
    assert "evolution-mx-swarm/security/advisories/new" in security_text
    assert "Evolution-Coding" not in security_text
    assert "Evolution-Coding" not in (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    import re

    referenced_paths = set(
        re.findall(
            r"`((?:docs|scripts|cli|planner|worker|judge|shared|dashboard|dashboard_v2|proof_fixtures)/[A-Za-z0-9_./-]*)`",
            contributing_text + "\n" + security_text,
        )
    )
    assert referenced_paths
    for rel_path in referenced_paths:
        assert (REPO_ROOT / rel_path).exists(), f"Broken path reference in docs: {rel_path}"


