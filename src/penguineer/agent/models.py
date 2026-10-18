"""Tool-calling models through Pydantic AI, so the provider is a setting.

Needs ``pip install "penguineer[agent]"``. A model is named ``provider:model-id`` in Pydantic
AI's notation (``anthropic:claude-opus-5-5``, ``bedrock:anthropic.claude-opus-5-5``,
``openai:gpt-5``), or passed as a Pydantic AI ``Model`` the caller built itself, for example
after checking it against the providers its data policy allows. Which providers are allowed is
the caller's decision; penguineer sends plant data only to the model it is given.

Only the single "send messages, get a response" step goes through Pydantic AI; the loop,
budgets and transcript stay in :mod:`penguineer.agent.loop`.
"""

from __future__ import annotations

import base64
import logging
import random
import time
from collections.abc import Callable
from typing import Any

from penguineer.agent.loop import Model

log = logging.getLogger("penguineer.agent")

RETRY_STATUS = {408, 429, 500, 502, 503, 504, 529}
RETRY_WORDS = (
    "throttl",
    "toomanyrequests",
    "too many requests",
    "rate limit",
    "serviceunavailable",
    "service unavailable",
    "overloaded",
    "modelnotready",
)


def is_retryable(e: Exception) -> bool:
    """Throttling and transient provider errors, normal when many runs share one model quota."""
    if getattr(e, "status_code", None) in RETRY_STATUS:
        return True
    resp = getattr(e, "response", None)  # botocore ClientError
    code = resp.get("Error", {}).get("Code", "") if isinstance(resp, dict) else ""
    text = f"{type(e).__name__} {code} {e}".lower()
    return any(w in text for w in RETRY_WORDS)


def with_retries(
    call: Callable[[], Any],
    *,
    budget_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
    first: float = 2.0,
    cap: float = 60.0,
) -> Any:
    """Call, retrying throttling with exponential backoff and jitter for ``budget_seconds``."""
    deadline = time.monotonic() + budget_seconds
    delay = first
    while True:
        try:
            return call()
        except Exception as e:
            if not is_retryable(e) or time.monotonic() + delay > deadline:
                raise
            wait = random.uniform(delay / 2, delay)
            log.warning("model call throttled (%s); retrying in %.0fs", type(e).__name__, wait)
            sleep(wait)
            delay = min(delay * 2, cap)


class PydanticAIModel:
    """Adapts a Pydantic AI model to the loop's :class:`~penguineer.agent.loop.Model` protocol.

    ``model`` is a Pydantic AI ``Model`` or a ``provider:model-id`` string. ``effort`` (low,
    medium, high, xhigh, max or off) sets the provider's thinking effort.
    """

    def __init__(
        self,
        model: Any,
        *,
        name: str | None = None,
        effort: str | None = None,
        max_tokens: int = 16000,
        retry_seconds: float = 300.0,
    ):
        if isinstance(model, str):
            from pydantic_ai.models import infer_model

            name = name or model
            model = infer_model(model)
        self.model = model
        self.name = name or getattr(model, "model_name", "model")
        self.thinking = {"max": "xhigh"}.get(effort, effort) if effort and effort != "off" else None
        self.max_tokens = max_tokens
        self.retry_seconds = retry_seconds

    def create(self, *, system: str, tools: list[dict], messages: list[dict]) -> dict:
        from pydantic_ai.direct import model_request_sync
        from pydantic_ai.models import ModelRequestParameters
        from pydantic_ai.tools import ToolDefinition

        defs = [
            ToolDefinition(
                name=t["name"],
                description=t.get("description"),
                parameters_json_schema=t["input_schema"],
            )
            for t in tools
        ]
        settings: dict[str, Any] = {"max_tokens": self.max_tokens}
        if self.thinking:
            settings["thinking"] = self.thinking
        params = ModelRequestParameters(function_tools=defs, allow_text_output=True)
        resp = with_retries(
            lambda: model_request_sync(
                self.model,
                to_pydantic_messages(system, messages),
                model_settings=settings,
                model_request_parameters=params,
            ),
            budget_seconds=self.retry_seconds,
        )
        return from_pydantic_response(resp)


def complete(model: Model, *, system: str, user: str) -> str:
    """One-shot text answer from any loop model."""
    resp = model.create(system=system, tools=[], messages=[{"role": "user", "content": user}])
    return "".join(b["text"] for b in resp["content"] if b["type"] == "text")


def _tool_result(b: dict, tool_names: dict[str, str]):
    from pydantic_ai.messages import BinaryContent, ToolReturnPart

    content: list = []
    for c in b["content"]:
        if c["type"] == "text":
            content.append(c["text"])
        elif c["type"] == "image":
            data = base64.b64decode(c["source"]["data"])
            content.append(BinaryContent(data, media_type=c["source"]["media_type"]))
    return ToolReturnPart(
        tool_name=tool_names.get(b["tool_use_id"], "tool"),
        content=content if len(content) > 1 else (content[0] if content else ""),
        tool_call_id=b["tool_use_id"],
        outcome="failed" if b.get("is_error") else "success",
    )


def to_pydantic_messages(system: str, messages: list[dict]) -> list:
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        SystemPromptPart,
        TextPart,
        ThinkingPart,
        ToolCallPart,
        UserPromptPart,
    )

    out: list = []
    tool_names: dict[str, str] = {}
    for i, m in enumerate(messages):
        head = [SystemPromptPart(system)] if i == 0 and system else []
        if m["role"] == "user":
            if isinstance(m["content"], str):
                out.append(ModelRequest(parts=[*head, UserPromptPart(m["content"])]))
                continue
            parts: list = list(head)
            for b in m["content"]:
                if b["type"] == "text":
                    parts.append(UserPromptPart(b["text"]))
                elif b["type"] == "tool_result":
                    parts.append(_tool_result(b, tool_names))
            out.append(ModelRequest(parts=parts))
        else:
            parts = []
            for b in m["content"]:
                if b["type"] == "text":
                    parts.append(TextPart(b["text"]))
                elif b["type"] == "thinking":
                    parts.append(
                        ThinkingPart(
                            b["content"],
                            id=b.get("id"),
                            signature=b.get("signature"),
                            provider_name=b.get("provider_name"),
                            provider_details=b.get("provider_details"),
                        )
                    )
                elif b["type"] == "tool_use":
                    tool_names[b["id"]] = b["name"]
                    parts.append(
                        ToolCallPart(b["name"], b.get("input") or {}, tool_call_id=b["id"])
                    )
            out.append(ModelResponse(parts=parts))
    return out


def from_pydantic_response(resp) -> dict:
    from pydantic_ai.messages import TextPart, ThinkingPart, ToolCallPart

    content: list[dict] = []
    for p in resp.parts:
        if isinstance(p, TextPart):
            content.append({"type": "text", "text": p.content})
        elif isinstance(p, ThinkingPart):
            block = {
                "type": "thinking",
                "content": p.content,
                "id": p.id,
                "signature": p.signature,
                "provider_name": p.provider_name,
                "provider_details": p.provider_details,
            }
            content.append({k: v for k, v in block.items() if v is not None})
        elif isinstance(p, ToolCallPart):
            content.append(
                {
                    "type": "tool_use",
                    "id": p.tool_call_id,
                    "name": p.tool_name,
                    "input": p.args_as_dict(),
                }
            )
    if any(b["type"] == "tool_use" for b in content):
        stop = "tool_use"
    else:
        stop = {"length": "max_tokens", "content_filter": "refusal"}.get(
            resp.finish_reason, "end_turn"
        )
    usage = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
    return {"content": content, "stop_reason": stop, "usage": usage}
