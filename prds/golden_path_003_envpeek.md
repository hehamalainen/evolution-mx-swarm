# EnvPeek CLI

Implement the narrow local-model slice for summarizing environment variable keys from a `.env` file.

## Strict File Targets
- tools/envpeek.py
- tests/test_envpeek.py

## Acceptance Criteria
- Plain mode prints `key_count` and a sorted comma-separated `keys` list.
- `--json` emits `{"key_count": N, "keys": [...]}` with sorted keys.
- Blank lines and `#` comments are ignored.
- Missing files exit non-zero and print a clear error message.
- Invalid `.env` syntax exits non-zero and prints a clear error message.
