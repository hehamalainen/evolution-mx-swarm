# Namecore Helper

Implement a narrow helper for stable name summaries.

## Strict File Targets

- tools/namecore.py
- tests/test_namecore.py

## Acceptance Criteria

1. `python -m pytest tests/test_namecore.py -q` passes.
2. Whitespace is stripped from each input entry.
3. Empty and whitespace-only entries are dropped after stripping.
4. The returned `names` list is sorted and unique using the stripped values.
5. The returned `name_count` equals the length of that final deduplicated `names` list.
