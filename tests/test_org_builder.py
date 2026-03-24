from boss.org_builder import OrgBuilder, OrgConstraints, WorkCategory, WorkPackage
from shared.schemas.teamplan import TeamPlan


def test_detect_categories_uses_boundaries_and_path_hints_for_python_cli() -> None:
    builder = OrgBuilder()
    prd = """
    # Textstats CLI

    Return only file blocks in this exact format.
    Only these files may be changed:
    - tools/textstats.py
    - tests/test_textstats.py
    """

    categories = builder._detect_categories(prd.lower(), ["tools/textstats.py", "tests/test_textstats.py"])

    assert WorkCategory.API in categories
    assert WorkCategory.UI not in categories


def test_deterministic_scores_are_structural() -> None:
    builder = OrgBuilder()
    package = WorkPackage(
        id="WP-001",
        title="CLI implementation",
        category=WorkCategory.API,
        description="Implement API functionality",
        target_files=["tools/jsonpeek.py"],
        target_toolchains=["python"],
        has_pre_existing_tests=True,
        has_strict_file_targets=True,
        is_single_file_scope=True,
        dependency_depth=1,
    )

    determinism = builder.calculate_determinism(package)
    novelty = builder.calculate_novelty(package)

    assert 0.0 <= determinism <= 1.0
    assert 0.0 <= novelty <= 1.0
    assert determinism > novelty


def test_build_teamplan_embeds_rig_plans_and_preserves_stream_runtime() -> None:
    builder = OrgBuilder()
    prd = """
    # Product
    Implement login, billing, and audit logging.

    Requirements:
    - authentication for users
    - stripe subscription billing
    - security audit logs
    - tests with pytest
    - touch auth/service.py and billing/stripe_client.py
    """

    teamplan = builder.build_teamplan(
        prd,
        OrgConstraints(max_budget_per_day=25.0, max_workers=20),
    )

    assert isinstance(teamplan, TeamPlan)
    assert teamplan.topology.value == "streams"
    assert teamplan.streams
    assert teamplan.rig_plans
    assert any(stream.name == "security" for stream in teamplan.streams)
    assert any(
        rig["rig_id"].startswith("rig-") and rig["streams"]
        for rig in teamplan.rig_plans
    )


def test_compile_rig_plans_emits_frontier_eligible_security_cluster() -> None:
    builder = OrgBuilder()
    prd = """
    # Scope
    Build OAuth login, payment subscriptions, and external webhook integration.
    No strict file paths are defined.
    """
    analysis = builder.analyze_prd(prd)
    rig_plans = builder.compile_rig_plans(analysis, OrgConstraints(max_budget_per_day=30.0, max_workers=30))

    assert rig_plans
    security_or_platform = [
        rig for rig in rig_plans if "platform" in rig.rig_id or "integration" in rig.rig_id
    ]
    assert security_or_platform
    assert any(
        "frontier" in shard.allowed_model_tiers
        for rig in security_or_platform
        for stream in rig.streams
        for shard in stream.shards
    )


def test_compile_rig_plans_prefers_python_toolchain_for_textstats_cli() -> None:
    builder = OrgBuilder()
    prd = """
    # Textstats CLI

    Build a small CLI tool called `textstats`.

    Only these files may be changed:
    - tools/textstats.py
    - tests/test_textstats.py

    Tests must pass with pytest.
    Return only file blocks in this exact format.
    """

    teamplan = builder.build_teamplan(
        prd,
        OrgConstraints(max_budget_per_day=25.0, max_workers=10),
    )

    execution_shards = [
        shard
        for rig in teamplan.rig_plans
        for stream in rig["streams"]
        if stream["stream_type"] == "execution"
        for shard in stream["shards"]
    ]

    assert execution_shards
    assert any("python" in shard["supported_toolchains"] for shard in execution_shards)
    assert all("typescript" not in shard["supported_toolchains"] for shard in execution_shards)
