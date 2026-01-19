"""Agent Boss - Intelligent Team Composition and Orchestration.

The Agent Boss is the strategic layer that:
1. Analyzes PRD to understand scope and complexity
2. Decides team composition (roles, skills, count)
3. Allocates budget based on estimated effort
4. Ignites and monitors the swarm
5. Makes strategic decisions during execution
"""

import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from shared.schemas import Spec, TaskType
from shared.utils import MXConfig, LLMClient, Message, create_llm_client
from planner import Planner


class ParallelizationStrategy(str, Enum):
    MAX = "max"                    # Full parallelization, fastest
    CONSERVATIVE = "conservative"  # Moderate parallelization, safer
    PHASED = "phased"             # Sequential phases, most controlled
    ADAPTIVE = "adaptive"          # Adjust based on success rate


class RiskTolerance(str, Enum):
    LOW = "low"           # Require human review for everything
    MEDIUM = "medium"     # Auto-merge low-risk, review medium+
    HIGH = "high"         # Auto-merge low and medium risk


@dataclass
class AgentRole:
    """Definition of a specialized agent role."""
    name: str
    description: str
    count: int
    skills: list[str]
    system_prompt: str
    task_types: list[TaskType]  # What task types this role handles
    tools: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "count": self.count,
            "skills": self.skills,
            "system_prompt": self.system_prompt,
            "task_types": [t.value for t in self.task_types],
            "tools": self.tools,
        }


