"""Helpers for stable name summaries."""

from __future__ import annotations


def summarize_names(raw_names: list[str]) -> dict[str, object]:
    """Return a stripped, sorted, unique name summary.

    Contract:
    - strip whitespace from each input entry
    - drop empty values after stripping
    - names: sorted(unique stripped names) using the final stripped values
    - name_count: len(names) after deduplication
    - implement only this function

    Examples:
    - [" Zeta ", "Alpha", "Alpha", ""] -> {"name_count": 2, "names": ["Alpha", "Zeta"]}
    - ["", "  ", "\t", "\n"] -> {"name_count": 0, "names": []}
    - ["Beta", " beta ", "Gamma"] -> {"name_count": 3, "names": ["Beta", "Gamma", "beta"]}

    Implementation hint:
    - a correct solution can build the final list with
      sorted({name.strip() for name in raw_names if name.strip()})
      and then return {"name_count": len(names), "names": names}
    """
    raise NotImplementedError("Implement name summary generation.")
