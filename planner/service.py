"""Planner Service - PRD to SPEC to TaskContracts.

The Planner is responsible for:
1. Parsing PRD.md and extracting requirements
2. Generating SPEC.json (machine-readable requirements graph)
3. Creating TaskContracts and publishing to queue
4. Replanning when requested by Judge
"""

import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from shared.schemas import (
    TaskContract,
    TaskType,
    TaskContext,
    AcceptanceCriteria,
    TaskConstraints,
    MergePolicy,
    RiskLevel,
    Spec,
    Requirement,
    RequirementType,
    RequirementStatus,
)
from shared.adapters import QueueAdapter, create_queue_adapter
from shared.utils import MXConfig, LLMClient, Message, create_llm_client


SYSTEM_PROMPT = """You are an expert software architect and project planner. Your role is to analyze PRDs (Product Requirements Documents) and break them down into machine-readable specifications and actionable tasks.

When analyzing a PRD:
1. Extract clear, testable requirements
2. Identify dependencies between requirements
3. Estimate complexity and risk
4. Create small, focused tasks (30-90 minutes each)
5. Define acceptance criteria that can be verified programmatically

Always output valid JSON when requested. Be precise and actionable."""


@dataclass
class PlannerState:
    """Planner state tracking."""
    prd_hash: str
    spec_generated: bool = False
    tasks_published: int = 0
    last_run: Optional[str] = None


