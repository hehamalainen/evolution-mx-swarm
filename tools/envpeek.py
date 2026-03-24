"""CLI for reporting environment variable keys from a .env-style file."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ENV_KEY_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(
        description="Report environment variable key metadata from a .env file."
    )
    parser.add_argument("path", help="Path to a UTF-8 .env file.")
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the summary as JSON.",
    )
    return parser


def read_env_keys(path: Path) -> list[str]:
    """Read valid keys from a .env file and reject malformed syntax."""
    keys: list[str] = []
    for lineno, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"line {lineno} is missing '='")
        key, _value = line.split("=", 1)
        key = key.strip()
        if not ENV_KEY_PATTERN.match(key):
            raise ValueError(f"line {lineno} has invalid key {key!r}")
        keys.append(key)
    return keys


def summarize_env_keys(keys: list[str]) -> dict[str, object]:
    """Return the sorted key summary for the provided environment keys.

    Contract:
    - keys: sorted(unique keys)
    - key_count: len(keys)
    - implement only this function; parser/error handling/output formatting are already correct
    """
    raise NotImplementedError("Implement .env key summary generation.")


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
        keys = read_env_keys(path)
    except FileNotFoundError:
        print(f"error: file not found: {path}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: invalid env: {exc}", file=sys.stderr)
        return 1

    summary = summarize_env_keys(keys)
    if args.as_json:
        print(json.dumps(summary))
    else:
        print(format_plain(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
