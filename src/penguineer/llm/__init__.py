"""Optional LLM providers: report prose, and graph relations for an engineer to confirm.

The LLM never ranks, scores or finds anything. It writes a report's summary from typed results,
checked against them before it is kept (:mod:`penguineer.report.summary`), and proposes plant
graph relations the rules could not infer, checked before they are added as inferred edges
(:mod:`penguineer.graph.propose`). ``--llm none``, the default, makes no network call.
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
