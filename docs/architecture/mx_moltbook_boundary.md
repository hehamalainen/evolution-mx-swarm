# MX vs Moltbook Boundary

Date: `2026-03-25`

This document freezes the temporary boundary policy while MX scaling work proceeds.

## Immediate Rule

The repo is still mixed, but from this point forward:

- new MX product work belongs in MX product paths only
- new Moltbook research work belongs in quarantined research paths only
- no new Moltbook research code should be added to MX core paths

This is a quarantine policy, not yet the final repo split.

## MX Product Surface

These areas remain part of MX:

- `boss`
- `worker`
- `judge`
- `planner`
- `shared`
- `cli`
- `dashboard`
- delivery proofs and qualification
- Aether integration
- scaling harnesses and reports

## Moltbook Research Surface

These areas are quarantined for later extraction:

- `vault/actor_map`
- `temp_analysis`
- Moltbook ecosystem analytics scripts
- RIS collectors and graph builders
- Moltbook posting loops
- non-product datasets under `data`

## Special Boundary

Marketplace or external-agent concepts may remain in MX only as abstractions.

- generic interface: stays in MX
- Moltbook implementation: moves out later

## Pre-Publish Constraint

Moltbook-linked credentials and tokens must be treated as a publish blocker.

Before pushing a clean public MX history:

1. inventory Moltbook-linked files
2. rotate/remove any live credentials
3. split or quarantine research paths
4. rerun proof gates

## Tooling

Use:

- `mx quarantine-audit`

to inventory the current boundary surface and detect live Moltbook secrets before publication.
