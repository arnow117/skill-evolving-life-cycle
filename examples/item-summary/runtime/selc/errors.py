"""Stable public failures returned by the command-line interface."""

from __future__ import annotations


class SelcError(Exception):
    """An expected error with a stable code and a recovery action."""

    def __init__(
        self,
        code: str,
        message: str,
        recovery: str = "inspect the managed runtime state and retry after repair",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.recovery = recovery
