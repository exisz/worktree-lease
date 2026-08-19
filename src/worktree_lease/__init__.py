"""Git worktree lease manager — bind worktrees to threads with safe TTL recovery."""

from .cli import __version__, main

__all__ = ["__version__", "main"]