class Planner:
    """
    Planner service for MX Swarm.

    Converts PRD.md -> SPEC.json -> TaskContracts -> Queue
    """

    def __init__(
        self,
        config: MXConfig,
        queue: Optional[QueueAdapter] = None,
        llm: Optional[LLMClient] = None,
        repo_path: str = ".",
    ):
        self.config = config
        self.queue = queue or create_queue_adapter(config.queue.to_dict())
        self.llm = llm or create_llm_client(config.llm.to_dict())
        self.repo_path = repo_path
        self.state: Optional[PlannerState] = None

    async def initialize(self) -> None:
        """Initialize planner connections."""
        await self.queue.connect()

    async def shutdown(self) -> None:
        """Cleanup planner connections."""
        await self.queue.disconnect()

    def _read_prd(self) -> str:
        """Read PRD.md from repo."""
        prd_path = os.path.join(self.repo_path, "PRD.md")
        if not os.path.exists(prd_path):
            raise FileNotFoundError(f"PRD.md not found at {prd_path}")

        with open(prd_path) as f:
            return f.read()

    def _read_agents_md(self) -> str:
        """Read AGENTS.md from repo (optional)."""
        agents_path = os.path.join(self.repo_path, "AGENTS.md")
        if os.path.exists(agents_path):
            with open(agents_path) as f:
                return f.read()
        return ""

    def _hash_prd(self, prd_content: str) -> str:
        """Generate hash of PRD for change detection."""
        return hashlib.sha256(prd_content.encode()).hexdigest()[:16]

    def _load_spec(self) -> Optional[Spec]:
        """Load existing SPEC.json if present."""
        spec_path = os.path.join(self.repo_path, "SPEC.json")
        if os.path.exists(spec_path):
            return Spec.load(spec_path)
        return None

    def _save_spec(self, spec: Spec) -> None:
        """Save SPEC.json to repo."""
        spec_path = os.path.join(self.repo_path, "SPEC.json")
        spec.save(spec_path)

    async def _extract_requirements(self, prd: str) -> list[Requirement]:
        """Use LLM to extract requirements from PRD."""
        prompt = f"""Analyze this PRD and extract all requirements as a JSON array.

PRD:
{prd}

For each requirement, provide:
- type: "functional", "non_functional", "constraint", or "acceptance"
- title: Short title (max 50 chars)
- description: Detailed description
- acceptance_criteria: Array of testable criteria
- priority: 1-10 (10 is highest)
- estimated_complexity: 1-10 (10 is most complex)
- prd_section: Section name where this was found

Output ONLY valid JSON array, no markdown, no explanation:
[
  {{
    "type": "functional",
    "title": "...",
    "description": "...",
    "acceptance_criteria": ["..."],
    "priority": 5,
    "estimated_complexity": 5,
    "prd_section": "..."
  }}
]"""

        response = await self.llm.chat(
            messages=[Message(role="user", content=prompt)],
            system=SYSTEM_PROMPT,
            max_tokens=8000,
            temperature=0.3,
        )

        # Parse JSON from response
        content = response.content.strip()

        # Try to extract JSON if wrapped in markdown
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0]
        elif "```" in content:
            content = content.split("```")[1].split("```")[0]

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            # Try to find JSON array in response
            match = re.search(r'\[.*\]', content, re.DOTALL)
            if match:
                data = json.loads(match.group())
            else:
                raise ValueError(f"Could not parse requirements JSON: {content[:500]}")

        requirements = []
        for item in data:
            req = Requirement.create(
                req_type=RequirementType(item["type"]),
                title=item["title"],
                description=item["description"],
                acceptance_criteria=item.get("acceptance_criteria", []),
                prd_section=item.get("prd_section"),
                priority=item.get("priority", 5),
                complexity=item.get("estimated_complexity", 5),
            )
            requirements.append(req)

        return requirements

    async def _identify_dependencies(
        self,
        requirements: list[Requirement],
    ) -> list[Requirement]:
        """Use LLM to identify dependencies between requirements."""
        req_summary = "\n".join(
            f"- {r.id}: {r.title}"
            for r in requirements
        )

        prompt = f"""Analyze these requirements and identify dependencies.

Requirements:
{req_summary}

For each requirement that depends on others, output JSON:
{{
  "dependencies": {{
    "REQ-ID": ["DEP-ID1", "DEP-ID2"],
    ...
  }}
}}

A requirement depends on another if it cannot be implemented until the other is done.
Only include requirements that have dependencies. Output ONLY valid JSON:"""

        response = await self.llm.chat(
            messages=[Message(role="user", content=prompt)],
            system=SYSTEM_PROMPT,
            max_tokens=2000,
            temperature=0.2,
        )

        content = response.content.strip()
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0]
        elif "```" in content:
            content = content.split("```")[1].split("```")[0]

        try:
            data = json.loads(content)
            deps = data.get("dependencies", {})

            # Apply dependencies
            req_map = {r.id: r for r in requirements}
            for req_id, dep_ids in deps.items():
                if req_id in req_map:
                    # Validate dependencies exist
                    valid_deps = [d for d in dep_ids if d in req_map]
                    req_map[req_id].dependencies = valid_deps

        except json.JSONDecodeError:
            pass  # Keep requirements without dependencies

        return requirements

    async def generate_spec(self, force: bool = False) -> Spec:
        """
        Generate SPEC.json from PRD.md.

        Args:
            force: Regenerate even if PRD hasn't changed

        Returns:
            Generated Spec
        """
        prd = self._read_prd()
        prd_hash = self._hash_prd(prd)

        # Check if we need to regenerate
        existing_spec = self._load_spec()
        if existing_spec and existing_spec.prd_hash == prd_hash and not force:
            return existing_spec

        # Extract requirements
        requirements = await self._extract_requirements(prd)

        # Identify dependencies
        requirements = await self._identify_dependencies(requirements)

        # Create spec
        spec = Spec.create(prd_hash)
        for req in requirements:
            spec.add_requirement(req)

        # Save spec
        self._save_spec(spec)

        self.state = PlannerState(
            prd_hash=prd_hash,
            spec_generated=True,
            last_run=datetime.utcnow().isoformat(),
        )

        return spec

    async def _create_task_for_requirement(
        self,
        requirement: Requirement,
        agents_md: str,
    ) -> list[TaskContract]:
        """Create TaskContracts for a single requirement."""
        prompt = f"""Create implementation tasks for this requirement.

Requirement:
- ID: {requirement.id}
- Title: {requirement.title}
- Description: {requirement.description}
- Acceptance Criteria: {json.dumps(requirement.acceptance_criteria)}
- Complexity: {requirement.estimated_complexity}/10

Agent Guidelines (from AGENTS.md):
{agents_md[:2000] if agents_md else "No specific guidelines."}

Break this into 1-3 tasks (prefer fewer, more focused tasks).
Each task should be completable in 30-90 minutes.

Output JSON array:
[
  {{
    "type": "feature|bugfix|test|refactor|ops|doc",
    "goal": "One sentence objective",
    "files_hint": ["suggested/files/to/modify.py"],
    "do_not_touch": ["files/to/avoid.py"],
    "checks": ["pytest tests/", "ruff check ."],
    "criteria": ["Readable acceptance criteria"],
    "timebox_minutes": 60,
    "risk": "low|medium|high",
    "merge_policy": "manual|auto_on_pass|require_review"
  }}
]

Output ONLY valid JSON:"""

        response = await self.llm.chat(
            messages=[Message(role="user", content=prompt)],
            system=SYSTEM_PROMPT,
            max_tokens=3000,
            temperature=0.4,
        )

        content = response.content.strip()
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0]
        elif "```" in content:
            content = content.split("```")[1].split("```")[0]

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r'\[.*\]', content, re.DOTALL)
            if match:
                data = json.loads(match.group())
            else:
                # Create a default task
                data = [{
                    "type": "feature",
                    "goal": requirement.title,
                    "files_hint": [],
                    "checks": [],
                    "criteria": requirement.acceptance_criteria,
                    "timebox_minutes": 60,
                    "risk": "medium",
                    "merge_policy": "require_review",
                }]

        tasks = []
        for item in data:
            task = TaskContract.create(
                task_type=TaskType(item.get("type", "feature")),
                goal=item["goal"],
                context=TaskContext(
                    prd_refs=[requirement.id],
                    files_hint=item.get("files_hint", []),
                    do_not_touch=item.get("do_not_touch", []),
                ),
                acceptance=AcceptanceCriteria(
                    checks=item.get("checks", []),
                    criteria=item.get("criteria", requirement.acceptance_criteria),
                ),
                constraints=TaskConstraints(
                    timebox_minutes=item.get("timebox_minutes", 60),
                    risk=RiskLevel(item.get("risk", "medium")),
                    merge_policy=MergePolicy(item.get("merge_policy", "require_review")),
                    retry_max=self.config.worker.retry_max,
                ),
                priority=requirement.priority,
                spec_requirement_ids=[requirement.id],
            )
            tasks.append(task)

        return tasks

    async def generate_tasks(
        self,
        spec: Optional[Spec] = None,
        requirements_filter: Optional[list[str]] = None,
    ) -> list[TaskContract]:
        """
        Generate TaskContracts from SPEC.

        Args:
            spec: Spec to use (loads from file if not provided)
            requirements_filter: Only generate tasks for these requirement IDs

        Returns:
            List of generated TaskContracts
        """
        if spec is None:
            spec = self._load_spec()
            if spec is None:
                spec = await self.generate_spec()

        agents_md = self._read_agents_md()

        # Get requirements to process
        if requirements_filter:
            requirements = [
                r for r in spec.requirements
                if r.id in requirements_filter
            ]
        else:
            # Only process ready requirements (dependencies met, not completed)
            requirements = spec.get_ready_requirements()

        # Generate tasks for each requirement
        all_tasks = []
        for req in requirements:
            tasks = await self._create_task_for_requirement(req, agents_md)
            all_tasks.extend(tasks)

            # Link tasks to requirement
            req.assigned_tasks.extend(t.id for t in tasks)
            req.status = RequirementStatus.IN_PROGRESS

        # Save updated spec
        self._save_spec(spec)

        return all_tasks

    async def publish_tasks(self, tasks: list[TaskContract]) -> int:
        """
        Publish tasks to queue.

        Args:
            tasks: TaskContracts to publish

        Returns:
            Number of tasks published
        """
        published = 0
        for task in tasks:
            await self.queue.publish(task, priority=task.priority)
            published += 1

        if self.state:
            self.state.tasks_published += published

        return published

    async def run(self, force_replan: bool = False) -> dict:
        """
        Full planning run: PRD -> SPEC -> Tasks -> Queue.

        Args:
            force_replan: Force regeneration of spec and tasks

        Returns:
            Summary of planning run
        """
        await self.initialize()

        try:
            # Generate spec
            spec = await self.generate_spec(force=force_replan)

            # Generate tasks
            tasks = await self.generate_tasks(spec)

            # Publish to queue
            published = await self.publish_tasks(tasks)

            return {
                "success": True,
                "spec_requirements": len(spec.requirements),
                "tasks_generated": len(tasks),
                "tasks_published": published,
                "progress": spec.progress_summary(),
            }

        finally:
            await self.shutdown()

    async def replan(self, failed_task_ids: list[str] = None) -> dict:
        """
        Replan after failures or when requested by Judge.

        Args:
            failed_task_ids: IDs of failed tasks to retry

        Returns:
            Summary of replanning
        """
        spec = self._load_spec()
        if spec is None:
            return await self.run(force_replan=True)

        await self.initialize()

        try:
            # Find requirements for failed tasks or incomplete requirements
            reqs_to_replan = []

            if failed_task_ids:
                for req in spec.requirements:
                    if any(tid in failed_task_ids for tid in req.assigned_tasks):
                        reqs_to_replan.append(req.id)
                        req.status = RequirementStatus.PENDING
                        req.assigned_tasks = []
            else:
                # Replan all non-completed requirements
                for req in spec.requirements:
                    if req.status != RequirementStatus.COMPLETED:
                        reqs_to_replan.append(req.id)
                        req.status = RequirementStatus.PENDING
                        req.assigned_tasks = []

            self._save_spec(spec)

            # Generate new tasks
            tasks = await self.generate_tasks(spec, requirements_filter=reqs_to_replan)
            published = await self.publish_tasks(tasks)

            return {
                "success": True,
                "requirements_replanned": len(reqs_to_replan),
                "tasks_generated": len(tasks),
                "tasks_published": published,
            }

        finally:
            await self.shutdown()


async def main():
    """CLI entry point for planner."""
    import argparse

    parser = argparse.ArgumentParser(description="MX Swarm Planner")
    parser.add_argument("--config", default="mx.config.json", help="Config file path")
    parser.add_argument("--repo", default=".", help="Repository path")
    parser.add_argument("--force", action="store_true", help="Force regeneration")
    parser.add_argument("--replan", action="store_true", help="Replan failed tasks")
    args = parser.parse_args()

    config = MXConfig.load(args.config)
    planner = Planner(config, repo_path=args.repo)

    if args.replan:
        result = await planner.replan()
    else:
        result = await planner.run(force_replan=args.force)

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
