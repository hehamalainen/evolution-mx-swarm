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
import shlex
from dataclasses import dataclass
from datetime import UTC, datetime

from shared.adapters import QueueAdapter, create_queue_adapter
from shared.schemas import (
    AcceptanceCriteria,
    MergePolicy,
    Requirement,
    RequirementStatus,
    RequirementType,
    RiskLevel,
    RiskTier,
    Spec,
    TaskConstraints,
    TaskContext,
    TaskContract,
    TaskStream,
    TaskType,
)
from shared.utils import LLMClient, Message, MXConfig, create_llm_client, snapshot_task
from shared.utils.metrics import append_trace_span

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
    last_run: str | None = None


class Planner:
    """
    Planner service for MX Swarm.

    Converts PRD.md -> SPEC.json -> TaskContracts -> Queue
    """

    def __init__(
        self,
        config: MXConfig,
        queue: QueueAdapter | None = None,
        llm: LLMClient | None = None,
        repo_path: str = ".",
        prd_path: str | None = None,
    ):
        self.config = config
        self.queue = queue or create_queue_adapter(config.queue.to_dict())
        self.llm = llm or create_llm_client(config.llm.to_dict())
        self.repo_path = repo_path
        self.prd_path = prd_path
        self.state: PlannerState | None = None

    async def initialize(self) -> None:
        """Initialize planner connections."""
        await self.queue.connect()

    async def shutdown(self) -> None:
        """Cleanup planner connections."""
        await self.queue.disconnect()

    def _read_prd(self) -> str:
        """Read PRD.md from repo."""
        prd_path = self._resolve_prd_path()
        if not os.path.exists(prd_path):
            raise FileNotFoundError(f"PRD.md not found at {prd_path}")

        with open(prd_path) as f:
            return f.read()

    def _resolve_prd_path(self) -> str:
        """Resolve the PRD path relative to the repo root when needed."""
        if self.prd_path:
            if os.path.isabs(self.prd_path):
                return self.prd_path
            return os.path.join(self.repo_path, self.prd_path)
        return os.path.join(self.repo_path, "PRD.md")

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

    def _load_spec(self) -> Spec | None:
        """Load existing SPEC.json if present."""
        spec_path = os.path.join(self.repo_path, "SPEC.json")
        if os.path.exists(spec_path):
            return Spec.load(spec_path)
        return None

    def _save_spec(self, spec: Spec) -> None:
        """Save SPEC.json to repo."""
        spec_path = os.path.join(self.repo_path, "SPEC.json")
        spec.save(spec_path)

    def _normalize_task_type(self, value: object) -> TaskType:
        """Normalize LLM-emitted task type values to known TaskType members."""
        if isinstance(value, TaskType):
            return value

        normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "task": "feature",
            "tasks": "feature",
            "implementation": "feature",
            "bug": "bugfix",
            "fix": "bugfix",
            "test_case": "test",
            "tests": "test",
            "qa": "test",
            "documentation": "doc",
            "docs": "doc",
            "operations": "ops",
        }
        normalized = aliases.get(normalized, normalized)

        try:
            return TaskType(normalized)
        except ValueError:
            print(f"  > Warning: Unknown task type '{value}', defaulting to 'feature'")
            return TaskType.FEATURE

    def _normalize_risk_level(self, value: object) -> RiskLevel:
        """Normalize LLM-emitted risk values to known RiskLevel members."""
        if isinstance(value, RiskLevel):
            return value

        normalized = str(value or "").strip().lower()
        aliases = {
            "med": "medium",
        }
        normalized = aliases.get(normalized, normalized)

        try:
            return RiskLevel(normalized)
        except ValueError:
            print(f"  > Warning: Unknown risk level '{value}', defaulting to 'medium'")
            return RiskLevel.MEDIUM

    def _normalize_merge_policy(self, value: object) -> MergePolicy:
        """Normalize LLM-emitted merge policy values to known MergePolicy members."""
        if isinstance(value, MergePolicy):
            return value

        normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "auto": "auto_on_pass",
            "review": "require_review",
            "manual_review": "require_review",
        }
        normalized = aliases.get(normalized, normalized)

        try:
            return MergePolicy(normalized)
        except ValueError:
            print(f"  > Warning: Unknown merge policy '{value}', defaulting to 'require_review'")
            return MergePolicy.REQUIRE_REVIEW

    def _normalize_relpath(self, value: str) -> str:
        cleaned = value.strip().strip("`").strip("'").strip('"')
        normalized = os.path.normpath(cleaned.replace("\\", "/")).replace("\\", "/")
        return "" if normalized == "." else normalized

    def _is_constrained_local_mode(self) -> bool:
        provider = (self.config.llm.provider or "").lower()
        model = (self.config.llm.model or "").lower()
        return provider == "ollama" and "7b" in model

    def _extract_markdown_section_items(self, prd: str, heading: str) -> list[str]:
        items: list[str] = []
        in_section = False

        for raw_line in prd.splitlines():
            line = raw_line.rstrip()
            stripped = line.strip()
            if stripped.startswith("#"):
                normalized_heading = re.sub(r"^#+\s*", "", stripped).strip().lower()
                if in_section:
                    break
                if normalized_heading == heading.lower():
                    in_section = True
                continue

            if not in_section or not stripped:
                continue

            bullet_match = re.match(r"^[-*]\s+(.*)$", stripped)
            ordered_match = re.match(r"^\d+\.\s+(.*)$", stripped)
            if bullet_match:
                items.append(bullet_match.group(1).strip())
            elif ordered_match:
                items.append(ordered_match.group(1).strip())

        return items

    def _extract_first_heading(self, prd: str) -> str:
        for raw_line in prd.splitlines():
            stripped = raw_line.strip()
            if stripped.startswith("# "):
                return stripped[2:].strip()
        return "Constrained Local Slice"

    def _extract_first_paragraph(self, prd: str) -> str:
        lines: list[str] = []
        seen_heading = False
        for raw_line in prd.splitlines():
            stripped = raw_line.strip()
            if not seen_heading:
                if stripped.startswith("# "):
                    seen_heading = True
                continue
            if not stripped:
                if lines:
                    break
                continue
            if stripped.startswith("#"):
                break
            lines.append(stripped)
        return " ".join(lines).strip()

    def _extract_strict_file_targets(self, prd: str) -> list[str]:
        targets = []
        seen: set[str] = set()
        for item in self._extract_markdown_section_items(prd, "Strict File Targets"):
            normalized = self._normalize_relpath(item)
            if (
                normalized
                and not os.path.isabs(normalized)
                and normalized != ".."
                and not normalized.startswith("../")
                and normalized not in seen
            ):
                targets.append(normalized)
                seen.add(normalized)
        return targets

    def _extract_acceptance_criteria_items(self, prd: str) -> list[str]:
        return self._extract_markdown_section_items(prd, "Acceptance Criteria")

    def _build_constrained_local_requirement(self, prd: str) -> Requirement:
        title = self._extract_first_heading(prd)
        description = self._extract_first_paragraph(prd) or (
            "Implement the strict local-model slice defined in the PRD using only the allowed file targets."
        )
        acceptance_criteria = self._extract_acceptance_criteria_items(prd) or ["Implementation passes targeted checks."]
        return Requirement.create(
            req_type=RequirementType.FUNCTIONAL,
            title=title[:50],
            description=description,
            acceptance_criteria=acceptance_criteria,
            prd_section="Constrained Local Mode",
            priority=8,
            complexity=3,
        )

    def _build_constrained_local_task(self, requirement: Requirement, prd: str) -> TaskContract:
        strict_targets = self._extract_strict_file_targets(prd)
        test_targets = [path for path in strict_targets if path.startswith("tests/") and path.endswith(".py")]
        existing_test_targets = [path for path in test_targets if self._path_exists(path)]
        mutable_targets = [path for path in strict_targets if path not in existing_test_targets]
        if not mutable_targets:
            mutable_targets = strict_targets[:]

        python_targets = []
        for path in [*mutable_targets, *existing_test_targets]:
            if path.endswith(".py") and path not in python_targets:
                python_targets.append(path)

        checks: list[str] = []
        pytest_targets = existing_test_targets or test_targets
        if pytest_targets:
            checks.append(
                "python -m pytest "
                + " ".join(shlex.quote(path) for path in pytest_targets[:2])
                + " -q"
            )
        if python_targets:
            checks.append(
                "python -m ruff check "
                + " ".join(shlex.quote(path) for path in python_targets[:6])
            )

        return TaskContract.create(
            task_type=TaskType.FEATURE,
            goal=f"Implement {requirement.title} using only the strict file targets",
            context=TaskContext(
                prd_refs=[requirement.id],
                files_hint=mutable_targets,
                do_not_touch=existing_test_targets,
            ),
            acceptance=AcceptanceCriteria(
                checks=checks,
                criteria=requirement.acceptance_criteria,
            ),
            constraints=TaskConstraints(
                timebox_minutes=45,
                risk=RiskLevel.LOW,
                merge_policy=MergePolicy.MANUAL,
                retry_max=self.config.worker.retry_max,
            ),
            priority=requirement.priority,
            spec_requirement_ids=[requirement.id],
        )

    def _path_exists(self, rel_path: str) -> bool:
        normalized = self._normalize_relpath(rel_path)
        if not normalized:
            return False
        if os.path.isabs(normalized) or normalized == ".." or normalized.startswith("../"):
            return False
        return os.path.exists(os.path.join(self.repo_path, normalized))

    def _list_available_test_targets(self, limit: int = 200) -> list[str]:
        tests_root = os.path.join(self.repo_path, "tests")
        if not os.path.isdir(tests_root):
            return []

        targets: list[str] = []
        for root, _, files in os.walk(tests_root):
            for filename in files:
                if not filename.endswith(".py"):
                    continue
                if not (filename.startswith("test_") or filename.endswith("_test.py")):
                    continue
                full_path = os.path.join(root, filename)
                rel_path = os.path.relpath(full_path, self.repo_path).replace("\\", "/")
                targets.append(rel_path)
        targets.sort()
        return targets[:limit]

    def _extract_pytest_targets(self, command: str) -> list[str]:
        try:
            tokens = shlex.split(command)
        except ValueError:
            return []
        if not tokens:
            return []

        if tokens[0] == "pytest":
            start_idx = 1
        elif len(tokens) >= 3 and tokens[0] in {"python", "python3"} and tokens[1] == "-m" and tokens[2] == "pytest":
            start_idx = 3
        else:
            return []

        targets: list[str] = []
        skip_next = False
        options_with_values = {
            "-k",
            "-m",
            "--maxfail",
            "--confcutdir",
            "--rootdir",
            "-c",
            "--tb",
            "--capture",
            "-p",
        }

        for token in tokens[start_idx:]:
            if skip_next:
                skip_next = False
                continue
            if token in options_with_values:
                skip_next = True
                continue
            if token.startswith("-"):
                continue
            targets.append(token)
        return targets

    def _build_repo_aware_checks(self, *, checks: list[str], files_hint: list[str]) -> list[str]:
        available_tests = self._list_available_test_targets()
        available_tests_set = set(available_tests)

        normalized_hints = [
            self._normalize_relpath(path)
            for path in files_hint
            if self._normalize_relpath(path)
        ]
        existing_hints = [path for path in normalized_hints if self._path_exists(path)]
        python_hints = [path for path in existing_hints if path.endswith(".py")]

        sanitized: list[str] = []
        seen: set[str] = set()
        for raw_check in checks:
            check = str(raw_check).strip()
            if not check:
                continue
            lower = check.lower()

            if "pytest" in lower:
                targets = self._extract_pytest_targets(check)
                valid_targets: list[str] = []
                for target in targets:
                    rel_target = self._normalize_relpath(target.split("::", 1)[0])
                    if not rel_target:
                        continue
                    if rel_target in available_tests_set or self._path_exists(rel_target):
                        valid_targets.append(target)
                if not valid_targets:
                    continue
                rebuilt = "python -m pytest " + " ".join(shlex.quote(t) for t in valid_targets) + " -q"
                if rebuilt not in seen:
                    sanitized.append(rebuilt)
                    seen.add(rebuilt)
                continue

            if "ruff" in lower and "check" in lower:
                if python_hints:
                    rebuilt = "python -m ruff check " + " ".join(shlex.quote(path) for path in python_hints[:6])
                    if rebuilt not in seen:
                        sanitized.append(rebuilt)
                        seen.add(rebuilt)
                continue

        if not sanitized and available_tests:
            inferred_targets: list[str] = []
            hint_stems = {
                os.path.splitext(os.path.basename(path))[0].lower()
                for path in normalized_hints
                if os.path.basename(path)
            }
            for test_path in available_tests:
                test_name = os.path.splitext(os.path.basename(test_path))[0].lower()
                if any(stem and stem in test_name for stem in hint_stems):
                    inferred_targets.append(test_path)
            if inferred_targets:
                rebuilt = "python -m pytest " + " ".join(shlex.quote(t) for t in inferred_targets[:2]) + " -q"
                sanitized.append(rebuilt)

        return sanitized

    async def _extract_requirements(self, prd: str) -> list[Requirement]:
        """Use LLM to extract requirements from PRD."""
        print(f"  > Extracting requirements from PRD using {self.config.llm.model}...")

        # Truncate PRD if too long for local models
        prd_truncated = prd[:4000] if len(prd) > 4000 else prd

        prompt = f"""Extract requirements from this PRD as JSON.

PRD:
{prd_truncated}

Output a JSON array with 3-7 requirements. Each requirement needs:
- type: "functional" or "non_functional"
- title: Short name
- description: What it does
- acceptance_criteria: ["one test"]
- priority: 5

RESPOND WITH ONLY JSON, NO OTHER TEXT:
[{{"type":"functional","title":"Example","description":"Test","acceptance_criteria":["Works"],"priority":5}}]"""

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
            try:
                # Normalize type field
                req_type = item.get("type", "functional")
                if req_type not in ["functional", "non_functional", "constraint", "acceptance"]:
                    req_type = "functional"

                req = Requirement.create(
                    req_type=RequirementType(req_type),
                    title=item.get("title", "Untitled")[:50],
                    description=item.get("description", ""),
                    acceptance_criteria=item.get("acceptance_criteria", ["Implemented"]),
                    prd_section=item.get("prd_section", "General"),
                    priority=min(10, max(1, int(item.get("priority", 5)))),
                    complexity=min(10, max(1, int(item.get("estimated_complexity", item.get("complexity", 5))))),
                )
                requirements.append(req)
            except Exception as e:
                print(f"  > Warning: Skipped invalid requirement: {e}")
                continue

        if not requirements:
            raise ValueError("No valid requirements extracted from PRD")

        return requirements

    async def _identify_dependencies(
        self,
        requirements: list[Requirement],
    ) -> list[Requirement]:
        """Use LLM to identify dependencies between requirements."""
        # Skip dependency analysis for small requirement sets - not worth the LLM call
        if len(requirements) <= 3:
            print(f"  > Skipping dependency analysis for {len(requirements)} requirements (too few)")
            return requirements

        print(f"  > Identifying dependencies for {len(requirements)} requirements...")
        req_summary = "\n".join(
            f"- {r.id}: {r.title}"
            for r in requirements
        )

        prompt = f"""Which requirements depend on others? Requirements:
{req_summary}

Output JSON with dependencies (or empty if none):
{{"dependencies": {{"REQ-001": ["REQ-002"]}}}}

RESPOND WITH ONLY JSON:"""

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

            # Handle different response formats from LLM
            if isinstance(data, dict):
                deps = data.get("dependencies", {})
            elif isinstance(data, list):
                # LLM returned a list - try to extract dependencies
                deps = {}
                for item in data:
                    if isinstance(item, dict):
                        if "id" in item and "depends_on" in item:
                            deps[item["id"]] = item["depends_on"]
                        elif "dependencies" in item:
                            deps.update(item.get("dependencies", {}))
            else:
                deps = {}

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

        if self._is_constrained_local_mode():
            strict_targets = self._extract_strict_file_targets(prd)
            if strict_targets:
                requirement = self._build_constrained_local_requirement(prd)
                spec = Spec.create(prd_hash)
                spec.add_requirement(requirement)
                self._save_spec(spec)
                self.state = PlannerState(
                    prd_hash=prd_hash,
                    spec_generated=True,
                    last_run=datetime.now(UTC).isoformat(),
                )
                append_trace_span(
                    trace_id=f"planner-{prd_hash}",
                    service="planner",
                    operation="generate_spec",
                    actor_id="planner",
                    attributes={
                        "requirements": 1,
                        "force": force,
                        "prd_hash": prd_hash,
                        "mode": "constrained_local",
                        "strict_targets": len(strict_targets),
                    },
                )
                return spec

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
            last_run=datetime.now(UTC).isoformat(),
        )

        append_trace_span(
            trace_id=f"planner-{prd_hash}",
            service="planner",
            operation="generate_spec",
            actor_id="planner",
            attributes={
                "requirements": len(requirements),
                "force": force,
                "prd_hash": prd_hash,
            },
        )

        return spec

    async def _create_task_for_requirement(
        self,
        requirement: Requirement,
        agents_md: str,
    ) -> list[TaskContract]:
        """Create TaskContracts for a single requirement."""
        prd = self._read_prd()
        if self._is_constrained_local_mode():
            strict_targets = self._extract_strict_file_targets(prd)
            if strict_targets:
                return [self._build_constrained_local_task(requirement, prd)]

        available_tests = self._list_available_test_targets(limit=80)
        available_tests_text = (
            "\n".join(f"- {path}" for path in available_tests)
            if available_tests
            else "- (No test targets discovered under tests/)"
        )
        prompt = f"""Create implementation tasks for this requirement.

Requirement:
- ID: {requirement.id}
- Title: {requirement.title}
- Description: {requirement.description}
- Acceptance Criteria: {json.dumps(requirement.acceptance_criteria)}
- Complexity: {requirement.estimated_complexity}/10

Agent Guidelines (from AGENTS.md):
{agents_md[:2000] if agents_md else "No specific guidelines."}

Existing pytest targets (use only these exact paths):
{available_tests_text}

Break this into 1-3 tasks (prefer fewer, more focused tasks).
Each task should be completable in 30-90 minutes.

Output JSON array:
[
  {{
    "type": "feature|bugfix|test|refactor|ops|doc",
    "goal": "One sentence objective",
    "files_hint": ["suggested/files/to/modify.py"],
    "do_not_touch": ["files/to/avoid.py"],
    "checks": ["pytest tests/existing_test.py::test_case", "ruff check existing/file.py"],
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
            max_tokens=1024,
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
            task_type = self._normalize_task_type(item.get("type", "feature"))

            # Map TaskType to TaskStream for tool whitelisting
            stream_map = {
                TaskType.FEATURE: TaskStream.FEATURE,
                TaskType.BUGFIX: TaskStream.FEATURE,
                TaskType.TEST: TaskStream.QA,
                TaskType.OPS: TaskStream.OPS,
                TaskType.DOC: TaskStream.DOCS,
                TaskType.SECURITY: TaskStream.SECURITY,
                TaskType.REFACTOR: TaskStream.FEATURE,
                TaskType.INTEGRATION: TaskStream.INTEGRATION,
            }
            task_stream = stream_map.get(task_type, TaskStream.FEATURE)

            # Map RiskLevel to RiskTier
            risk_level = self._normalize_risk_level(item.get("risk", "medium"))
            tier_map = {
                RiskLevel.LOW: RiskTier.TIER_0,
                RiskLevel.MEDIUM: RiskTier.TIER_1,
                RiskLevel.HIGH: RiskTier.TIER_2,
            }
            task_tier = tier_map.get(risk_level, RiskTier.TIER_1)

            task = TaskContract.create(
                task_type=task_type,
                goal=item["goal"],
                context=TaskContext(
                    prd_refs=[requirement.id],
                    files_hint=item.get("files_hint", []),
                    do_not_touch=item.get("do_not_touch", []),
                    tools=item.get("tools", []), # Use explicit tools if LLM provided them
                ),
                acceptance=AcceptanceCriteria(
                    checks=self._build_repo_aware_checks(
                        checks=item.get("checks", []),
                        files_hint=item.get("files_hint", []),
                    ),
                    criteria=item.get("criteria", requirement.acceptance_criteria),
                ),
                constraints=TaskConstraints(
                    timebox_minutes=item.get("timebox_minutes", 60),
                    risk=risk_level,
                    merge_policy=self._normalize_merge_policy(item.get("merge_policy", "require_review")),
                    retry_max=self.config.worker.retry_max,
                ),
                priority=requirement.priority,
                spec_requirement_ids=[requirement.id],
                stream=task_stream,
                tier=task_tier,
            )
            tasks.append(task)

        return tasks

    async def generate_tasks(
        self,
        spec: Spec | None = None,
        requirements_filter: list[str] | None = None,
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
        for i, req in enumerate(requirements, 1):
            print(f"  > Generating tasks for requirement {i}/{len(requirements)}: {req.title}...")
            tasks = await self._create_task_for_requirement(req, agents_md)
            prerequisite_task_ids: list[str] = []
            for dependency_id in req.dependencies:
                dependency = spec.get_requirement(dependency_id)
                if dependency is None:
                    continue
                prerequisite_task_ids.extend(dependency.assigned_tasks)
            prerequisite_task_ids = list(dict.fromkeys(prerequisite_task_ids))
            for task in tasks:
                task.prerequisite_requirement_ids = list(req.dependencies)
                task.prerequisite_task_ids = list(prerequisite_task_ids)
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
            snapshot_task(task, repo_path=self.repo_path, actor_id="planner")
            append_trace_span(
                trace_id=task.trace_id,
                service="planner",
                operation="publish_task",
                actor_id="planner",
                task_id=task.id,
                attempt_id=task.attempt_id,
                attributes={
                    "priority": task.priority,
                    "queue_provider": self.config.queue.provider,
                    "spec_ref": task.spec_ref,
                    "intent_anchor_id": task.intent_anchor_id,
                },
            )
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

    async def replan(self, failed_task_ids: list[str] | None = None) -> dict:
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