@dataclass
class TeamComposition:
    """Complete team composition decided by Agent Boss."""
    # Sizing
    estimated_complexity: int          # 1-10
    estimated_tasks: int               # Number of tasks expected
    recommended_workers: int           # Total worker count

    # Budget
    budget_usd: float
    cost_per_task_estimate: float
    max_daily_spend: float

    # Team
    roles: list[AgentRole]

    # Strategy
    parallelization_strategy: ParallelizationStrategy
    risk_tolerance: RiskTolerance

    # Timing
    estimated_duration_hours: float    # With swarm
    without_swarm_days: float          # Without swarm (single dev)

    # Metadata
    reasoning: str                     # Why these decisions
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def to_dict(self) -> dict:
        return {
            "estimated_complexity": self.estimated_complexity,
            "estimated_tasks": self.estimated_tasks,
            "recommended_workers": self.recommended_workers,
            "budget_usd": self.budget_usd,
            "cost_per_task_estimate": self.cost_per_task_estimate,
            "max_daily_spend": self.max_daily_spend,
            "roles": [r.to_dict() for r in self.roles],
            "parallelization_strategy": self.parallelization_strategy.value,
            "risk_tolerance": self.risk_tolerance.value,
            "estimated_duration_hours": self.estimated_duration_hours,
            "without_swarm_days": self.without_swarm_days,
            "reasoning": self.reasoning,
            "created_at": self.created_at,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    def summary(self) -> str:
        """Human-readable summary."""
        return f"""
═══════════════════════════════════════════════════════════════
                    TEAM COMPOSITION PLAN
═══════════════════════════════════════════════════════════════

SIZING
  Complexity:        {self.estimated_complexity}/10
  Estimated Tasks:   {self.estimated_tasks}
  Workers:           {self.recommended_workers}

BUDGET
  Total Budget:      ${self.budget_usd:.2f}
  Per Task:          ${self.cost_per_task_estimate:.2f}
  Daily Cap:         ${self.max_daily_spend:.2f}

TIMELINE
  With Swarm:        {self.estimated_duration_hours:.1f} hours
  Without Swarm:     {self.without_swarm_days:.1f} days
  Speedup:           {(self.without_swarm_days * 24 / self.estimated_duration_hours):.1f}x

TEAM ROLES
{self._format_roles()}

STRATEGY
  Parallelization:   {self.parallelization_strategy.value}
  Risk Tolerance:    {self.risk_tolerance.value}

REASONING
{self.reasoning}

═══════════════════════════════════════════════════════════════
"""

    def _format_roles(self) -> str:
        lines = []
        for role in self.roles:
            lines.append(f"  • {role.name} (x{role.count})")
            lines.append(f"    Skills: {', '.join(role.skills[:5])}")
        return "\n".join(lines)


# Predefined role templates
ROLE_TEMPLATES = {
    "backend_engineer": AgentRole(
        name="Backend Engineer",
        description="Builds APIs, services, and backend logic",
        count=1,
        skills=["python", "fastapi", "django", "postgresql", "redis", "rest-api"],
        system_prompt="""You are a senior backend engineer. Focus on:
- Clean, well-structured API design
- Proper error handling and validation
- Database schema design and queries
- Performance and scalability
- Security best practices""",
        task_types=[TaskType.FEATURE, TaskType.BUGFIX, TaskType.REFACTOR],
        tools=["read", "write", "bash", "grep"],
    ),

    "frontend_engineer": AgentRole(
        name="Frontend Engineer",
        description="Builds UI components and frontend logic",
        count=1,
        skills=["typescript", "react", "nextjs", "css", "tailwind", "accessibility"],
        system_prompt="""You are a senior frontend engineer. Focus on:
- Component-based architecture
- Responsive and accessible design
- State management
- Performance optimization
- User experience""",
        task_types=[TaskType.FEATURE, TaskType.BUGFIX, TaskType.REFACTOR],
        tools=["read", "write", "bash"],
    ),

    "devops_engineer": AgentRole(
        name="DevOps Engineer",
        description="Handles infrastructure, CI/CD, and deployment",
        count=1,
        skills=["docker", "kubernetes", "terraform", "github-actions", "gcp", "aws"],
        system_prompt="""You are a senior DevOps engineer. Focus on:
- Infrastructure as code
- CI/CD pipeline design
- Container orchestration
- Monitoring and observability
- Security and compliance""",
        task_types=[TaskType.OPS, TaskType.FEATURE],
        tools=["read", "write", "bash"],
    ),

    "test_engineer": AgentRole(
        name="Test Engineer",
        description="Writes tests and ensures quality",
        count=1,
        skills=["pytest", "jest", "cypress", "testing", "mocking", "coverage"],
        system_prompt="""You are a senior test engineer. Focus on:
- Comprehensive test coverage
- Unit, integration, and e2e tests
- Test data management
- Mocking and fixtures
- CI integration""",
        task_types=[TaskType.TEST],
        tools=["read", "write", "bash"],
    ),

    "docs_writer": AgentRole(
        name="Documentation Writer",
        description="Writes documentation and guides",
        count=1,
        skills=["markdown", "technical-writing", "api-docs", "tutorials"],
        system_prompt="""You are a technical writer. Focus on:
- Clear, concise documentation
- Code examples and tutorials
- API reference documentation
- Architecture diagrams
- User guides""",
        task_types=[TaskType.DOC],
        tools=["read", "write"],
    ),

    "security_engineer": AgentRole(
        name="Security Engineer",
        description="Reviews and implements security measures",
        count=1,
        skills=["security", "owasp", "authentication", "encryption", "auditing"],
        system_prompt="""You are a security engineer. Focus on:
- Security best practices
- Vulnerability assessment
- Authentication and authorization
- Data protection
- Compliance requirements""",
        task_types=[TaskType.FEATURE, TaskType.BUGFIX, TaskType.REFACTOR],
        tools=["read", "write", "bash", "grep"],
    ),

    "fullstack_generalist": AgentRole(
        name="Fullstack Generalist",
        description="Handles any task across the stack",
        count=1,
        skills=["python", "typescript", "react", "databases", "apis", "testing"],
        system_prompt="""You are a senior fullstack engineer. You can handle:
- Frontend and backend development
- Database design and queries
- API development
- Testing and documentation
- DevOps tasks when needed""",
        task_types=[TaskType.FEATURE, TaskType.BUGFIX, TaskType.TEST, TaskType.REFACTOR, TaskType.DOC],
        tools=["read", "write", "bash", "grep"],
    ),
}


AGENT_BOSS_SYSTEM_PROMPT = """You are the Agent Boss - a strategic AI that analyzes projects and assembles optimal teams.

Your job is to:
1. Deeply analyze the PRD to understand scope, complexity, and requirements
2. Estimate the effort required (tasks, time, budget)
3. Design the optimal team composition
4. Decide on parallelization and risk strategies

You think like a seasoned engineering manager who has shipped hundreds of products.
You balance speed with quality, cost with thoroughness.

When analyzing a PRD, consider:
- Technical domains involved (backend, frontend, infra, etc.)
- Integration complexity
- Testing requirements
- Security considerations
- Documentation needs

Output your analysis as structured JSON."""


class AgentBoss:
    """
    Agent Boss - The strategic orchestration layer.

    Analyzes PRDs and makes intelligent decisions about:
    - Team size and composition
    - Budget allocation
    - Parallelization strategy
    - Risk tolerance
    """

    def __init__(
        self,
        config: MXConfig,
        llm: Optional[LLMClient] = None,
        repo_path: str = ".",
    ):
        self.config = config
        self.llm = llm or create_llm_client(config.llm.to_dict())
        self.repo_path = repo_path

    def _read_prd(self) -> str:
        """Read PRD.md from repo."""
        prd_path = os.path.join(self.repo_path, "PRD.md")
        if not os.path.exists(prd_path):
            raise FileNotFoundError(f"PRD.md not found at {prd_path}")
        with open(prd_path) as f:
            return f.read()

    async def analyze_prd(self, prd: Optional[str] = None) -> TeamComposition:
        """
        Analyze PRD and generate team composition.

        This is the core intelligence of Agent Boss.
        """
        if prd is None:
            prd = self._read_prd()

        prompt = f"""Analyze this PRD and design the optimal team composition.

PRD:
{prd}

Available role templates:
{json.dumps(list(ROLE_TEMPLATES.keys()), indent=2)}

Analyze and respond with JSON:
{{
    "complexity": <1-10>,
    "estimated_tasks": <number>,
    "technical_domains": ["backend", "frontend", ...],
    "roles_needed": [
        {{"template": "backend_engineer", "count": 3, "justification": "..."}},
        ...
    ],
    "parallelization_strategy": "max|conservative|phased|adaptive",
    "risk_tolerance": "low|medium|high",
    "estimated_hours_with_swarm": <number>,
    "estimated_days_without_swarm": <number>,
    "budget_recommendation_usd": <number>,
    "reasoning": "Detailed explanation of your decisions..."
}}

Consider:
- More complex = more specialized roles
- High parallelization = more workers of each role
- Security-sensitive = lower risk tolerance
- Tight deadline = max parallelization + higher budget
"""

        response = await self.llm.chat(
            messages=[Message(role="user", content=prompt)],
            system=AGENT_BOSS_SYSTEM_PROMPT,
            max_tokens=4000,
            temperature=0.3,
        )

        # Parse response
        content = response.content.strip()
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0]
        elif "```" in content:
            content = content.split("```")[1].split("```")[0]

        try:
            analysis = json.loads(content)
        except json.JSONDecodeError:
            # Fallback to default composition
            return self._default_composition()

        # Build team composition
        roles = []
        total_workers = 0

        for role_spec in analysis.get("roles_needed", []):
            template_name = role_spec.get("template", "fullstack_generalist")
            count = role_spec.get("count", 1)

            if template_name in ROLE_TEMPLATES:
                role = ROLE_TEMPLATES[template_name]
                # Create copy with specified count
                roles.append(AgentRole(
                    name=role.name,
                    description=role.description,
                    count=count,
                    skills=role.skills,
                    system_prompt=role.system_prompt,
                    task_types=role.task_types,
                    tools=role.tools,
                ))
                total_workers += count

        # Ensure at least one generalist if no roles specified
        if not roles:
            roles.append(ROLE_TEMPLATES["fullstack_generalist"])
            total_workers = 5

        # Calculate budget
        estimated_tasks = analysis.get("estimated_tasks", 50)
        cost_per_task = 1.50  # Rough estimate: ~$1.50 per task with Claude
        budget = analysis.get("budget_recommendation_usd", estimated_tasks * cost_per_task)

        return TeamComposition(
            estimated_complexity=analysis.get("complexity", 5),
            estimated_tasks=estimated_tasks,
            recommended_workers=min(total_workers, 100),  # Cap at 100
            budget_usd=budget,
            cost_per_task_estimate=cost_per_task,
            max_daily_spend=budget / max(1, analysis.get("estimated_hours_with_swarm", 24) / 24),
            roles=roles,
            parallelization_strategy=ParallelizationStrategy(
                analysis.get("parallelization_strategy", "adaptive")
            ),
            risk_tolerance=RiskTolerance(
                analysis.get("risk_tolerance", "medium")
            ),
            estimated_duration_hours=analysis.get("estimated_hours_with_swarm", 24),
            without_swarm_days=analysis.get("estimated_days_without_swarm", 14),
            reasoning=analysis.get("reasoning", "Default analysis"),
        )

    def _default_composition(self) -> TeamComposition:
        """Fallback default team composition."""
        return TeamComposition(
            estimated_complexity=5,
            estimated_tasks=30,
            recommended_workers=10,
            budget_usd=100.0,
            cost_per_task_estimate=1.50,
            max_daily_spend=50.0,
            roles=[
                ROLE_TEMPLATES["fullstack_generalist"],
                ROLE_TEMPLATES["test_engineer"],
            ],
            parallelization_strategy=ParallelizationStrategy.ADAPTIVE,
            risk_tolerance=RiskTolerance.MEDIUM,
            estimated_duration_hours=24,
            without_swarm_days=7,
            reasoning="Default composition - PRD analysis failed",
        )

    def apply_composition(self, composition: TeamComposition) -> MXConfig:
        """Apply team composition to config."""
        config = self.config

        # Update worker settings
        config.worker.max_workers = composition.recommended_workers

        # Update budget
        config.budget.daily_usd_cap = composition.max_daily_spend

        # Update risk/merge settings
        if composition.risk_tolerance == RiskTolerance.HIGH:
            config.github.auto_merge_low_risk = True
        elif composition.risk_tolerance == RiskTolerance.LOW:
            config.github.auto_merge_low_risk = False

        # Save composition to repo
        comp_path = os.path.join(self.repo_path, "TEAM_COMPOSITION.json")
        with open(comp_path, "w") as f:
            f.write(composition.to_json())

        config.save()
        return config

    async def ignite_swarm(
        self,
        composition: Optional[TeamComposition] = None,
        dry_run: bool = False,
    ) -> dict:
        """
        Ignite the swarm with the given team composition.

        This is the "big red button" that starts everything.
        """
        # Analyze PRD if no composition provided
        if composition is None:
            print("Agent Boss analyzing PRD...")
            composition = await self.analyze_prd()

        print(composition.summary())

        if dry_run:
            return {
                "status": "dry_run",
                "composition": composition.to_dict(),
            }

        # Apply composition to config
        config = self.apply_composition(composition)

        # Run planner to generate tasks
        print("\nAgent Boss: Initiating Planner...")
        from judge import Judge
        planner = Planner(config, repo_path=self.repo_path)
        plan_result = await planner.run()

        print(f"  Generated {plan_result['tasks_generated']} tasks")
        print(f"  Published {plan_result['tasks_published']} to queue")

        return {
            "status": "ignited",
            "composition": composition.to_dict(),
            "plan_result": plan_result,
            "message": f"Swarm ignited with {composition.recommended_workers} workers",
        }


async def main():
    """CLI entry point for Agent Boss."""
    import argparse

    parser = argparse.ArgumentParser(description="MX Swarm Agent Boss")
    parser.add_argument("--config", default="mx.config.json", help="Config file path")
    parser.add_argument("--repo", default=".", help="Repository path")
    parser.add_argument("--analyze-only", action="store_true", help="Only analyze, don't ignite")
    parser.add_argument("--dry-run", action="store_true", help="Show plan without executing")
    args = parser.parse_args()

    config = MXConfig.load(args.config)
    boss = AgentBoss(config, repo_path=args.repo)

    if args.analyze_only:
        composition = await boss.analyze_prd()
        print(composition.summary())
    else:
        result = await boss.ignite_swarm(dry_run=args.dry_run)
        print(f"\nResult: {result['status']}")
        if result['status'] == 'ignited':
            print(f"Message: {result['message']}")


if __name__ == "__main__":
    asyncio.run(main())
