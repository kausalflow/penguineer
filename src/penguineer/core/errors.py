"""Errors with stable exit codes, shared by the CLI and the Python API."""

from __future__ import annotations


class PenguineerError(Exception):
    """Base error. ``exit_code`` is what the CLI exits with."""

    exit_code = 4


class ValidationFailed(PenguineerError):
    """The input is well-formed but fails a check, for example graph errors."""

    exit_code = 1


class UsageError(PenguineerError):
    """Bad parameters or missing files."""

    exit_code = 2


class NotEnoughData(PenguineerError):
    """The data cannot answer the question, for example no reference window."""

    exit_code = 3


class InternalError(PenguineerError):
    exit_code = 4


class NotImplementedYet(PenguineerError):
    """The command is registered and its parameters are validated, but its engine is not built."""

    exit_code = 4

    def __init__(self, command: str, milestone: str) -> None:
        super().__init__(
            f"'{command}' is not implemented yet; it arrives with the {milestone} milestone."
        )
        self.command = command
        self.milestone = milestone
