# TomlPeek CLI

Implement the narrow local-model slice for summarizing top-level keys from a TOML file.

## Strict File Targets
- tools/tomlpeek.py
- tests/test_tomlpeek.py

## Acceptance Criteria
- Plain mode prints `key_count` and a sorted comma-separated `keys` list.
- `--json` emits `{"key_count": N, "keys": [...]}` with sorted keys.
- Missing files exit non-zero and print a clear error message.
- Invalid TOML exits non-zero and prints a clear error message.
