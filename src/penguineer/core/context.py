"""Run context: everything an engine may read besides its typed params."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import numpy as np


class LLMMode(StrEnum):
    none = "none"
    local = "local"
    api = "api"


ProgressCallback = Callable[[int, int, str], None]


def _no_progress(done: int, total: int, message: str) -> None:
    return None


def _never_cancelled() -> bool:
    return False


@dataclass
class RunContext:
    """Per-call context. Engines read only this, never global state.

    ``project_root`` is the folder the caller passed in; all outputs of the call go under it,
    so several penguineer processes can run side by side on different folders.
    """

    project_root: Path
    run_id: str
    seed: int = 0
    llm: LLMMode = LLMMode.none
    cache_dir: Path | None = None
    progress: ProgressCallback = _no_progress
    cancelled: Callable[[], bool] = _never_cancelled
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("penguineer"))
    rng: np.random.Generator = field(init=False)

    def __post_init__(self) -> None:
        self.rng = np.random.default_rng(self.seed)
        if self.cache_dir is None:
            self.cache_dir = self.project_root / ".penguineer" / "cache"

    @property
    def run_dir(self) -> Path:
        return self.project_root / "runs" / self.run_id
