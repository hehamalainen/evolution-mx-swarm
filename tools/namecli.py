"""CLI for reporting sorted unique names from a text file."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from namecore import summarize_names


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(
        description="Report sorted unique names from a UTF-8 text file."
    )
    parser.add_argument("path", help="Path to a UTF-8 text file.")
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the summary as JSON.",
    )
    return parser


def build_summary_payload(text: str) -> dict[str, object]:
    """Build the summary payload from the provided file text.

    Contract:
    - split the input with text.splitlines()
    - pass the resulting list directly into summarize_names(...)
    - return exactly the helper result
    - implement only this function; parser/error handling/output are already correct

    Examples:
    - " Zeta \\nAlpha\\nAlpha\\n\\n" should return
      {"name_count": 2, "names": ["Alpha", "Zeta"]}

    Implementation hint:
    - a correct solution is:
      lines = text.splitlines()
      return summarize_names(lines)
    - do not loop over the lines and do not call summarize_names on one line at a time
    """
    _ = summarize_names
    raise NotImplementedError("Implement summary payload generation.")


def format_plain(summary: dict[str, object]) -> str:
    """Render summary in the plain-text format expected by the tests."""
    names = summary.get("names", [])
    rendered_names = ", ".join(str(name) for name in names)
    return "\n".join(
        [
            f"name_count: {summary['name_count']}",
            f"names: {rendered_names}",
        ]
    )


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

    payload = build_summary_payload(text)
    if args.as_json:
        print(json.dumps(payload))
    else:
        print(format_plain(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
