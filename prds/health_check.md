# PRD: MX Swarm Health Check

## Goal
Implement a lightweight health-check endpoint for the MX Swarm dashboard to provide real-time status and versioning information.

## Requirements
1.  **Endpoint**: `/api/health`
2.  **Payload**:
    - `status`: "OK"
    - `version`: The current short SHA of the Git commit.
    - `uptime`: The uptime of the system (optional).
3.  **Implementation**:
    - Use Python's `subprocess` to fetch the git hash.
    - Integrate into the existing FastAPI/Vite setup.

## Task for Outsourcing
The retrieval of the Git commit hash and formatting it for the API response should be handled by a specialized "Git Utilities" subservice. This task will be posted to the Moltbook Bazaar for fulfillment by a community agent.
