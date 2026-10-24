from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.check_release_hygiene import (
    REPO_ROOT,
    scan_git_history,
    scan_release_boundary,
    scan_text_for_credentials,
    scan_tracked_credentials,
    verify_publication_destination,
)
from scripts.check_release_hygiene import (
    main as hygiene_main,
)


def test_reg_output_removed_and_tracked_tree_has_no_credentials() -> None:
    assert not (REPO_ROOT / "reg_output.txt").exists()
    findings = scan_tracked_credentials(REPO_ROOT)
    assert findings == []
    history_findings = scan_git_history(REPO_ROOT)
    assert history_findings == []


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


def test_sanitize_git_history_rejects_existing_or_overlapping_paths_without_data_loss(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source_repo = workspace / "source_repo"
    source_repo.mkdir(parents=True)
    ancestor_sentinel = workspace / "keep_ancestor.txt"
    ancestor_sentinel.write_text("ancestor-data\n", encoding="utf-8")

    git_env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
        "GIT_AUTHOR_NAME": "Test Author",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test Author",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(["git", "init", "-q", str(source_repo)], check=True, env=git_env)
    source_file = source_repo / "README.md"
    source_file.write_text("# Source Repo Intact\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source_repo), "add", "README.md"], check=True, env=git_env)
    subprocess.run(
        ["git", "-C", str(source_repo), "commit", "-q", "-m", "initial commit"],
        check=True,
        env=git_env,
    )
    head_before = subprocess.run(
        ["git", "-C", str(source_repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    existing_output = tmp_path / "existing_output"
    existing_output.mkdir()
    output_sentinel = existing_output / "sentinel.txt"
    output_sentinel.write_text("do-not-delete\n", encoding="utf-8")

    nested_output = source_repo / "subdir" / "nested_output"
    script = REPO_ROOT / "scripts" / "sanitize_git_history.sh"

    for invalid_output, expected_msg in (
        (source_repo, "Refusing overlapping source/output repository paths"),
        (workspace, "Refusing overlapping source/output repository paths"),
        (nested_output, "Refusing overlapping source/output repository paths"),
        (existing_output, "Refusing to overwrite existing output path"),
    ):
        proc = subprocess.run(
            ["bash", str(script), "--i-have-owner-approval", str(source_repo), str(invalid_output)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 2
        assert expected_msg in proc.stderr

    # Verify source repository, ancestor directory, and existing output directory remain completely intact
    assert source_file.read_text(encoding="utf-8") == "# Source Repo Intact\n"
    assert ancestor_sentinel.read_text(encoding="utf-8") == "ancestor-data\n"
    assert output_sentinel.read_text(encoding="utf-8") == "do-not-delete\n"
    assert not nested_output.exists()
    head_after = subprocess.run(
        ["git", "-C", str(source_repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head_after == head_before


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
    assert "Provenance and Release Authority Confirmation" in notices_text
    assert "Remaining release gate:" not in notices_text

    for forbidden_asset in ("x-logo.png", "xai-logo.png", "opengraph.jpg"):
        assert not (REPO_ROOT / "dashboard_v2" / "public" / forbidden_asset).exists()


def test_sanitize_git_history_purges_disallowed_paths_and_scrubs_allowed_path_historical_credentials(
    tmp_path: Path,
) -> None:
    source_repo = tmp_path / "source_repo"
    output_repo = tmp_path / "sanitized_repo"
    source_repo.mkdir()

    git_env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
        "GIT_AUTHOR_NAME": "Test Author",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test Author",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(["git", "init", "-q", str(source_repo)], check=True, env=git_env)
    (source_repo / "RELEASE_ALLOWLIST.json").write_text(
        (REPO_ROOT / "RELEASE_ALLOWLIST.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    # Commit 1: includes disallowed files AND a split credential inside an allowed path (tests/test_release_hygiene.py)
    split_prefix = "_".join(("moltbook", "sk")) + "_"
    split_suffix = "8" * 32
    (source_repo / "reg_output.txt").write_text(
        f'{{"api_key":"{split_prefix}{split_suffix}"}}\n',
        encoding="utf-8",
    )
    (source_repo / "dashboard_v2" / "public").mkdir(parents=True)
    (source_repo / "dashboard_v2" / "public" / "x-logo.png").write_bytes(b"\x89PNG\r\n")
    (source_repo / "tests").mkdir()
    (source_repo / "tests" / "test_release_hygiene.py").write_text(
        f'leaked = "{split_prefix}" + "{split_suffix}"\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(source_repo), "add", "."], check=True, env=git_env)
    subprocess.run(
        ["git", "-C", str(source_repo), "commit", "-q", "-m", "commit 1 with historical leaks"],
        check=True,
        env=git_env,
    )

    # Commit 2 (HEAD): removes disallowed files and fixes tests/test_release_hygiene.py
    (source_repo / "reg_output.txt").unlink()
    (source_repo / "dashboard_v2" / "public" / "x-logo.png").unlink()
    (source_repo / "tests" / "test_release_hygiene.py").write_text(
        'clean = "synthetic_placeholder"\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(source_repo), "add", "-A"], check=True, env=git_env)
    subprocess.run(
        ["git", "-C", str(source_repo), "commit", "-q", "-m", "commit 2 clean head"],
        check=True,
        env=git_env,
    )

    # Before sanitization, history scan catches both disallowed historical files and the split credential in commit 1
    pre_findings = scan_git_history(source_repo)
    pre_kinds = {str(f["kind"]) for f in pre_findings}
    assert "history_forbidden_credential_or_asset_file" in pre_kinds
    assert "moltbook_api_key" in pre_kinds

    # Run sanitize_git_history.sh with owner approval flag
    script = REPO_ROOT / "scripts" / "sanitize_git_history.sh"
    proc = subprocess.run(
        ["bash", str(script), "--i-have-owner-approval", str(source_repo), str(output_repo)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"Sanitizer failed: {proc.stderr}\n{proc.stdout}"
    assert scan_git_history(output_repo) == []


def test_scan_git_history_rejects_shallow_clone_instead_of_reporting_success(
    tmp_path: Path,
) -> None:
    full_repo = tmp_path / "full_repo"
    shallow_repo = tmp_path / "shallow_repo"
    full_repo.mkdir()

    git_env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
        "GIT_AUTHOR_NAME": "Test Author",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test Author",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(["git", "init", "-q", str(full_repo)], check=True, env=git_env)
    (full_repo / "RELEASE_ALLOWLIST.json").write_text(
        (REPO_ROOT / "RELEASE_ALLOWLIST.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    synthetic_key = "_".join(("moltbook", "sk", "7" * 32))
    (full_repo / "README.md").write_text(f"leaked={synthetic_key}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(full_repo), "add", "."], check=True, env=git_env)
    subprocess.run(
        ["git", "-C", str(full_repo), "commit", "-q", "-m", "commit 1 with secret"],
        check=True,
        env=git_env,
    )

    (full_repo / "README.md").write_text("# Clean README\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(full_repo), "add", "README.md"], check=True, env=git_env)
    subprocess.run(
        ["git", "-C", str(full_repo), "commit", "-q", "-m", "commit 2 clean"],
        check=True,
        env=git_env,
    )

    # Full checkout detects the historical credential
    full_findings = scan_git_history(full_repo)
    assert any(item["kind"] == "moltbook_api_key" for item in full_findings)

    # Shallow clone (--depth 1) must be rejected by scan_git_history rather than reporting success
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{full_repo}", str(shallow_repo)],
        check=True,
        env=git_env,
    )
    shallow_findings = scan_git_history(shallow_repo)
    assert any(item["kind"] == "shallow_repository_history" for item in shallow_findings)
    assert hygiene_main(["--repo-root", str(shallow_repo), "--scan-history"]) == 1


def test_scan_git_history_detects_duplicate_blob_forbidden_path_and_commit_message_credentials(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "dup_and_commit_msg_repo"
    repo.mkdir()

    git_env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
        "GIT_AUTHOR_NAME": "Test Author",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test Author",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=git_env)
    (repo / "RELEASE_ALLOWLIST.json").write_text(
        (REPO_ROOT / "RELEASE_ALLOWLIST.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    shared_content = "# Identical content across allowed and forbidden paths\n"
    (repo / "README.md").write_text(shared_content, encoding="utf-8")
    (repo / "reg_output.txt").write_text(shared_content, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md", "reg_output.txt"], check=True, env=git_env)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "commit 1 with duplicate-blob forbidden path"],
        check=True,
        env=git_env,
    )

    # Commit 2 deletes reg_output.txt (so README.md shares the same blob OID in HEAD)
    # and embeds a synthetic credential in the commit message itself.
    (repo / "reg_output.txt").unlink()
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=git_env)
    synthetic_commit_key = "_".join(("moltbook", "sk", "6" * 32))
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "commit",
            "-q",
            "-m",
            f"commit 2 removing file but leaking {synthetic_commit_key} in message",
        ],
        check=True,
        env=git_env,
    )

    findings = scan_git_history(repo)
    assert any(
        item["path"] == "reg_output.txt"
        and item["kind"] == "history_forbidden_credential_or_asset_file"
        for item in findings
    ), f"Expected duplicate-blob forbidden path finding, got: {findings}"
    assert any(
        str(item["path"]).startswith("commit:") and item["kind"] == "moltbook_api_key"
        for item in findings
    ), f"Expected commit-message credential finding, got: {findings}"



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


def test_verify_publication_destination_rejects_unapproved_or_stale_remote_refs(
    tmp_path: Path,
) -> None:
    empty_remote = tmp_path / "empty_remote.git"
    dirty_remote = tmp_path / "dirty_remote.git"
    clean_remote = tmp_path / "clean_remote.git"
    empty_remote.mkdir()
    dirty_remote.mkdir()
    clean_remote.mkdir()

    git_env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
        "GIT_AUTHOR_NAME": "Test Author",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test Author",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    for r in (empty_remote, dirty_remote, clean_remote):
        subprocess.run(["git", "init", "-q", "--bare", str(r)], check=True, env=git_env)

    # 1. Empty destination remote is accepted as ready to receive the 3 approved refs
    assert verify_publication_destination(REPO_ROOT, str(empty_remote)) == []

    # 2. Remote with an unapproved branch or missing approved refs is rejected
    subprocess.run(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "push",
            "-q",
            str(dirty_remote),
            "HEAD:refs/heads/main",
            "HEAD:refs/heads/legacy-unapproved-branch",
        ],
        check=True,
        env=git_env,
    )
    dirty_findings = verify_publication_destination(REPO_ROOT, str(dirty_remote))
    dirty_kinds = {str(item["kind"]) for item in dirty_findings}
    assert "unapproved_remote_ref" in dirty_kinds
    assert "missing_approved_remote_ref" in dirty_kinds

    # 3. Remote containing exclusively the 3 approved refs at HEAD passes verification
    subprocess.run(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "push",
            "-q",
            str(clean_remote),
            "HEAD:refs/heads/main",
            "HEAD:refs/heads/feature/aether-sprint-g-sync",
            "HEAD:refs/heads/release/public-prototype-rc1",
        ],
        check=True,
        env=git_env,
    )
    assert verify_publication_destination(REPO_ROOT, str(clean_remote)) == []


