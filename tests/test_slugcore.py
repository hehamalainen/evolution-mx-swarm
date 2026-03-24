from tools.slugcore import normalize_slug


def test_normalize_slug_lowers_and_collapses_separators() -> None:
    assert normalize_slug("  Hello, World!  ") == "hello-world"


def test_normalize_slug_handles_mixed_symbols() -> None:
    assert normalize_slug("MX Swarm / Aether :: Proof") == "mx-swarm-aether-proof"


def test_normalize_slug_strips_empty_edges() -> None:
    assert normalize_slug("---Already-Clean---") == "already-clean"
