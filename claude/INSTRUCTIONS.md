# MX Swarm - Build and Usage Instructions

## Project Overview
**MX Swarm** is an autonomous system designed to take a Product Requirements Document (PRD) and execute it using a swarm of AI workers. It plans tasks, generates code, opens PRs, and verifies changes against a Definition of Done.

## Latest Updates & Fixes
The project has been recently updated with the following improvements:
1.  **Environment**: Virtual environment set up and dependencies installed.
2.  **Import Fixes**: Converted relative imports to absolute imports (`from shared`) across 6 files for better stability.
3.  **Dashboard**: Added missing `dashboard/__init__.py`.
4.  **Templates**: Created `.env.example` for easier API key configuration.

## Prerequisites
- **Python**: 3.11 or higher
- **Git**: For version control
- **API Keys**:
  - Anthropic API Key (required for the LLM)
  - GitHub Token (required for repo operations)

## Installation

1.  **Activate the virtual environment**:
    ```bash
    source venv/bin/activate
    ```

2.  **Install dependencies** (if not already done):
    ```bash
    pip install -r requirements.txt
    ```

3.  **Initialize the project**:
    ```bash
    python cli/main.py init --owner YOUR_GITHUB_USER --repo YOUR_REPO
    ```

## Configuration

Set the following environment variables. You can use the `.env.example` as a template.

```bash
export ANTHROPIC_API_KEY="your-key"
export GITHUB_TOKEN="your-token"
```

## Running the Application

The application is primarily run via the CLI.

### 1. Define Requirements
Edit the `PRD.md` file in the root directory. The current sample contains a **Calculator API** project.

### 2. Start the Swarm
Run the full swarm (Planner -> Workers -> Judge):
```bash
python cli/main.py run --workers 5
```

### 3. Intelligent Orchestration (Agent Boss)
To use the Agent Boss for more intelligent orchestration:
```bash
python cli/main.py ignite
```

### 4. Monitoring Dashboard
To open the visual control panel:
```bash
python cli/main.py dashboard
```
Then open your browser at `http://localhost:8000`.

### 5. Individual Components
-   **Status**: `python cli/main.py status`
-   **Plan**: `python cli/main.py plan`
-   **Judge**: `python cli/main.py judge`

## Testing
The project currently has **41 passing tests**. To run them:
```bash
pytest
```
