#!/usr/bin/env python3
# ruff: noqa: E402
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(REPO_ROOT))

from shared.utils import MXConfig
from shared.utils.qualification import load_qualification_corpus

GATE_PROFILES = {
    "A": {"workers": 100, "duration_minutes": 60},
    "B": {"workers": 300, "duration_minutes": 120},
}


@dataclass(frozen=True)
class GCPValidation:
    credentials_present: bool
    project_id_present: bool
    pubsub_library_installed: bool
    queue_provider_supported: bool
    frontier_llm_configured: bool
    frontier_llm_is_gemini: bool
    frontier_budget_configured: bool
    frontier_concurrency_configured: bool
    freeze_mode_valid: bool


@dataclass(frozen=True)
class GateABPlan:
    gate: str
    project_id: str
    topic_id: str
    subscription_id: str
    worker_count: int
    duration_minutes: int
    corpus_name: str
    slice_keys: list[str]
    queue_provider: str
    keda_polling_interval_seconds: int
    keda_cooldown_period_seconds: int
    keda_target_backlog_per_pod: int
    deployment_min_replicas: int
    deployment_max_replicas: int
    validation: GCPValidation


def _validate_environment(project_id: str, config: MXConfig) -> GCPValidation:
    credentials_present = bool(
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
    )
    try:
        pubsub_library_installed = importlib.util.find_spec("google.cloud.pubsub_v1") is not None
    except ModuleNotFoundError:
        pubsub_library_installed = False
    frontier_llm_configured = config.frontier_llm is not None and bool(config.frontier_llm.model)
    frontier_llm_is_gemini = frontier_llm_configured and config.frontier_llm.provider.lower() == "gemini"
    frontier_budget_configured = config.budget.frontier_daily_usd_cap > 0 and config.budget.daily_usd_cap > 0
    frontier_concurrency_configured = config.budget.max_frontier_concurrency > 0
    freeze_mode_valid = config.budget.freeze_mode == "frontier_only"
    return GCPValidation(
        credentials_present=credentials_present,
        project_id_present=bool(project_id),
        pubsub_library_installed=pubsub_library_installed,
        queue_provider_supported=True,
        frontier_llm_configured=frontier_llm_configured,
        frontier_llm_is_gemini=frontier_llm_is_gemini,
        frontier_budget_configured=frontier_budget_configured,
        frontier_concurrency_configured=frontier_concurrency_configured,
        freeze_mode_valid=freeze_mode_valid,
    )


