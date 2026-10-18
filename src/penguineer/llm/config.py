"""Which LLM to use: ``--llm`` and ``--llm-model``, then the environment, then penguineer.yaml.

    llm:
      mode: local            # none, local or api
      model: <model name>    # as the endpoint or provider names it; there is no default
      base_url: http://localhost:11434/v1   # local: any OpenAI-compatible endpoint
      provider: anthropic    # api: anthropic or openai, key from ANTHROPIC_API_KEY / OPENAI_API_KEY

``PENGUINEER_LLM_MODEL``, ``PENGUINEER_LLM_BASE_URL`` and ``PENGUINEER_LLM_PROVIDER`` override the
file. No model is chosen for you: which model may see plant data is the plant's decision.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from penguineer.core.context import LLMMode
from penguineer.core.errors import UsageError

DEFAULT_BASE_URL = "http://localhost:11434/v1"  # Ollama's OpenAI-compatible endpoint


class LLMConfig(BaseModel):
    mode: LLMMode = LLMMode.none
    model: str | None = None
    base_url: str = Field(DEFAULT_BASE_URL, description="local: the OpenAI-compatible endpoint")
    provider: Literal["anthropic", "openai"] = Field("anthropic", description="api: whose API")
    timeout_s: float = 120.0


def load_config(project_root: Path, mode: LLMMode, model: str | None = None) -> LLMConfig:
    """The LLM settings for one call; ``mode`` and ``model`` come from the command's flags."""
    raw: dict = {}
    path = project_root / "penguineer.yaml"
    if path.exists():
        try:
            doc = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as e:
            raise UsageError(f"{path}: not valid YAML: {e}") from e
        raw = dict(doc.get("llm") or {}) if isinstance(doc, dict) else {}
    env = {
        "model": os.environ.get("PENGUINEER_LLM_MODEL"),
        "base_url": os.environ.get("PENGUINEER_LLM_BASE_URL"),
        "provider": os.environ.get("PENGUINEER_LLM_PROVIDER"),
    }
    raw.update({k: v for k, v in env.items() if v})
    raw["mode"] = mode
    if model:
        raw["model"] = model
    try:
        return LLMConfig.model_validate(raw)
    except ValueError as e:
        raise UsageError(f"llm settings in {path}: {e}") from e
