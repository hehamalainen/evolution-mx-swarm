"""Helpers for stable slug normalization."""

from __future__ import annotations


def normalize_slug(value: str) -> str:
    """Return a normalized slug for the provided input text.

    Contract:
    - lowercase the input
    - trim surrounding whitespace
    - treat every non-alphanumeric character, including existing "-", as a separator
    - collapse each run of separators into exactly one "-"
    - strip leading and trailing "-"
    - implement only this function

    Examples:
    - "  Hello, World!  " -> "hello-world"
    - "MX Swarm / Aether :: Proof" -> "mx-swarm-aether-proof"
    - "---Already-Clean---" -> "already-clean"

    Implementation hint:
    - a correct solution can lowercase/strip first, then use
      re.sub(r"[^a-z0-9]+", "-", value), and finally strip("-")
    """
    raise NotImplementedError("Implement slug normalization.")
