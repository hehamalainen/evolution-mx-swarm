# Jsonpeek CLI

Build a small CLI tool called `jsonpeek`.

## Goal

Read a JSON file whose top-level value must be an object and report:

- `key_count`
- `keys`

## User Stories

1. As a user, I can run `python tools/jsonpeek.py sample.json` and get plain-text output with the top-level key count and sorted key list.
2. As a user, I can run `python tools/jsonpeek.py sample.json --json` and get valid JSON with keys `key_count` and `keys`.
3. As a user, if the input file does not exist, the tool exits non-zero and prints a clear error message.
4. As a user, if the file contains invalid JSON, an empty file, or a non-object top-level value, the tool exits non-zero and prints a clear invalid JSON error.

## Strict File Targets

Only these files may be changed:

- `tools/jsonpeek.py`
- `tests/test_jsonpeek.py`

## Acceptance Criteria

1. `python tools/jsonpeek.py sample.json` prints the sorted key summary.
2. `python tools/jsonpeek.py sample.json --json` prints valid JSON with keys `key_count` and `keys`.
3. Missing file exits non-zero and prints an error message.
4. Invalid JSON, empty file, or non-object top-level JSON exits non-zero and prints an error message.
5. `pytest -q tests/test_jsonpeek.py` passes.

## Implementation Constraints

- Keep scope minimal.
- Do not touch unrelated files.
- Prefer standard library only.
- Return only file edits for the two strict targets above.

## Swarm Output Contract

Return only file blocks in this exact format:

```text
```python:relative/path.py
<complete file content>
```
```
