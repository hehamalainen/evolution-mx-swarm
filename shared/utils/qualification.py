"""Reusable model-qualification corpus loading and scoring."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shared.schemas import NackReason

FORMAT_NONCOMPLIANT_REASONS = {
    NackReason.ERR_NO_DIFF.value,
    NackReason.ERR_REFUSAL.value,
}


@dataclass(frozen=True)
class QualificationSlice:
    key: str
    prd_path: str
    implementation_path: str
    test_paths: list[str]
    prerequisite_keys: list[str]


@dataclass(frozen=True)
class QualificationThresholds:
    min_format_compliance_rate: float
    min_deterministic_acceptance_rate: float
    max_false_success_rate: float
    max_duplicate_settlement_events: int
    max_duplicate_claim_events: int
    max_lifecycle_bug_events: int


@dataclass(frozen=True)
class QualificationCorpus:
    name: str
    description: str
    model_pool: str
    default_runs: int
    default_workers: int
    timeout_seconds: int
    thresholds: QualificationThresholds
    slices: list[QualificationSlice]


@dataclass(frozen=True)
class QualificationSummary:
    corpus_name: str
    model_pool: str
    expected_runs: int
    completed_runs: int
    total_slice_attempts: int
    format_compliant_attempts: int
    deterministic_acceptance_attempts: int
    false_success_rate: float
    duplicate_settlement_events: int
    duplicate_claim_events: int
    lifecycle_bug_events: int
    format_compliance_rate: float
    deterministic_acceptance_rate: float
    passed: bool
    failure_reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "corpus_name": self.corpus_name,
            "model_pool": self.model_pool,
            "expected_runs": self.expected_runs,
            "completed_runs": self.completed_runs,
            "total_slice_attempts": self.total_slice_attempts,
            "format_compliant_attempts": self.format_compliant_attempts,
            "deterministic_acceptance_attempts": self.deterministic_acceptance_attempts,
            "false_success_rate": self.false_success_rate,
            "duplicate_settlement_events": self.duplicate_settlement_events,
            "duplicate_claim_events": self.duplicate_claim_events,
            "lifecycle_bug_events": self.lifecycle_bug_events,
            "format_compliance_rate": self.format_compliance_rate,
            "deterministic_acceptance_rate": self.deterministic_acceptance_rate,
            "passed": self.passed,
            "failure_reasons": self.failure_reasons,
        }


def load_qualification_corpus(path: str | Path) -> QualificationCorpus:
    corpus_path = Path(path)
    with open(corpus_path) as handle:
        payload = json.load(handle)

    thresholds = payload.get("thresholds", {})
    slices_payload = payload.get("slices", [])
    slices = [
        QualificationSlice(
            key=str(item["key"]),
            prd_path=str(item["prd_path"]),
            implementation_path=str(item["implementation_path"]),
            test_paths=[str(test_path) for test_path in item.get("test_paths", [])],
            prerequisite_keys=[str(key) for key in item.get("prerequisite_keys", [])],
        )
        for item in slices_payload
    ]

    return QualificationCorpus(
        name=str(payload["name"]),
        description=str(payload.get("description", "")),
        model_pool=str(payload.get("model_pool", "local_open_weight")),
        default_runs=int(payload.get("default_runs", 3)),
        default_workers=int(payload.get("default_workers", 5)),
        timeout_seconds=int(payload.get("timeout_seconds", 1200)),
        thresholds=QualificationThresholds(
            min_format_compliance_rate=float(thresholds.get("min_format_compliance_rate", 1.0)),
            min_deterministic_acceptance_rate=float(thresholds.get("min_deterministic_acceptance_rate", 1.0)),
            max_false_success_rate=float(thresholds.get("max_false_success_rate", 0.0)),
            max_duplicate_settlement_events=int(thresholds.get("max_duplicate_settlement_events", 0)),
            max_duplicate_claim_events=int(thresholds.get("max_duplicate_claim_events", 0)),
            max_lifecycle_bug_events=int(thresholds.get("max_lifecycle_bug_events", 0)),
        ),
        slices=slices,
    )


def summarize_qualification(
    corpus: QualificationCorpus,
    run_results: list[Any],
    *,
    expected_runs: int | None = None,
) -> QualificationSummary:
    expected = expected_runs or corpus.default_runs
    total_slice_attempts = 0
    format_compliant_attempts = 0
    deterministic_acceptance_attempts = 0
    duplicate_settlement_events = 0
    duplicate_claim_events = 0
    lifecycle_bug_events = 0
    false_success_rates: list[float] = []
    failure_reasons: list[str] = []

    for result in run_results:
        total_slice_attempts += len(result.slices)
        duplicate_settlement_events += int(result.duplicate_settlement_events)
        duplicate_claim_events += int(result.duplicate_claim_events)
        lifecycle_bug_events += len(getattr(result, "lifecycle_bug_events", []))
        false_success_rates.append(float(result.false_success_rate))
        if getattr(result, "failure_reason", None):
            failure_reasons.append(str(result.failure_reason))

        for slice_result in result.slices:
            terminal_reason = str(slice_result.terminal_reason or "").strip()
            if terminal_reason not in FORMAT_NONCOMPLIANT_REASONS:
                format_compliant_attempts += 1
            if bool(slice_result.verification_passed):
                deterministic_acceptance_attempts += 1

    false_success_rate = max(false_success_rates) if false_success_rates else 0.0
    format_compliance_rate = (
        format_compliant_attempts / total_slice_attempts if total_slice_attempts else 0.0
    )
    deterministic_acceptance_rate = (
        deterministic_acceptance_attempts / total_slice_attempts if total_slice_attempts else 0.0
    )

    thresholds = corpus.thresholds
    if len(run_results) != expected:
        failure_reasons.append(
            f"completed runs ({len(run_results)}) did not match expected runs ({expected})"
        )
    if format_compliance_rate < thresholds.min_format_compliance_rate:
        failure_reasons.append(
            f"format compliance {format_compliance_rate:.2%} below threshold {thresholds.min_format_compliance_rate:.2%}"
        )
    if deterministic_acceptance_rate < thresholds.min_deterministic_acceptance_rate:
        failure_reasons.append(
            "deterministic acceptance "
            f"{deterministic_acceptance_rate:.2%} below threshold "
            f"{thresholds.min_deterministic_acceptance_rate:.2%}"
        )
    if false_success_rate > thresholds.max_false_success_rate:
        failure_reasons.append(
            f"false success rate {false_success_rate:.4f} exceeded {thresholds.max_false_success_rate:.4f}"
        )
    if duplicate_settlement_events > thresholds.max_duplicate_settlement_events:
        failure_reasons.append(
            f"duplicate settlement events {duplicate_settlement_events} exceeded {thresholds.max_duplicate_settlement_events}"
        )
    if duplicate_claim_events > thresholds.max_duplicate_claim_events:
        failure_reasons.append(
            f"duplicate claim events {duplicate_claim_events} exceeded {thresholds.max_duplicate_claim_events}"
        )
    if lifecycle_bug_events > thresholds.max_lifecycle_bug_events:
        failure_reasons.append(
            f"lifecycle bug events {lifecycle_bug_events} exceeded {thresholds.max_lifecycle_bug_events}"
        )

    return QualificationSummary(
        corpus_name=corpus.name,
        model_pool=corpus.model_pool,
        expected_runs=expected,
        completed_runs=len(run_results),
        total_slice_attempts=total_slice_attempts,
        format_compliant_attempts=format_compliant_attempts,
        deterministic_acceptance_attempts=deterministic_acceptance_attempts,
        false_success_rate=false_success_rate,
        duplicate_settlement_events=duplicate_settlement_events,
        duplicate_claim_events=duplicate_claim_events,
        lifecycle_bug_events=lifecycle_bug_events,
        format_compliance_rate=format_compliance_rate,
        deterministic_acceptance_rate=deterministic_acceptance_rate,
        passed=not failure_reasons,
        failure_reasons=failure_reasons,
    )
