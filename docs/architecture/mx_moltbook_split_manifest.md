# MX vs Moltbook Split Manifest

## Purpose
This manifest converts the separation inventory into an actionable split plan.

The target state is:
- `evolution-mx-swarm` contains MX product code, proof gates, scale harnesses, and delivery surfaces.
- Moltbook ecosystem research moves to a separate workspace/repo.
- Any remaining integration between the two is explicit, optional, and generic.

## Split timing
This split starts as a quarantine policy immediately.

The actual file extraction should happen after the first scaling milestone is frozen.

## Move
These paths should move out of MX into the Moltbook Research workspace.

High-confidence directories:
- `vault/actor_map/**`
- `temp_analysis/**`

High-confidence scripts:
- `scripts/ris_collector.py`
- `scripts/build_ris_graph.py`
- `scripts/zoo_theory_analyzer.py`
- `scripts/ris_analyzer.py`
- `scripts/ris_background_loop.py`
- `scripts/snapshot_ris_graph.py`
- `scripts/content_analytics_batch.py`
- `scripts/backfill_message_trends.py`
- `scripts/real_task_loop.py`
- `scripts/full_loop_orchestrator.py`

High-confidence support modules:
- `molt_client.py`
- `scripts/_moltbook_env.py`

Conditional data move:
- `data/**` when the dataset is Moltbook/RIS-specific and not required by MX proof or scale harnesses

## Keep
These paths stay in MX and should remain the product baseline.

Core runtime:
- `boss/**`
- `worker/**`
- `judge/**`
- `planner/**`
- `shared/**`
- `cli/**`

Product-facing surfaces:
- `dashboard/**`
- `qualification/**`
- `proof_fixtures/**`
- `scripts/run_aether_adapter_proof.py`
- `scripts/run_aether_worker_proof.py`
- `scripts/run_aether_live_proof.py`
- `scripts/run_mock_sink_live_proof.py`
- `scripts/run_scale_control_plane.py`
- `scripts/run_scale_worker_load.py`
- `scripts/run_quarantine_audit.py`

Documentation and reports that stay with MX:
- `docs/architecture/**` except documents that are Moltbook-only
- `reports/**` that describe MX proofs, scaling, or Aether integration

## Review
These paths need an explicit decision before the split because they touch MX-adjacent abstractions.

- `boss/agent_boss.py`
  - Keep only if the remaining behavior is generic swarm control logic.
  - Move or extract if it contains Moltbook-specific coordination.

- `shared/utils/bazaar_client.py`
  - Keep the generic external-agent marketplace interface in MX.
  - Move any Moltbook-specific implementation details out.

- `dashboard/api.py`
  - Keep MX control-plane dashboards.
  - Remove or optionalize actor-map and Moltbook-specific analytics views.

## Boundary rules after split
- MX must not require Moltbook files for normal operation.
- Moltbook research must not write into MX core product paths.
- Any external-agent integration inside MX must be expressed through generic interfaces.
- Concrete Moltbook implementation code must live outside MX.

## Security actions
Current tree state:
- hardcoded Moltbook token strings removed from active files
- current audit reports `secret_hits = 0`

Still required before publication:
- rotate any historical Moltbook credentials that were ever committed
- keep `MOLTBOOK_API_KEY` out of committed config
- treat `MOLTBOOK_ANALYTICS_DB_PATH` as local-only configuration

## Execution sequence
1. Freeze the current MX scaling milestone.
2. Create the Moltbook Research workspace/repo.
3. Move all `Move` paths.
4. Resolve the `Review` paths by either extracting Moltbook-specific code or making it optional.
5. Re-run MX proof gates and scaling smoke.
6. Publish only after MX no longer depends on Moltbook assets.

## Acceptance criteria
The split is complete when all of the following are true:
- MX proof gates still pass.
- MX scale harnesses still pass.
- MX boots and runs without Moltbook data or Moltbook scripts present.
- Moltbook research can evolve independently.
- no live Moltbook credentials remain in the active tree.
