"""LLM providers behind one interface: ``structured(prompt, schema) -> schema instance``.

- ``local``: any OpenAI-compatible endpoint (Ollama, llama.cpp, vLLM), over the standard library.
- ``api``: Anthropic or OpenAI through their SDKs (``pip install "penguineer[llm-api]"``).

Every provider builds its request body with :meth:`payload` first, so ``--llm-dry-run`` can show
exactly what would be sent (:class:`DryRun`) and the cache can key on it
(:class:`CachedProvider`).
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from penguineer.core.context import LLMMode
from penguineer.core.errors import PenguineerError, UsageError
from penguineer.llm.config import LLMConfig

T = TypeVar("T", bound=BaseModel)


class LLMError(PenguineerError):
    """The LLM could not be reached or answered with something unusable.

    Callers fall back to their deterministic text and say so; it never stops a run.
    """


@dataclass(frozen=True)
class Prompt:
    """A versioned prompt from ``penguineer/llm/prompts/<name>-<version>.md``."""

    name: str
    version: int
    system: str
    user: str = ""

    @property
    def id(self) -> str:
        return f"{self.name}@{self.version}"

    def with_input(self, data: Any) -> Prompt:
        """The same prompt with ``data`` as the user message, as indented JSON."""
        text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
        return Prompt(self.name, self.version, self.system, text)


def load_prompt(name: str, version: int) -> Prompt:
    text = resources.files("penguineer.llm").joinpath("prompts", f"{name}-{version}.md").read_text()
    return Prompt(name, version, text.strip())


class LLMProvider(Protocol):
    name: str  # local, anthropic or openai
    model: str

    def payload(self, prompt: Prompt, schema: type[BaseModel]) -> dict[str, Any]: ...

    def structured(self, prompt: Prompt, schema: type[T]) -> T: ...


def _parse(schema: type[T], data: Any) -> T:
    try:
        return (
            schema.model_validate_json(data)
            if isinstance(data, str)
            else schema.model_validate(data)
        )
    except (ValidationError, ValueError) as e:
        raise LLMError(f"the answer does not fit {schema.__name__}: {e}") from e


def _openai_payload(model: str, prompt: Prompt, schema: type[BaseModel]) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt.system},
            {"role": "user", "content": prompt.user},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema()},
        },
    }


@dataclass
class OpenAICompatible:
    """A local OpenAI-compatible endpoint: POST {base_url}/chat/completions."""

    model: str
    base_url: str
    timeout_s: float = 120.0
    api_key: str | None = None
    name: str = "local"

    def payload(self, prompt: Prompt, schema: type[BaseModel]) -> dict[str, Any]:
        # Local servers take a temperature; the hosted APIs' newer models may refuse one.
        return {**_openai_payload(self.model, prompt, schema), "temperature": 0}

    def structured(self, prompt: Prompt, schema: type[T]) -> T:
        url = self.base_url.rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = json.dumps(self.payload(prompt, schema)).encode()
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                answer = json.loads(response.read())
        except urllib.error.HTTPError as e:
            raise LLMError(f"{url}: HTTP {e.code} {e.reason}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise LLMError(f"{url}: {getattr(e, 'reason', e)}") from e
        except json.JSONDecodeError as e:
            raise LLMError(f"{url}: the answer is not JSON") from e
        try:
            content = answer["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"{url}: no message in the answer") from e
        return _parse(schema, content)


@dataclass
class OpenAIAPI:
    """OpenAI's API through its SDK; the key comes from OPENAI_API_KEY."""

    model: str
    timeout_s: float = 120.0
    client: Any = None
    name: str = "openai"

    def __post_init__(self) -> None:
        if self.client is None:
            try:
                import openai
            except ImportError as e:
                raise UsageError(
                    '--llm api with provider openai needs: pip install "penguineer[llm-api]"'
                ) from e
            if not os.environ.get("OPENAI_API_KEY"):
                raise UsageError("--llm api with provider openai needs OPENAI_API_KEY")
            self.client = openai.OpenAI(timeout=self.timeout_s)

    def payload(self, prompt: Prompt, schema: type[BaseModel]) -> dict[str, Any]:
        return _openai_payload(self.model, prompt, schema)

    def structured(self, prompt: Prompt, schema: type[T]) -> T:
        try:
            answer = self.client.chat.completions.create(**self.payload(prompt, schema))
            content = answer.choices[0].message.content
        except Exception as e:  # the SDK's errors share no base class we can name here
            raise LLMError(f"openai: {e}") from e
        return _parse(schema, content)


