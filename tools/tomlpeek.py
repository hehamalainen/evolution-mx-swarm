"""CLI for reporting top-level key metadata from a TOML file."""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(
        description="Report top-level key metadata for a TOML file."
    )
    parser.add_argument("path", help="Path to a TOML file.")
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the summary as JSON.",
    )
    return parser


def read_toml_document(path: Path) -> dict[str, object]:
    """Read the requested file as TOML and require a mapping payload."""
    with path.open("rb") as handle:
        payload = tomllib.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("top-level TOML value must be a mapping")
    return payload


def summarize_document(payload: dict[str, object]) -> dict[str, object]:
    """Return the sorted top-level key summary for the provided document.

    Contract:
    - keys: sorted(payload.keys())
    - key_count: len(keys)
    - implement only this function; parser/error handling/output formatting are already correct
    """
    raise NotImplementedError("Implement TOML summary generation.")


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
        payload = read_toml_document(path)
    except FileNotFoundError:
        print(f"error: file not found: {path}", file=sys.stderr)
        return 1
    except (tomllib.TOMLDecodeError, ValueError) as exc:
        print(f"error: invalid toml: {exc}", file=sys.stderr)
        return 1

    summary = summarize_document(payload)
    if args.as_json:
        print(json.dumps(summary))
    else:
        print(format_plain(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
