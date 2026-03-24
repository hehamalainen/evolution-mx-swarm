"""MX Swarm CLI package."""


def main() -> None:
    """Entrypoint wrapper to avoid importing cli.main at package import time."""
    from .main import main as _main

    _main()


__all__ = ["main"]
