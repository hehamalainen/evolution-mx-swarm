# Agent Guidelines

This file provides guidelines for MX Swarm workers.

## Code Style
- Follow existing patterns in the codebase
- Use meaningful variable and function names (snake_case for Python)
- Add comments for complex logic
- Use type hints in Python code

## Testing
- Add tests for new functionality using pytest
- Ensure existing tests pass
- Place tests in `tests/` directory

## Security
- Never commit secrets or credentials
- Validate all inputs
- Use parameterized queries for databases

## Constraints
- Keep PRs focused and small
- Don't modify core infrastructure without explicit task
- Prefer incremental changes over large rewrites
- Do not modify files in `.github/` unless explicitly asked

## Dashboard Wiring (Current Baseline)
- No dead controls: UI exposes only actions wired to real backend behavior.
- Telemetry source: Dashboard status, queue depth, and active tasks come from CLI heartbeats.
- VCS targets: Only `aether` or `github` are supported (no "both").
- Org Chart: Force-layout graph with drag-to-move, double-click to pin, and Freeze/Unfreeze toggle.
