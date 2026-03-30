import json
import subprocess
from pathlib import Path

from scripts.run_mock_sink_live_proof import _is_mock_merge_diff_gap, _patch_config


def test_patch_config_switches_to_local_mock_github(tmp_path: Path) -> None:
    config_path = tmp_path / "mx.config.json"
    config_path.write_text(
        json.dumps(
            {
                "vcs": {
                    "provider": "aether",
                    "canonical_enforcement": True,
                    "post_landing_verification_required": True,
                    "reconciliation_enabled": True,
                },
                "github": {
                    "repo_owner": "old",
                    "repo_name": "old",
                    "default_branch": "dev",
                    "token": "secret:x",
                },
                "worker": {},
            }
        ),
        encoding="utf-8",
    )

    _patch_config(config_path)
    payload = json.loads(config_path.read_text(encoding="utf-8"))

    assert payload["vcs"]["provider"] == "github"
    assert payload["vcs"]["canonical_enforcement"] is False
    assert payload["vcs"]["post_landing_verification_required"] is True
    assert payload["vcs"]["reconciliation_enabled"] is False
    assert payload["github"]["repo_owner"] == "local"
    assert payload["github"]["repo_name"] == "swarm"
    assert payload["github"]["default_branch"] == "main"
    assert payload["github"]["token"] is None
    assert payload["worker"]["max_workers"] == 1


def test_copy_worktree_initializes_main_branch(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "mx.config.json").write_text("{}", encoding="utf-8")
    (source / "scripts").mkdir()
    (source / "scripts" / "mini.py").write_text("print('ok')\n", encoding="utf-8")

    from scripts.run_aether_live_proof import _copy_worktree

    target = tmp_path / "target"
    _copy_worktree(source, target)

    branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=str(target),
        check=True,
        capture_output=True,
        text=True,
    )
    assert branch.stdout.strip() == "main"


def test_is_mock_merge_diff_gap_identifies_expected_local_merge_shape() -> None:
    payload = {
        "proof": {"failure_point": "artifact_verification"},
        "run": {
            "slices": [
                {
                    "failure_reason": "unexpected branch diff: []",
                }
            ]
        },
    }

    assert _is_mock_merge_diff_gap(payload) is True
