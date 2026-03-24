# Slug CLI

Implement a CLI that depends on the existing slug helper.

## Strict File Targets

- tools/slugcli.py
- tests/test_slugcli.py

## Acceptance Criteria

1. `python -m pytest tests/test_slugcli.py -q` passes.
2. Plain mode prints `slug: <value>`.
3. `--json` prints `{\"slug\": \"<value>\"}`.
4. Missing files exit non-zero with an error message.
