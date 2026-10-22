# GCP Gate A/B Staging Plan

- gate: `A`
- project_id: `otterboard-f08f8`
- topic_id: `mx-gate-a-20261022082823`
- subscription_id: `mx-gate-a-20261022082823`
- worker_count: `100`
- duration_minutes: `60`
- corpus_name: `gate2_local_open_weight`
- slices: `jsonpeek, envpeek, tomlpeek`

## Validation

- credentials_present: `True`
- project_id_present: `True`
- pubsub_library_installed: `False`
- queue_provider_supported: `True`
- frontier_llm_configured: `True`
- frontier_llm_is_gemini: `True`
- frontier_budget_configured: `True`
- frontier_concurrency_configured: `True`
- freeze_mode_valid: `True`

## KEDA

- min_replicas: `10`
- max_replicas: `100`
- target_backlog_per_pod: `1`

## Output Files

- `gcp_gate_ab_plan.json`
- `keda_scaledobject.yaml`
- `deployment_env.json`