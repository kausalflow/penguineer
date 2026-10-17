"""penguineer: find the root cause of off-spec lots from historian and LIMS exports.

The Python API mirrors the CLI one to one::

    import penguineer
    result = penguineer.run("data health", data="historian.csv")
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

try:
    __version__ = version("penguineer")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0"

import penguineer.commands  # noqa: E402,F401  registers the built-in commands
from penguineer.core import (  # noqa: E402
    CommandResult,
    LLMMode,
    PenguineerError,
    RunContext,
    RunOutcome,
    default_registry,
)


def run(
    command: str, /, params: Any = None, *, project: Path | str = ".", seed: int = 0, **kwargs: Any
) -> CommandResult:
    """Run a command by name, e.g. ``run("data health", data="historian.csv")``."""
    return execute(command, params, project=project, seed=seed, **kwargs).result


def execute(
    command: str,
    /,
    params: Any = None,
    *,
    project: Path | str = ".",
    seed: int = 0,
    cache_dir: Path | None = None,
    progress=None,
    cancelled=None,
    **kwargs: Any,
) -> RunOutcome:
    """Like :func:`run`, but also return the run record and run folder."""
    if params is None:
        params = kwargs
    elif kwargs:
        raise TypeError("pass either a params object or keyword arguments, not both")
    return default_registry.execute(
        command,
        params,
        project=project,
        seed=seed,
        cache_dir=cache_dir,
        progress=progress,
        cancelled=cancelled,
    )


def schemas() -> dict[str, dict[str, Any]]:
    """JSON Schemas of every command's Params and Result."""
    return default_registry.schemas()


__all__ = [
    "CommandResult",
    "LLMMode",
    "PenguineerError",
    "RunContext",
    "RunOutcome",
    "__version__",
    "execute",
    "run",
    "schemas",
]
