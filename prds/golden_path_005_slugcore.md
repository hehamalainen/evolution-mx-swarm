# Slugcore Helper

Implement a narrow helper for stable slug normalization.

## Strict File Targets

- tools/slugcore.py
- tests/test_slugcore.py

## Acceptance Criteria

1. `python -m pytest tests/test_slugcore.py -q` passes.
2. Slugs are lowercase.
3. Whitespace and punctuation collapse to single `-`.
4. Leading and trailing separators are removed.