def build_gate_ab_plan(
    *,
    gate: str,
    project_id: str,
    corpus_path: Path,
    config_path: Path | None = None,
    topic_prefix: str = "mx-gate",
    subscription_prefix: str = "mx-gate",
) -> GateABPlan:
    gate_key = gate.upper()
    if gate_key not in GATE_PROFILES:
        raise ValueError(f"Unsupported gate {gate}. Expected one of: {', '.join(sorted(GATE_PROFILES))}")

    profile = GATE_PROFILES[gate_key]
    corpus = load_qualification_corpus(corpus_path)
    config = MXConfig.load_or_create(str(config_path or (REPO_ROOT / "mx.config.cloud.json")))
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    topic_id = f"{topic_prefix.lower()}-{gate_key.lower()}-{timestamp}"
    subscription_id = f"{subscription_prefix.lower()}-{gate_key.lower()}-{timestamp}"
    worker_count = int(profile["workers"])

    return GateABPlan(
        gate=gate_key,
        project_id=project_id,
        topic_id=topic_id,
        subscription_id=subscription_id,
        worker_count=worker_count,
        duration_minutes=int(profile["duration_minutes"]),
        corpus_name=corpus.name,
        slice_keys=[slice.key for slice in corpus.slices],
        queue_provider="gcp",
        keda_polling_interval_seconds=15,
        keda_cooldown_period_seconds=120,
        keda_target_backlog_per_pod=1,
        deployment_min_replicas=max(1, worker_count // 10),
        deployment_max_replicas=worker_count,
        validation=_validate_environment(project_id, config),
    )


def _render_scaledobject_yaml(plan: GateABPlan) -> str:
    return f"""apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: mx-gate-{plan.gate.lower()}-workers
spec:
  scaleTargetRef:
    name: mx-worker
  pollingInterval: {plan.keda_polling_interval_seconds}
  cooldownPeriod: {plan.keda_cooldown_period_seconds}
  minReplicaCount: {plan.deployment_min_replicas}
  maxReplicaCount: {plan.deployment_max_replicas}
  triggers:
    - type: gcp-pubsub
      metadata:
        mode: SubscriptionSize
        subscriptionName: {plan.subscription_id}
        value: "{plan.keda_target_backlog_per_pod}"
"""


def _render_deployment_env(plan: GateABPlan) -> dict[str, str]:
    return {
        "MX_QUEUE_PROVIDER": "gcp",
        "MX_GCP_PROJECT_ID": plan.project_id,
        "MX_GCP_TOPIC_ID": plan.topic_id,
        "MX_GCP_SUBSCRIPTION_ID": plan.subscription_id,
    }


def _write_report(report_dir: Path, plan: GateABPlan, corpus_path: Path) -> None:
    json_path = report_dir / "gcp_gate_ab_plan.json"
    md_path = report_dir / "gcp_gate_ab_plan.md"
    scaledobject_path = report_dir / "keda_scaledobject.yaml"
    env_path = report_dir / "deployment_env.json"

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "corpus_path": str(corpus_path),
        "plan": asdict(plan),
        "deployment_env": _render_deployment_env(plan),
    }
    with open(json_path, "w") as handle:
        json.dump(payload, handle, indent=2)
    with open(scaledobject_path, "w") as handle:
        handle.write(_render_scaledobject_yaml(plan))
    with open(env_path, "w") as handle:
        json.dump(payload["deployment_env"], handle, indent=2)

    lines = [
        "# GCP Gate A/B Staging Plan",
        "",
        f"- gate: `{plan.gate}`",
        f"- project_id: `{plan.project_id}`",
        f"- topic_id: `{plan.topic_id}`",
        f"- subscription_id: `{plan.subscription_id}`",
        f"- worker_count: `{plan.worker_count}`",
        f"- duration_minutes: `{plan.duration_minutes}`",
        f"- corpus_name: `{plan.corpus_name}`",
        f"- slices: `{', '.join(plan.slice_keys)}`",
        "",
        "## Validation",
        "",
        f"- credentials_present: `{plan.validation.credentials_present}`",
        f"- project_id_present: `{plan.validation.project_id_present}`",
        f"- pubsub_library_installed: `{plan.validation.pubsub_library_installed}`",
        f"- queue_provider_supported: `{plan.validation.queue_provider_supported}`",
        f"- frontier_llm_configured: `{plan.validation.frontier_llm_configured}`",
        f"- frontier_llm_is_gemini: `{plan.validation.frontier_llm_is_gemini}`",
        f"- frontier_budget_configured: `{plan.validation.frontier_budget_configured}`",
        f"- frontier_concurrency_configured: `{plan.validation.frontier_concurrency_configured}`",
        f"- freeze_mode_valid: `{plan.validation.freeze_mode_valid}`",
        "",
        "## KEDA",
        "",
        f"- min_replicas: `{plan.deployment_min_replicas}`",
        f"- max_replicas: `{plan.deployment_max_replicas}`",
        f"- target_backlog_per_pod: `{plan.keda_target_backlog_per_pod}`",
        "",
        "## Output Files",
        "",
        f"- `{json_path.name}`",
        f"- `{scaledobject_path.name}`",
        f"- `{env_path.name}`",
    ]
    with open(md_path, "w") as handle:
        handle.write("\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare GCP Gate A/B staging artifacts.")
    parser.add_argument("--gate", choices=sorted(GATE_PROFILES), required=True, help="Gate profile to prepare.")
    parser.add_argument("--project-id", required=True, help="GCP project id for Pub/Sub resources.")
    parser.add_argument(
        "--corpus",
        default=str(REPO_ROOT / "qualification" / "corpora" / "gate2_local_open_weight.json"),
        help="Qualification corpus used as the staging workload seed.",
    )
    parser.add_argument("--topic-prefix", default="mx-gate", help="Prefix used for Pub/Sub topic ids.")
    parser.add_argument("--subscription-prefix", default="mx-gate", help="Prefix used for Pub/Sub subscription ids.")
    parser.add_argument(
        "--config",
        default=str(REPO_ROOT / "mx.config.cloud.json"),
        help="Cloud config used for frontier budget validation.",
    )
    args = parser.parse_args()

    corpus_path = Path(args.corpus).resolve()
    config_path = Path(args.config).resolve()
    plan = build_gate_ab_plan(
        gate=args.gate,
        project_id=args.project_id,
        corpus_path=corpus_path,
        config_path=config_path,
        topic_prefix=args.topic_prefix,
        subscription_prefix=args.subscription_prefix,
    )
    report_dir = REPO_ROOT / "reports" / "gcp_gate_ab" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report_dir.mkdir(parents=True, exist_ok=True)
    _write_report(report_dir, plan, corpus_path)
    print(f"GCP Gate {plan.gate} staging plan written to {report_dir}")
    validation = plan.validation
    ready = (
        validation.credentials_present
        and validation.project_id_present
        and validation.pubsub_library_installed
        and validation.queue_provider_supported
        and validation.frontier_llm_configured
        and validation.frontier_llm_is_gemini
        and validation.frontier_budget_configured
        and validation.frontier_concurrency_configured
        and validation.freeze_mode_valid
    )
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
