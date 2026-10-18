"""Optional LLM providers, for prose only.

The LLM never ranks, scores or finds anything: it writes text from typed results, and that text
is checked against those results before it is kept (see :mod:`penguineer.report.summary`).
``--llm none``, the default, makes no network call.
"""

from penguineer.llm.config import LLMConfig, load_config
from penguineer.llm.provider import (
    CachedProvider,
    DryRun,
    LLMError,
    LLMProvider,
    Prompt,
    load_prompt,
    make_provider,
)

__all__ = [
    "CachedProvider",
    "DryRun",
    "LLMConfig",
    "LLMError",
    "LLMProvider",
    "Prompt",
    "load_config",
    "load_prompt",
    "make_provider",
]
