# MX Swarm Golden Paths

## golden_path_001

- branch: `task/4b6070bf-implement-textstats-cli-using-`
- commit: `830115f`
- mock PR: `#1`
- PR URL: `http://localhost/mock/hehamalainen/evolution-mx-swarm/pull/1`
- verified independently in a separate worktree
- acceptance proof:
  - `python -m pytest tests/test_textstats.py -q`
  - `python -m ruff check tools/textstats.py tests/test_textstats.py`

## golden_path_002

- branch: `task/4ab625a3-implement-jsonpeek-cli-using-o`
- commit: `dc1ef6b`
- mock PR: `#3`
- PR URL: `http://localhost/mock/hehamalainen/evolution-mx-swarm/pull/3`
- verified independently in a separate worktree with the frozen read-only test asset overlaid
- acceptance proof:
  - `python -m pytest tests/test_jsonpeek.py -q`
  - `python -m ruff check tools/jsonpeek.py tests/test_jsonpeek.py`

## Notes

- `golden_path_001` is integrated in the working tree as the current `tools/textstats.py` baseline.
- `golden_path_002` remains on its generated task branch so its provenance stays explicit.
- Strict-target workers now stage only generated files, not overlaid context files.
- All future golden-path runs must execute against a proven baseline, not against incidental dirty-branch state.
- Promotion from one golden path to the next requires the artifact branch, submission event, and independent verification evidence to remain intact.
