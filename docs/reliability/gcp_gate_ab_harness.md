# GCP Gate A/B Harness

This harness prepares concrete staging artifacts for Gate A and Gate B on GCP.

## Source of truth

- staging plan generator: `scripts/run_gcp_gate_ab.py`
- gate definitions: `docs/reliability/stage_gate_criteria.md`
- workload seed corpus: `qualification/corpora/gate2_local_open_weight.json`

## What it produces

For each run:

- `reports/gcp_gate_ab/<timestamp>/gcp_gate_ab_plan.json`
- `reports/gcp_gate_ab/<timestamp>/gcp_gate_ab_plan.md`
- `reports/gcp_gate_ab/<timestamp>/keda_scaledobject.yaml`
- `reports/gcp_gate_ab/<timestamp>/deployment_env.json`

These are preparation artifacts, not proof of cloud execution by themselves.

## Default usage

```bash
./.venv/bin/python scripts/run_gcp_gate_ab.py \
  --gate A \
  --project-id your-gcp-project
```

```bash
./.venv/bin/python scripts/run_gcp_gate_ab.py \
  --gate B \
  --project-id your-gcp-project
```

## Validation

The harness records whether:

- a project id was supplied
- GCP credentials appear present
- `google.cloud.pubsub_v1` is installed

If these checks fail, the plan is still written, but the command exits non-zero to prevent pretending the environment is ready.
