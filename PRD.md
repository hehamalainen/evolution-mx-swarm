# Textstats CLI

Build a small CLI tool called `textstats`.

## Goal

Read a `.txt` file and report:

- `lines`
- `words`
- `chars`

## User Stories

1. As a user, I can run `python tools/textstats.py sample.txt` and get plain-text output with line, word, and character counts.
2. As a user, I can run `python tools/textstats.py sample.txt --json` and get valid JSON with keys `lines`, `words`, and `chars`.
3. As a user, if the input file does not exist, the tool exits non-zero and prints a clear error message.

## Strict File Targets

Only these files may be changed:

- `tools/textstats.py`
- `tests/test_textstats.py`

## Acceptance Criteria

1. `python tools/textstats.py sample.txt` prints all three counters.
2. `python tools/textstats.py sample.txt --json` prints valid JSON with keys `lines`, `words`, and `chars`.
3. A missing file exits non-zero and prints an error message.
4. `pytest -q tests/test_textstats.py` passes.

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
