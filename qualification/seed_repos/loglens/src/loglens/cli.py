"""Command-line entry point for the loglens seed app."""

from __future__ import annotations

import argparse
import sys

from loglens.parsing import load_entries
from loglens.registry import COMMANDS, discover_commands
from loglens.runtime import render_result


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level parser and register discovered commands."""
    discover_commands()

    parser = argparse.ArgumentParser(
        prog="loglens",
        description="Analyze structured log files with plugin-style subcommands.",
    )
    parser.add_argument("logfile", help="Path to the log file to analyze.")
    subparsers = parser.add_subparsers(dest="command")

    for name, spec in sorted(COMMANDS.items()):
        subparser = subparsers.add_parser(name, help=spec.help_text, description=spec.help_text)
        spec.configure(subparser)
        subparser.set_defaults(_handler=spec.handler)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the seed CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)

    handler = getattr(args, "_handler", None)
    if handler is None:
        parser.print_help(sys.stderr)
        return 2

    try:
        entries = load_entries(args.logfile)
        result = handler(entries, args)
        print(render_result(result, output_format=getattr(args, "format", "plain")))
    except FileNotFoundError:
        print(f"error: file not found: {args.logfile}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except NotImplementedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
