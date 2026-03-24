"""CLI for reporting top-level key metadata from a JSON object file."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(
        description="Report top-level key metadata for a JSON object file."
    )
    parser.add_argument("path", help="Path to a UTF-8 JSON file.")
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the summary as JSON.",
    )
    return parser


def read_json_object(path: Path) -> dict[str, object]:
    """Read the requested file as JSON and require an object payload."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("top-level JSON value must be an object")
    return payload


def summarize_object(payload: dict[str, object]) -> dict[str, object]:
    """Return the sorted key summary for the provided object.

    Contract:
    - keys: sorted(payload.keys())
    - key_count: len(keys)
    - use only Python's standard library and built-in sorted(...)
    - implement only this function; parser/error handling/output formatting are already correct
    """
    raise NotImplementedError("Implement JSON summary generation.")


def format_plain(summary: dict[str, object]) -> str:
    """Render summary in the plain-text format expected by the tests."""
    keys = summary.get("keys", [])
    rendered_keys = ", ".join(str(key) for key in keys)
    return "\n".join(
        [
            f"key_count: {summary['key_count']}",
            f"keys: {rendered_keys}",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    """Run the CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)
    path = Path(args.path)

    try:
        payload = read_json_object(path)
    except FileNotFoundError:
        print(f"error: file not found: {path}", file=sys.stderr)
        return 1
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"error: invalid json: {exc}", file=sys.stderr)
        return 1

    summary = summarize_object(payload)
    if args.as_json:
        print(json.dumps(summary))
    else:
        print(format_plain(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
