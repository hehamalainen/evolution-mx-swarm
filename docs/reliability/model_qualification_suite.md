# Model Qualification Suite

This suite turns the proven Gate 2 narrow-task corpus into a reusable admission test for the local/open-weight model pool.

## Source of truth

- corpus manifest: `qualification/corpora/gate2_local_open_weight.json`
- reusable scorer: `shared/utils/qualification.py`
- execution runner: `scripts/run_model_qualification.py`
- underlying isolated corpus runner: `scripts/run_gate2_local_parallel.py`

## What is measured

- format compliance rate
- deterministic acceptance rate
- false-success rate
- duplicate settlement events
- duplicate claim events
- lifecycle bug events

Format compliance is intentionally measured separately from deterministic acceptance:

- `ERR_NO_DIFF` and `ERR_REFUSAL` count as format-noncompliant
- `ERR_SYNTAX` and `ERR_CHECK_FAIL` count as format-compliant but acceptance-failed

This separation prevents weak models from appearing "qualified" simply because they emit fenced blocks.

## Default run

```bash
./.venv/bin/python scripts/run_model_qualification.py \
  --corpus qualification/corpora/gate2_local_open_weight.json \
  --model-label ollama/mistral:7b
```

Outputs:

- `reports/model_qualification/<timestamp>/model_qualification.json`
- `reports/model_qualification/<timestamp>/model_qualification.md`

## Promotion rule

A model is not admitted into the bulk local/open-weight pool unless the corpus passes with the thresholds versioned in the manifest.
