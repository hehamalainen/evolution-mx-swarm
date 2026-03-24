# Name CLI

Implement a CLI that depends on the existing name summary helper.

## Strict File Targets

- tools/namecli.py
- tests/test_namecli.py

## Acceptance Criteria

1. `python -m pytest tests/test_namecli.py -q` passes.
2. Plain mode prints `name_count` and `names`.
3. `--json` prints the exact helper payload.
4. Missing files exit non-zero with an error message.
5. `build_summary_payload(...)` passes `text.splitlines()` into the helper once and returns the helper result unchanged.
