"""
Org Builder - PRD to TEAMPLAN Synthesis

This module analyzes a PRD and generates a TEAMPLAN.json that defines
the entire organizational structure for the swarm execution.

Key steps:
1. Parse PRD and extract work structure
2. Build dependency graph (SPEC/Work DAG)
3. Compute scores: complexity, coupling, risk
4. Choose topology based on scores
5. Staff role mix using heuristics
6. Emit TEAMPLAN.json
"""

import hashlib
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from enum import Enum

from shared.schemas.teamplan import (
    TeamPlan, Topology, StreamConfig, PodConfig,
    CapsConfig, RiskMergePolicy, MergePolicy,
    TaskPolicy, RetryPolicy, BudgetPolicy,
    ReorgPolicy, ResetTriggers, ObservabilityConfig
)
from shared.utils.llm import LLMClient


class WorkCategory(str, Enum):
    """Categories of work detected in PRD."""
    UI = "ui"
    API = "api"
    DATABASE = "database"
    AUTH = "auth"
    PAYMENTS = "payments"
    SECURITY = "security"
    INFRA = "infra"
    INTEGRATION = "integration"
    DOCS = "docs"
    OBSERVABILITY = "observability"


@dataclass
class WorkPackage:
    """A unit of work extracted from PRD."""
    id: str
    title: str
    category: WorkCategory
    description: str
    dependencies: List[str] = field(default_factory=list)
    acceptance_criteria: List[str] = field(default_factory=list)
    risk_indicators: List[str] = field(default_factory=list)
    estimated_complexity: float = 0.5  # 0-1


@dataclass
class PRDAnalysis:
    """Results of PRD analysis."""
    work_packages: List[WorkPackage]
    categories_touched: List[WorkCategory]
    dependency_graph: Dict[str, List[str]]  # id -> [dependency_ids]

    # Computed scores
    complexity_score: float = 0.0    # 0-1: how much work
    coupling_score: float = 0.0      # 0-1: how interconnected
    risk_score: float = 0.0          # 0-1: auth/payments/security

    # Detected constraints
    has_auth: bool = False
    has_payments: bool = False
    has_pii: bool = False
    has_external_integrations: bool = False
    estimated_tasks: int = 0


@dataclass
class OrgConstraints:
    """Constraints provided for org building."""
    max_budget_per_day: float = 50.0
    max_workers: int = 100
    deadline_hours: Optional[int] = None
    allowed_models: List[str] = field(default_factory=lambda: ["claude-sonnet-4-20250514"])
    repo_url: Optional[str] = None
    ci_provider: str = "github"
    staging_url: Optional[str] = None
    require_manual_review_tier2: bool = True


