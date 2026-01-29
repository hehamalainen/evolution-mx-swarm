# MX Swarm Agent Roster & Guidelines

## 🤖 Colleague Agent Roster

The Evolution-MX-Swarm project is developed by a multi-agent collective. Each agent has specific strengths and operational modes.

### 1. Antigravity (Orchestrator/Architect)
- **Role:** High-level planning, complex refactors, and artifact management.
- **Model:** Gemini 2.0 Flash (Agentic Mode).
- **Function:** Orchestrates the other agents and maintains the project's long-term vision.

### 2. Claude Code (Implementation Agent)
- **Role:** Rapid implementation, codebase exploration, and CLI-based refactoring.
- **Tool:** `claude` CLI.
- **Strength:** Precision in editing and deep understanding of code context.

### 3. Codex CLI (The "Logic Engine")
- **Role:** Strategic coding, performance optimization, and reasoning.
- **Model:** **GPT-5.2 Codex**.
- **Tool:** `codex` CLI.
- **Strength:** Advanced reasoning for complex logic and algorithmic optimizations.

### 4. MX Swarm (Autonomous Delivery)
- **Role:** Scalable task execution, PR generation, and CI/CD automation.
- **Tool:** `mx run`.
- **Function:** The autonomous engine that drives the PRD-to-Shipping pipeline.

---

## 📜 Shared Operational Protocols

### Code Style
- **Python:** Use `snake_case`. Follow PEP 8. Use Type Hints diligently.
- **Comments:** Docstrings for every class and method. Inline comments for "the why," not "the what."
- **Nomenclature:** Variable names must be descriptive and context-aware.

### Testing & Verification
- **Pytest:** All new logic must be accompanied by unit or integration tests in `tests/`.
- **Pre-check:** Run `ruff check .` and `pytest` before submitting changes via `AetherVCS`.

### Collaboration via Aether
- All agents must use the **Aether VCS** provider (`AetherVCS`) to submit proposals.
- Do not bypass the `PROMOTED` gate unless explicitly authorized by the Orchestrator.

### Constraints
- **Scope:** Keep tasks and PRs focused. One goal per submission.
- **Security:** Never commit `.env` files or API keys.
- **Consistency:** Use common shared schemas in `shared/schemas/` to ensure inter-agent compatibility.

### Dashboard Wiring (Current Baseline)
- **No dead controls:** UI exposes only actions wired to real backend behavior.
- **Telemetry source:** Dashboard status, queue depth, and active tasks come from CLI heartbeats.
- **VCS targets:** Only `aether` or `github` are supported (no "both").
- **Org Chart:** Force-layout graph with drag-to-move, double-click to pin, and Freeze/Unfreeze toggle.
