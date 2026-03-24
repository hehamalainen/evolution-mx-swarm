import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.run_gate2_local_parallel as gate2_runner
from scripts.run_gate2_local_parallel import (
    _evaluate_dashboard_truth,
    _forced_local_execution_env,
    _verify_aether_submission,
)
from scripts.run_model_qualification import _model_safe_worker_cap
from shared.utils.config import MXConfig
from shared.utils.qualification import load_qualification_corpus, summarize_qualification


def test_load_qualification_corpus_reads_gate2_manifest() -> None:
    corpus = load_qualification_corpus("qualification/corpora/gate2_local_open_weight.json")

    assert corpus.name == "gate2_local_open_weight"
    assert corpus.default_runs == 3
    assert [slice.key for slice in corpus.slices] == ["jsonpeek", "envpeek", "tomlpeek"]
    assert all(slice.prerequisite_keys == [] for slice in corpus.slices)


def test_load_qualification_corpus_reads_dependency_chain_manifest() -> None:
    corpus = load_qualification_corpus("qualification/corpora/dependency_chain_local_open_weight.json")

    assert corpus.name == "dependency_chain_local_open_weight"
    assert [slice.key for slice in corpus.slices] == ["slugcore", "slugcli"]
    assert corpus.slices[0].prerequisite_keys == []
    assert corpus.slices[1].prerequisite_keys == ["slugcore"]


def test_summarize_qualification_separates_format_from_acceptance() -> None:
    corpus = load_qualification_corpus("qualification/corpora/gate2_local_open_weight.json")
    run_results = [
        SimpleNamespace(
            slices=[
                SimpleNamespace(terminal_reason="ERR_NO_DIFF", verification_passed=False),
                SimpleNamespace(terminal_reason="ERR_SYNTAX", verification_passed=False),
                SimpleNamespace(terminal_reason=None, verification_passed=True),
            ],
            duplicate_settlement_events=0,
            duplicate_claim_events=0,
            lifecycle_bug_events=[],
            false_success_rate=0.0,
            failure_reason=None,
        )
    ]

    summary = summarize_qualification(corpus, run_results, expected_runs=1)

    assert summary.total_slice_attempts == 3
    assert summary.format_compliant_attempts == 2
    assert summary.deterministic_acceptance_attempts == 1
    assert summary.format_compliance_rate == 2 / 3
    assert summary.deterministic_acceptance_rate == 1 / 3
    assert summary.passed is False


def test_forced_local_execution_env_masks_github_tokens(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "env-token")
    monkeypatch.setenv("MX_GITHUB_TOKEN", "mx-token")
    monkeypatch.delenv("MX_FORCE_LOCAL_MODE", raising=False)

    with _forced_local_execution_env():
        assert "GITHUB_TOKEN" not in os.environ
        assert "MX_GITHUB_TOKEN" not in os.environ
        assert os.environ["MX_FORCE_LOCAL_MODE"] == "1"

    assert os.environ["GITHUB_TOKEN"] == "env-token"
    assert os.environ["MX_GITHUB_TOKEN"] == "mx-token"
    assert "MX_FORCE_LOCAL_MODE" not in os.environ


def test_model_safe_worker_cap_limits_ollama_7b() -> None:
    config = MXConfig()
    config.worker.max_workers = 10
    config.llm.provider = "ollama"
    config.llm.model = "mistral:7b"

    assert _model_safe_worker_cap(config) == 1


@pytest.mark.asyncio
async def test_prewarm_local_llm_only_runs_for_ollama(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class DummyClient:
        async def chat(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(content="ok")

    monkeypatch.setattr(gate2_runner, "create_llm_client", lambda _cfg: DummyClient())

    config = MXConfig()
    config.llm.provider = "ollama"
    config.llm.model = "mistral:7b"
    await gate2_runner._prewarm_local_llm(config)

    assert len(calls) == 1
    assert calls[0]["max_tokens"] == 8

    config.llm.provider = "anthropic"
    await gate2_runner._prewarm_local_llm(config)
    assert len(calls) == 1


def test_dashboard_truth_allows_all_failure_runs_without_submission_events(tmp_path: Path) -> None:
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    task_state_log = runtime / "task_state_events.jsonl"
    task_state_log.write_text(
        "\n".join(
            [
                '{"task_id":"t1","state_to":"dead_letter","timestamp":"2026-03-09T00:00:00+00:00"}',
                '{"task_id":"t2","state_to":"settled_nack","timestamp":"2026-03-09T00:00:01+00:00"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (runtime / "submission_events.jsonl").write_text("", encoding="utf-8")

    assert _evaluate_dashboard_truth(tmp_path, {"t1", "t2"}) is True


def test_verify_aether_submission_accepts_verified_promoted_payload() -> None:
    passed, changed_files, error = _verify_aether_submission(
        submission_event={
            "provider": "aether",
            "canonical_status": "promoted",
            "landing_verification_status": "verified",
            "affected_paths": ["tools/jsonpeek.py"],
        },
        implementation_path="tools/jsonpeek.py",
    )

    assert passed is True
    assert changed_files == ["tools/jsonpeek.py"]
    assert error is None


def test_verify_aether_submission_rejects_missing_landing_verification() -> None:
    passed, changed_files, error = _verify_aether_submission(
        submission_event={
            "provider": "aether",
            "canonical_status": "promoted",
            "landing_verification_status": "failed",
            "landing_verification_detail": "missing landed path evidence",
            "affected_paths": ["tools/jsonpeek.py"],
        },
        implementation_path="tools/jsonpeek.py",
    )

    assert passed is False
    assert changed_files == ["tools/jsonpeek.py"]
    assert error == "missing landed path evidence"
