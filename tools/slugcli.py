"""CLI for reporting a normalized slug from a text file."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from slugcore import normalize_slug


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(description="Report a normalized slug from a UTF-8 text file.")
    parser.add_argument("path", help="Path to a UTF-8 text file.")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Emit structured JSON.")
    return parser


def format_output(text: str) -> dict[str, str]:
    """Build the CLI payload from the input text.

    Contract:
    - slug: normalize_slug(text)
    - implement only this function; parser/error handling/output are already correct
    - return exactly {"slug": <normalized slug>}
    """
    _ = normalize_slug
    raise NotImplementedError("Implement slug CLI formatting.")


def main(argv: list[str] | None = None) -> int:
    """Run the CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)
    path = Path(args.path)

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"error: file not found: {path}", file=sys.stderr)
        return 1

    payload = format_output(text)
    if args.as_json:
        print(json.dumps(payload))
    else:
        print(f"slug: {payload['slug']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