class OrgBuilder:
    """
    Builds TEAMPLAN from PRD analysis.

    This is the "org synthesizer" that decides team structure
    and governance based on what needs to be built.
    """

    # Risk keywords for detection
    RISK_KEYWORDS = {
        "auth": ["authentication", "login", "password", "oauth", "jwt", "session", "token", "credentials"],
        "payments": ["payment", "billing", "stripe", "subscription", "credit card", "checkout", "pricing"],
        "security": ["security", "encryption", "vulnerability", "sanitize", "xss", "csrf", "injection"],
        "pii": ["personal data", "pii", "gdpr", "privacy", "user data", "sensitive"]
    }

    # Coupling indicators
    COUPLING_KEYWORDS = ["depends on", "requires", "after", "before", "shared", "common", "reuse"]

    def __init__(self, llm_client: Optional[LLMClient] = None):
        self.llm = llm_client

    def analyze_prd(self, prd_content: str, dod_content: Optional[str] = None) -> PRDAnalysis:
        """
        Analyze PRD to extract work structure and compute scores.

        Uses a combination of:
        - Keyword detection (fast, deterministic)
        - LLM analysis (optional, for deeper understanding)
        """
        prd_lower = prd_content.lower()

        # Detect categories touched
        categories = self._detect_categories(prd_lower)

        # Detect risk indicators
        has_auth = any(kw in prd_lower for kw in self.RISK_KEYWORDS["auth"])
        has_payments = any(kw in prd_lower for kw in self.RISK_KEYWORDS["payments"])
        has_pii = any(kw in prd_lower for kw in self.RISK_KEYWORDS["pii"])
        has_security = any(kw in prd_lower for kw in self.RISK_KEYWORDS["security"])

        # Detect external integrations
        integration_patterns = ["api", "webhook", "third-party", "external", "integration"]
        has_external = any(p in prd_lower for p in integration_patterns)

        # Compute coupling score based on keyword density
        coupling_mentions = sum(1 for kw in self.COUPLING_KEYWORDS if kw in prd_lower)
        coupling_score = min(coupling_mentions / 10, 1.0)

        # Compute complexity based on document length and section count
        word_count = len(prd_content.split())
        section_count = prd_content.count("#")
        complexity_score = min((word_count / 3000 + section_count / 20) / 2, 1.0)

        # Compute risk score
        risk_factors = sum([has_auth, has_payments, has_pii, has_security])
        risk_score = min(risk_factors / 3, 1.0)

        # Estimate tasks based on complexity
        estimated_tasks = max(10, int(complexity_score * 100))

        # Extract work packages (simplified - would use LLM in production)
        work_packages = self._extract_work_packages(prd_content, categories)

        # Build dependency graph
        dependency_graph = self._build_dependency_graph(work_packages)

        return PRDAnalysis(
            work_packages=work_packages,
            categories_touched=categories,
            dependency_graph=dependency_graph,
            complexity_score=complexity_score,
            coupling_score=coupling_score,
            risk_score=risk_score,
            has_auth=has_auth,
            has_payments=has_payments,
            has_pii=has_pii,
            has_external_integrations=has_external,
            estimated_tasks=estimated_tasks
        )

    def _detect_categories(self, prd_lower: str) -> List[WorkCategory]:
        """Detect which work categories are present."""
        categories = []

        category_keywords = {
            WorkCategory.UI: ["frontend", "ui", "ux", "component", "page", "form", "button", "modal"],
            WorkCategory.API: ["api", "endpoint", "rest", "graphql", "route", "controller"],
            WorkCategory.DATABASE: ["database", "schema", "migration", "model", "table", "index"],
            WorkCategory.AUTH: ["auth", "login", "permission", "role", "access"],
            WorkCategory.PAYMENTS: ["payment", "billing", "subscription", "invoice"],
            WorkCategory.SECURITY: ["security", "encryption", "audit", "compliance"],
            WorkCategory.INFRA: ["deploy", "infrastructure", "docker", "kubernetes", "ci/cd"],
            WorkCategory.INTEGRATION: ["integration", "webhook", "third-party", "sync"],
            WorkCategory.DOCS: ["documentation", "readme", "guide", "tutorial"],
            WorkCategory.OBSERVABILITY: ["logging", "monitoring", "metrics", "alerting", "tracing"]
        }

        for category, keywords in category_keywords.items():
            if any(kw in prd_lower for kw in keywords):
                categories.append(category)

        return categories if categories else [WorkCategory.API, WorkCategory.UI]

    def _extract_work_packages(self, prd_content: str, categories: List[WorkCategory]) -> List[WorkPackage]:
        """Extract work packages from PRD sections."""
        packages = []

        # Simple extraction: one package per major category
        for i, category in enumerate(categories):
            pkg = WorkPackage(
                id=f"WP-{i+1:03d}",
                title=f"{category.value.title()} Implementation",
                category=category,
                description=f"Implement {category.value} functionality as specified in PRD",
                dependencies=[f"WP-{j+1:03d}" for j in range(i) if j < 3],  # Simplified deps
                acceptance_criteria=[
                    f"{category.value.title()} functionality works as specified",
                    "Tests pass",
                    "Documentation updated"
                ],
                risk_indicators=self._get_risk_indicators(category),
                estimated_complexity=0.5 + (0.1 * len(categories))
            )
            packages.append(pkg)

        return packages

    def _get_risk_indicators(self, category: WorkCategory) -> List[str]:
        """Get risk indicators for a category."""
        high_risk_categories = {
            WorkCategory.AUTH: ["credential handling", "session management"],
            WorkCategory.PAYMENTS: ["financial data", "pci compliance"],
            WorkCategory.SECURITY: ["vulnerability surface", "access control"],
            WorkCategory.DATABASE: ["data integrity", "migration risk"]
        }
        return high_risk_categories.get(category, [])

    def _build_dependency_graph(self, packages: List[WorkPackage]) -> Dict[str, List[str]]:
        """Build dependency graph from work packages."""
        return {pkg.id: pkg.dependencies for pkg in packages}

    def build_teamplan(
        self,
        prd_content: str,
        constraints: OrgConstraints,
        dod_content: Optional[str] = None
    ) -> TeamPlan:
        """
        Main entry point: Build TEAMPLAN from PRD + constraints.

        Steps:
        1. Analyze PRD
        2. Choose topology
        3. Staff role mix
        4. Set governance caps
        5. Configure policies
        """
        # Step 1: Analyze PRD
        analysis = self.analyze_prd(prd_content, dod_content)

        # Step 2: Choose topology
        topology = self._choose_topology(analysis)

        # Step 3: Staff role mix
        streams = self._staff_streams(analysis, constraints)

        # Step 4: Set governance caps
        caps = self._compute_caps(analysis, constraints)

        # Step 5: Configure policies
        risk_policy = self._compute_risk_policy(analysis, constraints)
        task_policy = self._compute_task_policy(analysis)
        budget_policy = self._compute_budget_policy(constraints)
        reorg_policy = self._compute_reorg_policy(analysis)
        reset_triggers = self._compute_reset_triggers(analysis)

        # Build TEAMPLAN
        teamplan = TeamPlan(
            version="0.1",
            topology=topology,
            streams=streams,
            caps=caps,
            risk_merge_policy=risk_policy,
            task_policy=task_policy,
            retry_policy=RetryPolicy(),
            budget_policy=budget_policy,
            reorg_policy=reorg_policy,
            reset_triggers=reset_triggers,
            observability=ObservabilityConfig(),
            prd_hash=hashlib.sha256(prd_content.encode()).hexdigest()[:16],
            complexity_score=analysis.complexity_score,
            coupling_score=analysis.coupling_score,
            risk_score=analysis.risk_score
        )

        return teamplan

    def _choose_topology(self, analysis: PRDAnalysis) -> Topology:
        """Choose organizational topology based on analysis."""
        # V1: Always use streams (simpler, good for most cases)
        # V2: Would add pods for high-coupling scenarios

        if analysis.coupling_score > 0.7:
            # High coupling - would benefit from pods, but v1 uses streams
            # with tighter caps and more QA emphasis
            pass

        return Topology.STREAMS

    def _staff_streams(self, analysis: PRDAnalysis, constraints: OrgConstraints) -> List[StreamConfig]:
        """
        Staff role streams based on PRD analysis.

        Base mix: Feature 50-60%, QA 20-30%, Ops 10%, Security 5-10%, Docs 5-10%

        Adjustments:
        - High coupling: +QA, -Feature parallelism
        - High risk: +Security, +QA, smaller task sizes
        - External integrations: +Ops
        """
        max_workers = constraints.max_workers

        # Base percentages
        feature_pct = 0.55
        qa_pct = 0.25
        ops_pct = 0.10
        security_pct = 0.05
        docs_pct = 0.05

        # Adjust for coupling
        if analysis.coupling_score > 0.5:
            feature_pct -= 0.10
            qa_pct += 0.10

        # Adjust for risk
        if analysis.risk_score > 0.5:
            security_pct += 0.05
            qa_pct += 0.05
            feature_pct -= 0.10

        # Adjust for auth/payments
        if analysis.has_auth or analysis.has_payments:
            security_pct += 0.05
            feature_pct -= 0.05

        # Adjust for external integrations
        if analysis.has_external_integrations:
            ops_pct += 0.05
            feature_pct -= 0.05

        # Compute worker counts
        def compute_workers(pct: float) -> Tuple[int, int]:
            target = max(1, int(max_workers * pct))
            max_w = max(target, int(target * 1.4))
            return target, max_w

        streams = [
            StreamConfig(
                name="feature",
                role="feature_engineer",
                target_workers=compute_workers(feature_pct)[0],
                max_workers=compute_workers(feature_pct)[1],
                tools=["git", "tests", "ci", "read", "write"],
                priority=2
            ),
            StreamConfig(
                name="qa",
                role="qa_engineer",
                target_workers=compute_workers(qa_pct)[0],
                max_workers=compute_workers(qa_pct)[1],
                tools=["git", "tests", "ci", "read"],
                priority=3  # Higher priority for quality
            ),
            StreamConfig(
                name="ops",
                role="ops_engineer",
                target_workers=compute_workers(ops_pct)[0],
                max_workers=compute_workers(ops_pct)[1],
                tools=["git", "ci", "deploy", "observability"],
                priority=2
            ),
            StreamConfig(
                name="security",
                role="security_engineer",
                target_workers=compute_workers(security_pct)[0],
                max_workers=compute_workers(security_pct)[1],
                tools=["git", "ci", "scan", "read"],
                priority=4  # Highest priority
            ),
            StreamConfig(
                name="docs",
                role="docs_engineer",
                target_workers=compute_workers(docs_pct)[0],
                max_workers=compute_workers(docs_pct)[1],
                tools=["git", "read", "write"],
                priority=1  # Lower priority
            )
        ]

        return streams

    def _compute_caps(self, analysis: PRDAnalysis, constraints: OrgConstraints) -> CapsConfig:
        """Compute governance caps based on analysis."""
        # Base caps
        pr_cap_per_hour = 15
        open_pr_cap = 25
        max_active_tasks = 60

        # Tighten for high coupling (more merge conflicts expected)
        if analysis.coupling_score > 0.5:
            pr_cap_per_hour = 10
            open_pr_cap = 15

        # Tighten for high risk (need more careful review)
        if analysis.risk_score > 0.5:
            pr_cap_per_hour = 8
            open_pr_cap = 12

        # Scale with worker count
        max_active_tasks = min(60, constraints.max_workers)

        return CapsConfig(
            pr_cap_per_hour=pr_cap_per_hour,
            open_pr_cap=open_pr_cap,
            max_parallel_ci=10,
            max_active_tasks=max_active_tasks
        )

    def _compute_risk_policy(self, analysis: PRDAnalysis, constraints: OrgConstraints) -> RiskMergePolicy:
        """Compute merge policy by risk tier."""
        # Default: tier0 auto, tier1 with flag, tier2 manual
        tier2_policy = MergePolicy.MANUAL_REVIEW_REQUIRED

        # If high risk overall, make tier1 manual too
        if analysis.risk_score > 0.7:
            return RiskMergePolicy(
                tier0=MergePolicy.AUTO_ON_GREEN_FEATURE_FLAG,
                tier1=MergePolicy.MANUAL_REVIEW_REQUIRED,
                tier2=MergePolicy.MANUAL_REVIEW_REQUIRED
            )

        return RiskMergePolicy()

    def _compute_task_policy(self, analysis: PRDAnalysis) -> TaskPolicy:
        """Compute task constraints."""
        # Smaller tasks for high coupling (reduce conflict surface)
        max_files = 12 if analysis.coupling_score < 0.5 else 8
        max_modules = 3 if analysis.coupling_score < 0.5 else 2
        max_minutes = 45 if analysis.complexity_score < 0.5 else 60

        return TaskPolicy(
            requires_spec_ref=True,
            max_files_touched=max_files,
            max_modules_touched=max_modules,
            max_task_minutes=max_minutes,
            require_tests=True,
            require_evidence=True
        )

    def _compute_budget_policy(self, constraints: OrgConstraints) -> BudgetPolicy:
        """Compute budget policy from constraints."""
        return BudgetPolicy(
            usd_per_day=constraints.max_budget_per_day,
            usd_per_hour_soft=constraints.max_budget_per_day / 8,  # ~8 hour day
            hard_stop=True,
            burn_alert_thresholds=[50, 75, 90]
        )

    def _compute_reorg_policy(self, analysis: PRDAnalysis) -> ReorgPolicy:
        """Compute reorg policy."""
        # More frequent checks for complex/coupled projects
        interval = 60 if analysis.complexity_score < 0.5 else 45
        hysteresis = 30 if analysis.coupling_score < 0.5 else 20

        return ReorgPolicy(
            interval_minutes=interval,
            hysteresis_minutes=hysteresis,
            max_worker_delta_per_interval=20,
            prefer_caps_before_reorg=True,
            allow_stream_merge=False,
            allow_stream_split=False
        )

    def _compute_reset_triggers(self, analysis: PRDAnalysis) -> ResetTriggers:
        """Compute reset trigger thresholds."""
        # Stricter thresholds for risky projects
        ci_threshold = 85 if analysis.risk_score < 0.5 else 90
        nack_threshold = 25 if analysis.coupling_score < 0.5 else 20

        return ResetTriggers(
            ci_health_lt=ci_threshold,
            nack_rate_gt=nack_threshold,
            stall_minutes_gt=60,
            conflict_rate_gt=30,
            retry_cascade_count=5
        )


async def build_teamplan_from_prd(
    prd_path: str,
    constraints: Optional[OrgConstraints] = None,
    dod_path: Optional[str] = None,
    llm_client: Optional[LLMClient] = None
) -> TeamPlan:
    """
    Convenience function to build TEAMPLAN from file paths.

    Usage:
        teamplan = await build_teamplan_from_prd("./PRD.md", constraints)
        teamplan.save("./TEAMPLAN.json")
    """
    # Read PRD
    with open(prd_path, 'r') as f:
        prd_content = f.read()

    # Read DoD if provided
    dod_content = None
    if dod_path:
        with open(dod_path, 'r') as f:
            dod_content = f.read()

    # Use default constraints if not provided
    if constraints is None:
        constraints = OrgConstraints()

    # Build teamplan
    builder = OrgBuilder(llm_client)
    return builder.build_teamplan(prd_content, constraints, dod_content)
