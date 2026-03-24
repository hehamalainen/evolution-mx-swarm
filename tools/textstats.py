"""CLI for reporting line, word, and character counts for a text file."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(
        description="Report line, word, and character counts for a text file."
    )
    parser.add_argument("path", help="Path to a UTF-8 text file.")
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the counts as JSON.",
    )
    return parser


def read_text(path: Path) -> str:
    """Read the requested file as UTF-8 text."""
    return path.read_text(encoding="utf-8")


def count_text(text: str) -> dict[str, int]:
    r"""Return line, word, and character counts for the provided text.

    Contract:
    - lines: len(text.splitlines())
    - words: len(re.findall(r'\w+', text))
    - chars: len(text)
    """
    lines = len(text.splitlines())
    words = len(re.findall(r"\w+", text))
    chars = len(text)
    return {"lines": lines, "words": words, "chars": chars}


def format_plain(stats: dict[str, int]) -> str:
    """Render stats in the plain-text format expected by the tests."""
    return "\n".join(f"{name}: {stats[name]}" for name in ("lines", "words", "chars"))


def main(argv: list[str] | None = None) -> int:
    """Run the CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)
    path = Path(args.path)

    try:
        text = read_text(path)
    except FileNotFoundError:
        print(f"error: file not found: {path}", file=sys.stderr)
        return 1

    stats = count_text(text)
    if args.as_json:
        print(json.dumps(stats))
    else:
        print(format_plain(stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
