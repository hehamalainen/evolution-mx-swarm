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
import math
import re
from dataclasses import dataclass, field
from enum import Enum

from shared.schemas.org_topology import RigPlan, ShardPlan, StreamPlan
from shared.schemas.teamplan import (
    BudgetPolicy,
    CapsConfig,
    MergePolicy,
    ObservabilityConfig,
    ReorgPolicy,
    ResetTriggers,
    RetryPolicy,
    RiskMergePolicy,
    StreamConfig,
    TaskPolicy,
    TeamPlan,
    Topology,
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
    dependencies: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    risk_indicators: list[str] = field(default_factory=list)
    estimated_complexity: float = 0.5  # 0-1
    target_files: list[str] = field(default_factory=list)
    target_toolchains: list[str] = field(default_factory=list)
    has_pre_existing_tests: bool = False
    has_strict_file_targets: bool = False
    is_single_file_scope: bool = False
    dependency_depth: int = 0
    determinism_score: float = 0.0
    novelty_score: float = 0.0
    structural_risk_score: float = 0.0


@dataclass
class GoalCluster:
    """Deterministic cluster of related work packages for rig synthesis."""

    cluster_id: str
    package_ids: list[str]
    categories: list[WorkCategory]
    target_task_classes: list[str]
    supported_toolchains: list[str]
    determinism_score: float
    novelty_score: float
    risk_score: float
    coupling_density: float
    primary_runtime_stream: str
    requires_security_stream: bool
    requires_ops_stream: bool


@dataclass
class PRDAnalysis:
    """Results of PRD analysis."""
    work_packages: list[WorkPackage]
    categories_touched: list[WorkCategory]
    dependency_graph: dict[str, list[str]]  # id -> [dependency_ids]

    # Computed scores
    complexity_score: float = 0.0    # 0-1: how much work
    coupling_score: float = 0.0      # 0-1: how interconnected
    risk_score: float = 0.0          # 0-1: auth/payments/security
    determinism_score: float = 0.0   # 0-1: higher means stricter/easier to bound
    novelty_score: float = 0.0       # 0-1: higher means less like admitted corpus

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
    deadline_hours: int | None = None
    allowed_models: list[str] = field(default_factory=lambda: ["claude-sonnet-4-20250514"])
    repo_url: str | None = None
    ci_provider: str = "github"
    staging_url: str | None = None
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
    TEST_KEYWORDS = ["pytest", "unit test", "integration test", "test coverage", "tests/"]
    FILE_HINT_RE = re.compile(r"\b(?:[\w.-]+/)*[\w.-]+\.[A-Za-z0-9]+\b")
    CATEGORY_ORDER = [
        WorkCategory.AUTH,
        WorkCategory.PAYMENTS,
        WorkCategory.SECURITY,
        WorkCategory.DATABASE,
        WorkCategory.API,
        WorkCategory.UI,
        WorkCategory.INFRA,
        WorkCategory.INTEGRATION,
        WorkCategory.OBSERVABILITY,
        WorkCategory.DOCS,
    ]

    # Coupling indicators
    COUPLING_KEYWORDS = ["depends on", "requires", "after", "before", "shared", "common", "reuse"]

    def __init__(self, llm_client: LLMClient | None = None):
        self.llm = llm_client

    def analyze_prd(self, prd_content: str, dod_content: str | None = None) -> PRDAnalysis:
        """
        Analyze PRD to extract work structure and compute scores.

        Uses a combination of:
        - Keyword detection (fast, deterministic)
        - LLM analysis (optional, for deeper understanding)
        """
        prd_lower = prd_content.lower()

        explicit_path_hints = self._extract_path_hints(prd_content)

        # Detect categories touched
        categories = self._detect_categories(prd_lower, explicit_path_hints)

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

        has_pre_existing_tests = any(keyword in prd_lower for keyword in self.TEST_KEYWORDS)

        # Extract work packages (simplified - would use LLM in production)
        work_packages = self._extract_work_packages(
            prd_content,
            categories,
            explicit_path_hints=explicit_path_hints,
            has_pre_existing_tests=has_pre_existing_tests,
        )

        # Build dependency graph
        dependency_graph = self._build_dependency_graph(work_packages)
        for package in work_packages:
            package.dependency_depth = self._estimate_dependency_depth(package.id, dependency_graph)
            package.determinism_score = self.calculate_determinism(package)
            package.novelty_score = self.calculate_novelty(package)
            package.structural_risk_score = self._calculate_structural_risk(package)

        determinism_score = (
            sum(package.determinism_score for package in work_packages) / max(len(work_packages), 1)
        )
        novelty_score = (
            sum(package.novelty_score for package in work_packages) / max(len(work_packages), 1)
        )

        return PRDAnalysis(
            work_packages=work_packages,
            categories_touched=categories,
            dependency_graph=dependency_graph,
            complexity_score=complexity_score,
            coupling_score=coupling_score,
            risk_score=risk_score,
            determinism_score=determinism_score,
            novelty_score=novelty_score,
            has_auth=has_auth,
            has_payments=has_payments,
            has_pii=has_pii,
            has_external_integrations=has_external,
            estimated_tasks=estimated_tasks
        )

    def _detect_categories(self, prd_lower: str, explicit_path_hints: list[str]) -> list[WorkCategory]:
        """Detect which work categories are present."""
        categories: set[WorkCategory] = set()

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
            if any(self._keyword_present(prd_lower, kw) for kw in keywords):
                categories.add(category)

        for path in explicit_path_hints:
            categories.update(self._infer_categories_from_path(path))

        if not categories:
            return [WorkCategory.API]
        return self._sort_categories(categories)

    def _extract_work_packages(
        self,
        prd_content: str,
        categories: list[WorkCategory],
        *,
        explicit_path_hints: list[str],
        has_pre_existing_tests: bool,
    ) -> list[WorkPackage]:
        """Extract work packages from PRD sections."""
        packages = []

        # Simple extraction: one package per major category
        for i, category in enumerate(categories):
            category_paths = [
                path for path in explicit_path_hints if self._path_matches_category(path, category)
            ]
            if not category_paths and len(categories) == 1:
                category_paths = list(explicit_path_hints)
            inferred_toolchains = self._infer_toolchains_from_paths(category_paths)
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
                estimated_complexity=0.5 + (0.1 * len(categories)),
                target_files=category_paths[:4],
                target_toolchains=inferred_toolchains or self._category_target_toolchains(category),
                has_pre_existing_tests=has_pre_existing_tests,
                has_strict_file_targets=bool(category_paths),
                is_single_file_scope=len(category_paths) == 1,
            )
            packages.append(pkg)

        return packages

    def _keyword_present(self, text: str, keyword: str) -> bool:
        pattern = re.compile(rf"(?<![a-z0-9]){re.escape(keyword.lower())}(?![a-z0-9])")
        return bool(pattern.search(text))

    def _sort_categories(self, categories: set[WorkCategory]) -> list[WorkCategory]:
        order = {category: index for index, category in enumerate(self.CATEGORY_ORDER)}
        return sorted(categories, key=lambda category: order.get(category, len(order)))

    def _infer_categories_from_path(self, path: str) -> list[WorkCategory]:
        lowered = path.lower()
        inferred: list[WorkCategory] = []

        token_map = {
            WorkCategory.AUTH: ("auth", "login", "session", "token"),
            WorkCategory.PAYMENTS: ("payment", "billing", "stripe", "invoice"),
            WorkCategory.SECURITY: ("security", "scan", "policy"),
            WorkCategory.DATABASE: ("db", "database", "schema", "migration", ".sql"),
            WorkCategory.INTEGRATION: ("integration", "webhook", "connector"),
            WorkCategory.OBSERVABILITY: ("metric", "trace", "monitor", "observability"),
            WorkCategory.DOCS: ("docs", "readme", ".md"),
        }
        for category, tokens in token_map.items():
            if any(token in lowered for token in tokens):
                inferred.append(category)

        if lowered.endswith((".tsx", ".jsx", ".ts", ".js", ".css", ".html")):
            inferred.append(WorkCategory.UI)
        elif lowered.endswith(".py"):
            inferred.append(WorkCategory.API)
        elif lowered.endswith((".yml", ".yaml", ".toml", ".sh")):
            inferred.append(WorkCategory.INFRA)

        deduped: list[WorkCategory] = []
        for category in inferred:
            if category not in deduped:
                deduped.append(category)
        return deduped

    def _extract_path_hints(self, prd_content: str) -> list[str]:
        hints = []
        seen: set[str] = set()
        for match in self.FILE_HINT_RE.findall(prd_content):
            normalized = match.strip().strip("`")
            if "." not in normalized or normalized.lower().startswith("http"):
                continue
            if normalized not in seen:
                hints.append(normalized)
                seen.add(normalized)
        return hints

    def _path_matches_category(self, path: str, category: WorkCategory) -> bool:
        return category in self._infer_categories_from_path(path)

    def _infer_toolchains_from_paths(self, paths: list[str]) -> list[str]:
        inferred: list[str] = []
        for path in paths:
            lowered = path.lower()
            if lowered.endswith(".py"):
                inferred.append("python")
            elif lowered.endswith((".ts", ".tsx", ".js", ".jsx")):
                inferred.append("typescript")
            elif lowered.endswith(".sql"):
                inferred.append("sql")
            elif lowered.endswith(".sh"):
                inferred.append("shell")
            elif lowered.endswith((".yml", ".yaml")):
                inferred.append("yaml")
            elif lowered.endswith(".md"):
                inferred.append("markdown")
            elif lowered.endswith(".toml"):
                inferred.append("toml")
        deduped: list[str] = []
        for toolchain in inferred:
            if toolchain not in deduped:
                deduped.append(toolchain)
        return deduped

    def _get_risk_indicators(self, category: WorkCategory) -> list[str]:
        """Get risk indicators for a category."""
        high_risk_categories = {
            WorkCategory.AUTH: ["credential handling", "session management"],
            WorkCategory.PAYMENTS: ["financial data", "pci compliance"],
            WorkCategory.SECURITY: ["vulnerability surface", "access control"],
            WorkCategory.DATABASE: ["data integrity", "migration risk"]
        }
        return high_risk_categories.get(category, [])

    def _build_dependency_graph(self, packages: list[WorkPackage]) -> dict[str, list[str]]:
        """Build dependency graph from work packages."""
        return {pkg.id: pkg.dependencies for pkg in packages}

    def _estimate_dependency_depth(self, package_id: str, graph: dict[str, list[str]]) -> int:
        seen: set[str] = set()

        def walk(node_id: str) -> int:
            if node_id in seen:
                return 0
            seen.add(node_id)
            dependencies = graph.get(node_id, [])
            if not dependencies:
                return 0
            return 1 + max(walk(dependency) for dependency in dependencies)

        return walk(package_id)

    def calculate_determinism(self, package: WorkPackage) -> float:
        score = 0.1
        if package.has_strict_file_targets:
            score += 0.3
        if package.has_pre_existing_tests:
            score += 0.3
        if package.is_single_file_scope:
            score += 0.2
        if package.category in {WorkCategory.DOCS, WorkCategory.OBSERVABILITY}:
            score += 0.1
        if package.category in {WorkCategory.AUTH, WorkCategory.PAYMENTS, WorkCategory.INTEGRATION}:
            score -= 0.1
        if package.dependency_depth > 2:
            score -= 0.1
        return max(0.0, min(1.0, score))

    def calculate_novelty(self, package: WorkPackage) -> float:
        score = 0.1
        if package.dependency_depth > 3:
            score += 0.3
        elif package.dependency_depth > 1:
            score += 0.15
        if not package.target_files:
            score += 0.3
        if package.category in {
            WorkCategory.AUTH,
            WorkCategory.PAYMENTS,
            WorkCategory.SECURITY,
            WorkCategory.INTEGRATION,
        }:
            score += 0.2
        if package.has_strict_file_targets:
            score -= 0.1
        return max(0.0, min(1.0, score))

    def _calculate_structural_risk(self, package: WorkPackage) -> float:
        score = min(1.0, 0.3 + (0.15 * len(package.risk_indicators)))
        if package.category in {
            WorkCategory.AUTH,
            WorkCategory.PAYMENTS,
            WorkCategory.SECURITY,
            WorkCategory.DATABASE,
        }:
            score += 0.25
        if package.dependency_depth > 2:
            score += 0.1
        return max(0.0, min(1.0, score))

    def _category_target_toolchains(self, category: WorkCategory) -> list[str]:
        toolchains = {
            WorkCategory.UI: ["typescript"],
            WorkCategory.API: ["python"],
            WorkCategory.DATABASE: ["python", "sql"],
            WorkCategory.AUTH: ["python"],
            WorkCategory.PAYMENTS: ["python"],
            WorkCategory.SECURITY: ["python", "shell"],
            WorkCategory.INFRA: ["shell", "yaml"],
            WorkCategory.INTEGRATION: ["python", "yaml"],
            WorkCategory.DOCS: ["markdown"],
            WorkCategory.OBSERVABILITY: ["python", "yaml"],
        }
        return toolchains.get(category, ["python"])

    def _category_task_classes(self, category: WorkCategory) -> list[str]:
        mapping = {
            WorkCategory.DOCS: ["doc"],
            WorkCategory.INFRA: ["ops"],
            WorkCategory.SECURITY: ["security"],
            WorkCategory.INTEGRATION: ["integration"],
        }
        return mapping.get(category, ["feature"])

    def _cluster_key_for_package(self, package: WorkPackage) -> str:
        if package.category in {WorkCategory.AUTH, WorkCategory.PAYMENTS, WorkCategory.SECURITY}:
            return "platform_core"
        if package.category == WorkCategory.UI:
            return "frontend"
        if package.category in {WorkCategory.API, WorkCategory.DATABASE}:
            return "backend"
        if package.category in {WorkCategory.INFRA, WorkCategory.OBSERVABILITY}:
            return "ops"
        if package.category == WorkCategory.INTEGRATION:
            return "integration"
        if package.category == WorkCategory.DOCS:
            return "docs"
        return package.category.value

    def _primary_runtime_stream(self, categories: set[WorkCategory], cluster_id: str) -> str:
        if categories == {WorkCategory.DOCS}:
            return "docs"
        if cluster_id == "integration":
            return "integration"
        if cluster_id == "ops":
            return "ops"
        return "feature"

    def _compute_cluster_coupling(
        self,
        package_ids: list[str],
        dependency_graph: dict[str, list[str]],
        global_coupling_score: float,
    ) -> float:
        if len(package_ids) <= 1:
            return global_coupling_score
        package_set = set(package_ids)
        internal_edges = sum(
            1
            for package_id in package_ids
            for dependency in dependency_graph.get(package_id, [])
            if dependency in package_set
        )
        possible_edges = max(1, len(package_ids) * (len(package_ids) - 1))
        normalized = internal_edges / possible_edges
        return max(global_coupling_score * 0.5, min(1.0, normalized + (global_coupling_score * 0.5)))

    def _compile_goal_clusters(self, analysis: PRDAnalysis) -> list[GoalCluster]:
        buckets: dict[str, list[WorkPackage]] = {}
        for package in analysis.work_packages:
            buckets.setdefault(self._cluster_key_for_package(package), []).append(package)

        clusters: list[GoalCluster] = []
        for cluster_id, packages in buckets.items():
            categories = {package.category for package in packages}
            package_ids = [package.id for package in packages]
            determinism = sum(package.determinism_score for package in packages) / max(len(packages), 1)
            novelty = sum(package.novelty_score for package in packages) / max(len(packages), 1)
            risk = sum(package.structural_risk_score for package in packages) / max(len(packages), 1)
            coupling = self._compute_cluster_coupling(package_ids, analysis.dependency_graph, analysis.coupling_score)
            target_task_classes = sorted(
                {task_class for package in packages for task_class in self._category_task_classes(package.category)}
            )
            supported_toolchains = sorted(
                {toolchain for package in packages for toolchain in package.target_toolchains}
            )
            requires_security_stream = (
                risk > 0.7
                or any(category in {WorkCategory.AUTH, WorkCategory.PAYMENTS, WorkCategory.SECURITY} for category in categories)
            )
            requires_ops_stream = any(
                category in {WorkCategory.INFRA, WorkCategory.OBSERVABILITY, WorkCategory.INTEGRATION}
                for category in categories
            )
            clusters.append(
                GoalCluster(
                    cluster_id=cluster_id,
                    package_ids=package_ids,
                    categories=sorted(categories, key=lambda item: item.value),
                    target_task_classes=target_task_classes,
                    supported_toolchains=supported_toolchains or ["python"],
                    determinism_score=determinism,
                    novelty_score=novelty,
                    risk_score=risk,
                    coupling_density=coupling,
                    primary_runtime_stream=self._primary_runtime_stream(categories, cluster_id),
                    requires_security_stream=requires_security_stream,
                    requires_ops_stream=requires_ops_stream,
                )
            )
        return clusters

    def compile_rig_plans(self, analysis: PRDAnalysis, constraints: OrgConstraints) -> list[RigPlan]:
        clusters = self._compile_goal_clusters(analysis)
        if not clusters:
            return []

        base_width = max(1, constraints.max_workers // max(len(clusters), 1))
        base_rig_budget = constraints.max_budget_per_day / max(len(clusters), 1)
        rig_plans: list[RigPlan] = []

        for cluster in clusters:
            width_factor = cluster.determinism_score / max(cluster.coupling_density + 0.1, 0.1)
            risk_factor = 1 - (cluster.risk_score * 0.5)
            target_width = max(1, math.floor(base_width * width_factor * risk_factor))
            target_width = min(target_width, max(1, base_width * 2), constraints.max_workers)
            qa_density = min(1.0, 0.15 + (0.35 * cluster.risk_score) + (0.2 * cluster.novelty_score) + (0.15 * cluster.coupling_density))
            qa_workers = max(1, math.ceil(target_width * qa_density))
            security_workers = max(1, math.ceil(target_width * max(0.2, cluster.risk_score * 0.35)))
            ops_workers = max(1, math.ceil(target_width * 0.2))
            economic_risk = min(1.0, (cluster.risk_score * 0.6) + (cluster.novelty_score * 0.4))
            frontier_ratio = min(
                1.0,
                (cluster.novelty_score + (1 - cluster.determinism_score))
                / max(0.2, 2 * (economic_risk + 0.1)),
            )
            frontier_budget_cap = round(base_rig_budget * frontier_ratio, 2)
            max_mutable_files = 1 if cluster.determinism_score >= 0.8 else 2 if cluster.determinism_score >= 0.5 else 4
            if cluster.coupling_density >= 0.7 or cluster.risk_score >= 0.75:
                max_mutable_files = min(max_mutable_files, 2)

            allowed_tiers = ["local_open_weight"]
            if cluster.novelty_score > 0.45 or cluster.determinism_score < 0.45 or cluster.risk_score > 0.7:
                allowed_tiers.append("frontier")

            streams = [
                StreamPlan(
                    stream_id=f"{cluster.cluster_id}-execution",
                    stream_type="execution",
                    qa_density_ratio=round(qa_density, 3),
                    shards=[
                        ShardPlan(
                            shard_id=f"{cluster.cluster_id}.execution",
                            target_task_classes=cluster.target_task_classes,
                            supported_toolchains=cluster.supported_toolchains,
                            max_mutable_files=max_mutable_files,
                            concurrency_cap=target_width,
                            freeze_condition_threshold=2 if cluster.risk_score > 0.75 else 3,
                            allowed_model_tiers=allowed_tiers,
                        )
                    ],
                ),
                StreamPlan(
                    stream_id=f"{cluster.cluster_id}-qa",
                    stream_type="qa_verification",
                    qa_density_ratio=round(qa_density, 3),
                    shards=[
                        ShardPlan(
                            shard_id=f"{cluster.cluster_id}.qa",
                            target_task_classes=["test", "bugfix"],
                            supported_toolchains=cluster.supported_toolchains,
                            max_mutable_files=2,
                            concurrency_cap=qa_workers,
                            freeze_condition_threshold=3,
                            allowed_model_tiers=["local_open_weight"],
                        )
                    ],
                ),
            ]

            if cluster.requires_security_stream:
                streams.append(
                    StreamPlan(
                        stream_id=f"{cluster.cluster_id}-security",
                        stream_type="security_audit",
                        qa_density_ratio=round(qa_density, 3),
                        shards=[
                            ShardPlan(
                                shard_id=f"{cluster.cluster_id}.security",
                                target_task_classes=["security"],
                                supported_toolchains=cluster.supported_toolchains,
                                max_mutable_files=2,
                                concurrency_cap=security_workers,
                                freeze_condition_threshold=2,
                                allowed_model_tiers=["local_open_weight", "frontier"],
                            )
                        ],
                    )
                )

            if cluster.requires_ops_stream and cluster.primary_runtime_stream != "ops":
                streams.append(
                    StreamPlan(
                        stream_id=f"{cluster.cluster_id}-ops",
                        stream_type="ops",
                        qa_density_ratio=round(qa_density, 3),
                        shards=[
                            ShardPlan(
                                shard_id=f"{cluster.cluster_id}.ops",
                                target_task_classes=["ops", "integration"],
                                supported_toolchains=cluster.supported_toolchains,
                                max_mutable_files=2,
                                concurrency_cap=ops_workers,
                                freeze_condition_threshold=3,
                                allowed_model_tiers=["local_open_weight"],
                            )
                        ],
                    )
                )

            rig_plans.append(
                RigPlan(
                    rig_id=f"rig-{cluster.cluster_id}",
                    goal_cluster_node_ids=cluster.package_ids,
                    frontier_budget_cap_usd=frontier_budget_cap,
                    governance_owner_id=f"governor:{cluster.cluster_id}",
                    streams=streams,
                )
            )

        return rig_plans

    def _aggregate_streams_from_rig_plans(
        self,
        rig_plans: list[RigPlan],
        constraints: OrgConstraints,
    ) -> list[StreamConfig]:
        if not rig_plans:
            return []

        stream_totals: dict[str, int] = {
            "feature": 0,
            "qa": 0,
            "ops": 0,
            "security": 0,
            "docs": 0,
            "integration": 0,
        }

        for rig_plan in rig_plans:
            for stream in rig_plan.streams:
                total = sum(shard.concurrency_cap for shard in stream.shards)
                if stream.stream_type == "qa_verification":
                    stream_totals["qa"] += total
                    continue
                if stream.stream_type == "security_audit":
                    stream_totals["security"] += total
                    continue
                if stream.stream_type == "ops":
                    stream_totals["ops"] += total
                    continue

                shard = stream.shards[0]
                target_classes = set(shard.target_task_classes)
                if target_classes == {"doc"}:
                    stream_totals["docs"] += total
                elif "integration" in target_classes and "feature" not in target_classes:
                    stream_totals["integration"] += total
                elif target_classes == {"ops"}:
                    stream_totals["ops"] += total
                else:
                    stream_totals["feature"] += total

        stream_specs = {
            "feature": ("feature_engineer", ["git", "tests", "ci", "read", "write"], 2),
            "qa": ("qa_engineer", ["git", "tests", "ci", "read"], 3),
            "ops": ("ops_engineer", ["git", "ci", "deploy", "observability"], 2),
            "security": ("security_engineer", ["git", "ci", "scan", "read"], 4),
            "docs": ("docs_engineer", ["git", "read", "write"], 1),
            "integration": ("integration_engineer", ["git", "tests", "ci", "read", "write"], 2),
        }

        streams: list[StreamConfig] = []
        total_workers = 0
        for name, target in stream_totals.items():
            if target <= 0:
                continue
            role, tools, priority = stream_specs[name]
            max_workers = max(target, math.ceil(target * 1.4))
            streams.append(
                StreamConfig(
                    name=name,
                    role=role,
                    target_workers=target,
                    max_workers=max_workers,
                    tools=tools,
                    priority=priority,
                )
            )
            total_workers += target

        if total_workers > constraints.max_workers and total_workers > 0:
            scale = constraints.max_workers / total_workers
            for stream_cfg in streams:
                scaled_target = max(1, math.floor(stream_cfg.target_workers * scale))
                stream_cfg.target_workers = scaled_target
                stream_cfg.max_workers = max(scaled_target, math.ceil(scaled_target * 1.4))

        return streams

    def build_teamplan(
        self,
        prd_content: str,
        constraints: OrgConstraints,
        dod_content: str | None = None
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

        # Step 3: Compile deterministic rig plans and derive current stream staffing.
        rig_plans = self.compile_rig_plans(analysis, constraints)
        streams = self._aggregate_streams_from_rig_plans(rig_plans, constraints) or self._staff_streams(
            analysis,
            constraints,
        )

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
            rig_plans=[plan.model_dump(mode="json") for plan in rig_plans],
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
            risk_score=analysis.risk_score,
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

    def _staff_streams(self, analysis: PRDAnalysis, constraints: OrgConstraints) -> list[StreamConfig]:
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
        def compute_workers(pct: float) -> tuple[int, int]:
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
    constraints: OrgConstraints | None = None,
    dod_path: str | None = None,
    llm_client: LLMClient | None = None
) -> TeamPlan:
    """
    Convenience function to build TEAMPLAN from file paths.

    Usage:
        teamplan = await build_teamplan_from_prd("./PRD.md", constraints)
        teamplan.save("./TEAMPLAN.json")
    """
    # Read PRD
    with open(prd_path) as f:
        prd_content = f.read()

    # Read DoD if provided
    dod_content = None
    if dod_path:
        with open(dod_path) as f:
            dod_content = f.read()

    # Use default constraints if not provided
    if constraints is None:
        constraints = OrgConstraints()

    # Build teamplan
    builder = OrgBuilder(llm_client)
    return builder.build_teamplan(prd_content, constraints, dod_content)