@dataclass
class AnthropicAPI:
    """Anthropic's API through its SDK, with the schema as a forced tool; ANTHROPIC_API_KEY."""

    model: str
    timeout_s: float = 120.0
    max_tokens: int = 2048
    client: Any = None
    name: str = "anthropic"

    def __post_init__(self) -> None:
        if self.client is None:
            try:
                import anthropic
            except ImportError as e:
                raise UsageError(
                    '--llm api with provider anthropic needs: pip install "penguineer[llm-api]"'
                ) from e
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise UsageError("--llm api with provider anthropic needs ANTHROPIC_API_KEY")
            self.client = anthropic.Anthropic(timeout=self.timeout_s)

    def payload(self, prompt: Prompt, schema: type[BaseModel]) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": prompt.system,
            "messages": [{"role": "user", "content": prompt.user}],
            "tools": [
                {
                    "name": "answer",
                    "description": f"Give the answer as {schema.__name__}.",
                    "input_schema": schema.model_json_schema(),
                }
            ],
            "tool_choice": {"type": "tool", "name": "answer"},
        }

    def structured(self, prompt: Prompt, schema: type[T]) -> T:
        try:
            answer = self.client.messages.create(**self.payload(prompt, schema))
        except Exception as e:  # the SDK's errors share no base class we can name here
            raise LLMError(f"anthropic: {e}") from e
        for block in getattr(answer, "content", []):
            if getattr(block, "type", None) == "tool_use":
                return _parse(schema, block.input)
        raise LLMError("anthropic: the answer has no tool call")


@dataclass
class CachedProvider:
    """Answers cached under ``cache_dir`` by a hash of the prompt version, model and request.

    Re-running a report with the same results gives the same text, without a second call.
    """

    inner: LLMProvider
    cache_dir: Path

    @property
    def name(self) -> str:
        return self.inner.name

    @property
    def model(self) -> str:
        return self.inner.model

    def payload(self, prompt: Prompt, schema: type[BaseModel]) -> dict[str, Any]:
        return self.inner.payload(prompt, schema)

    def key(self, prompt: Prompt, schema: type[BaseModel]) -> str:
        blob = json.dumps(
            {
                "prompt": prompt.id,
                "provider": self.inner.name,
                "model": self.inner.model,
                "payload": self.payload(prompt, schema),
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(blob.encode()).hexdigest()

    def structured(self, prompt: Prompt, schema: type[T]) -> T:
        path = self.cache_dir / f"{prompt.name}-{self.key(prompt, schema)[:32]}.json"
        if path.exists():
            try:
                return schema.model_validate_json(path.read_text())
            except ValidationError:
                pass  # a cache from an older schema: ask again
        answer = self.inner.structured(prompt, schema)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(answer.model_dump_json(indent=2))
        return answer


@dataclass
class DryRun:
    """Records what would be sent and sends nothing: every call fails with :class:`LLMError`."""

    inner: LLMProvider
    sent: list[dict[str, Any]] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.inner.name

    @property
    def model(self) -> str:
        return self.inner.model

    def payload(self, prompt: Prompt, schema: type[BaseModel]) -> dict[str, Any]:
        return self.inner.payload(prompt, schema)

    def structured(self, prompt: Prompt, schema: type[T]) -> T:
        self.sent.append(
            {
                "prompt": prompt.id,
                "provider": self.inner.name,
                "endpoint": endpoint(self.inner),
                "body": self.payload(prompt, schema),
            }
        )
        raise LLMError("dry run: nothing was sent")


def endpoint(provider: LLMProvider) -> str:
    """Where a provider's requests go, for the dry run."""
    while isinstance(provider, CachedProvider | DryRun):
        provider = provider.inner
    if isinstance(provider, OpenAICompatible):
        return provider.base_url.rstrip("/") + "/chat/completions"
    if isinstance(provider, AnthropicAPI):
        return "https://api.anthropic.com/v1/messages"
    if isinstance(provider, OpenAIAPI):
        return "https://api.openai.com/v1/chat/completions"
    return provider.name


def make_provider(
    config: LLMConfig, cache_dir: Path | None = None, *, dry_run: bool = False
) -> LLMProvider | None:
    """The provider ``config`` names, cached under ``cache_dir``; None for ``--llm none``.

    With ``dry_run`` the provider only records its requests: no SDK or key is needed.
    """
    if config.mode == LLMMode.none:
        return None
    if not config.model:
        raise UsageError(
            f"--llm {config.mode} needs a model: pass --llm-model, set PENGUINEER_LLM_MODEL, or "
            "set llm.model in penguineer.yaml"
        )
    provider: LLMProvider
    client = "dry run" if dry_run else None  # any non-None client skips the SDK and the key
    if config.mode == LLMMode.local:
        provider = OpenAICompatible(
            model=config.model,
            base_url=config.base_url,
            timeout_s=config.timeout_s,
            api_key=os.environ.get("PENGUINEER_LLM_API_KEY"),
        )
    elif config.provider == "openai":
        provider = OpenAIAPI(model=config.model, timeout_s=config.timeout_s, client=client)
    else:
        provider = AnthropicAPI(model=config.model, timeout_s=config.timeout_s, client=client)
    if dry_run:
        return DryRun(provider)
    return CachedProvider(provider, cache_dir) if cache_dir is not None else provider
