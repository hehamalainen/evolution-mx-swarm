"""Shared output formatting surface for plain, table, JSON, and CSV renderers."""


def join_lines(lines: list[str]) -> str:
    """Render plain-text command output from preformatted lines."""
    return "\n".join(lines)
