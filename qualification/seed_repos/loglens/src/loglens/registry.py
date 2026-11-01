"""Command registry for self-registering loglens features."""

from __future__ import annotations

import argparse
import importlib
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass

from loglens import commands
from loglens.parsing import LogEntry
from loglens.runtime import CommandResult

CommandHandler = Callable[[list[LogEntry], argparse.Namespace], CommandResult]
ParserConfigurer = Callable[[argparse.ArgumentParser], None]


@dataclass(frozen=True)
class CommandSpec:
    """Registered command metadata."""

    help_text: str
    configure: ParserConfigurer
    handler: CommandHandler


COMMANDS: dict[str, CommandSpec] = {}
_DISCOVERED = False


def register_command(
    *,
    name: str,
    help_text: str,
    configure: ParserConfigurer,
) -> Callable[[CommandHandler], CommandHandler]:
    """Register a command handler under the shared dispatch table."""

    def decorator(handler: CommandHandler) -> CommandHandler:
        COMMANDS[name] = CommandSpec(
            help_text=help_text,
            configure=configure,
            handler=handler,
        )
        return handler

    return decorator


def discover_commands() -> dict[str, CommandSpec]:
    """Import every module under loglens.commands once."""
    global _DISCOVERED

    if _DISCOVERED:
        return COMMANDS

    for module in pkgutil.iter_modules(commands.__path__):
        importlib.import_module(f"{commands.__name__}.{module.name}")
    _DISCOVERED = True
    return COMMANDS
