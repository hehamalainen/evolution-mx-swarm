# Agent Guidelines for MX Swarm

This file provides guidelines and constraints for MX Swarm workers operating in this repository.

## Core Principles

1. **Minimal changes**: Make the smallest change that satisfies the task goal
2. **Follow patterns**: Match existing code style and architecture
3. **Test everything**: Add tests for new functionality
4. **Document complexity**: Add comments only where logic isn't self-evident

## Code Style

### Python
- Use type hints for function signatures
- Follow PEP 8 naming conventions
- Keep functions under 50 lines
- Use dataclasses for data structures

### TypeScript/JavaScript
- Use TypeScript strict mode
- Prefer functional patterns
- Use async/await over callbacks
- Keep components small and focused

### General
- No magic numbers - use named constants
- Prefer composition over inheritance
- Handle errors explicitly
- Log meaningful messages

## Testing Requirements

- Unit tests for all new functions
- Integration tests for API endpoints
- Maintain existing test coverage
- Tests must pass before PR submission

## Security Guidelines

- **Never commit secrets** - use environment variables
- **Validate all inputs** - especially from external sources
- **Use parameterized queries** - no string concatenation for SQL
- **Sanitize outputs** - prevent XSS in web contexts
- **Follow least privilege** - request minimal permissions

## File Constraints

### Do Not Modify
These files require human review and should not be modified by workers:

- `.env*` - Environment configurations
- `**/secrets/**` - Secret configurations
- `docker-compose.prod.yml` - Production configs
- `Makefile` - Build system
- `.github/workflows/deploy*.yml` - Deployment workflows

### Require Extra Care
These files are sensitive and changes should be minimal:

- `package.json` / `pyproject.toml` - Dependencies
- Database migrations
- Authentication/authorization code
- Payment processing code

## PR Guidelines

### Title Format
```
[task-id] Brief description of change
```

### Description Requirements
- Link to task/requirement
- Summary of changes
- Testing performed
- Known limitations

### Evidence Pack
Every PR must include:
1. Commands run and their outputs
2. List of files changed
3. How changes were verified
4. Known risks or limitations

## Behavioral Constraints

### Workers MUST:
- Create branches from `main` with format `task/{id}-{slug}`
- Run all acceptance checks before submitting PR
- Include evidence pack in PR description
- Respect timebox limits

### Workers MUST NOT:
- Push directly to `main` or `master`
- Modify files in do_not_touch list
- Commit secrets or credentials
- Make changes outside task scope
- Skip CI checks or tests

## Communication

Workers should:
- Add clear commit messages explaining WHY changes were made
- Comment on complex logic in code
- Flag uncertainties in PR description
- Ask for human review when appropriate

## Quality Gates

All PRs must pass:
1. **Lint**: Code style checks (ruff/eslint)
2. **Type**: Type checking (mypy/tsc)
3. **Test**: Unit and integration tests
4. **Security**: Dependency and code scanning
5. **Build**: Successful build/compile

## Escalation

Workers should escalate to human when:
- Task requirements are ambiguous
- Changes require security-sensitive modifications
- Acceptance criteria cannot be met
- Multiple approaches exist with significant trade-offs
