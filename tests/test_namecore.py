from tools.namecore import summarize_names


def test_summarize_names_sorts_and_deduplicates() -> None:
    assert summarize_names([" Zeta ", "Alpha", "Alpha", ""]) == {
        "name_count": 2,
        "names": ["Alpha", "Zeta"],
    }


def test_summarize_names_drops_blank_entries() -> None:
    assert summarize_names(["", "  ", "\t", "\n"]) == {
        "name_count": 0,
        "names": [],
    }


def test_summarize_names_keeps_case_sensitive_values() -> None:
    assert summarize_names(["Beta", " beta ", "Gamma"]) == {
        "name_count": 3,
        "names": ["Beta", "Gamma", "beta"],
    }
