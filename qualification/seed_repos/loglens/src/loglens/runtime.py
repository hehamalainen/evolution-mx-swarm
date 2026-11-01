"""Shared command result and rendering helpers."""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from loglens.shared import formatting

OUTPUT_FORMATS = ("plain", "table", "json", "csv")


@dataclass(frozen=True)
class CommandResult:
    """Normalized command payload for every CLI feature."""

    plain_lines: list[str]
    columns: list[str]
    rows: list[dict[str, object]]


def add_output_format_option(parser: argparse.ArgumentParser) -> None:
    """Attach the shared output-format option to a command parser."""
    parser.add_argument(
        "--format",
        choices=OUTPUT_FORMATS,
        default="plain",
        help="Output format for the command result.",
    )


def render_result(result: CommandResult, *, output_format: str) -> str:
    """Render a command result in the requested output format."""
    if output_format == "plain":
        return formatting.join_lines(result.plain_lines)

    formatter = getattr(formatting, f"render_{output_format}", None)
    if formatter is None:
        raise NotImplementedError(f"output format '{output_format}' is not available yet")
    return formatter(result.rows, result.columns)
