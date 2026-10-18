"""The tool loop: a model calling the host's tools until the investigation is done.

The loop is a plain tool loop, so every model call and tool call passes through code the caller
can account for: budgets, a transcript, checkpoints, pauses and notes from people following the
run. :class:`ScriptedModel` plays a fixed list of tool calls through the same loop, for offline
demos and tests.

Messages use Anthropic-style content blocks (``text``, ``tool_use``, ``tool_result``, ``image``);
:mod:`penguineer.agent.models` adapts them to any provider.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from typing import Any, Protocol


class Model(Protocol):
    name: str

    def create(self, *, system: str, tools: list[dict], messages: list[dict]) -> dict: ...


def _usage() -> dict:
    return {"input_tokens": 0, "output_tokens": 0}


class ScriptedModel:
    """Plays back a fixed sequence of turns. Each turn is a list of ``(tool_name, input)`` calls."""

    def __init__(self, turns: list[list[tuple[str, dict]]], name: str = "scripted"):
        self.turns = list(turns)
        self.name = name
        self.i = 0

    def advance(self, model_calls: int) -> None:
        """Skip the turns already played before a pause, so a resumed run continues the script."""
        self.i = model_calls

    def create(self, *, system: str, tools: list[dict], messages: list[dict]) -> dict:
        if self.i >= len(self.turns):
            return {
                "content": [{"type": "text", "text": "Done."}],
                "stop_reason": "end_turn",
                "usage": _usage(),
            }
        calls = self.turns[self.i]
        self.i += 1
        content = [
            {"type": "tool_use", "id": f"toolu_s{self.i}_{k}", "name": name, "input": inp}
            for k, (name, inp) in enumerate(calls)
        ]
        return {"content": content, "stop_reason": "tool_use", "usage": _usage()}


class ScriptedText:
    """A model that always answers with the same text, such as a scripted review."""

    def __init__(self, text: str, name: str = "scripted"):
        self.text, self.name = text, name

    def create(self, *, system: str, tools: list[dict], messages: list[dict]) -> dict:
        return {
            "content": [{"type": "text", "text": self.text}],
            "stop_reason": "end_turn",
            "usage": _usage(),
        }


class ToolOutput:
    """What a tool returns to the model (``text``, ``images``), and ``meta`` for people following
    the run, such as figure paths. ``meta`` goes to the transcript only, never to the model."""

    def __init__(
        self,
        text: str,
        images: list[bytes] | None = None,
        is_error: bool = False,
        meta: dict | None = None,
    ):
        self.text, self.images, self.is_error, self.meta = text, images or [], is_error, meta or {}


def _result_block(tool_use_id: str, out: ToolOutput) -> dict:
    content: list[dict] = [{"type": "text", "text": out.text or "(no output)"}]
    for png in out.images[:2]:
        source = {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(png).decode(),
        }
        content.append({"type": "image", "source": source})
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if out.is_error:
        block["is_error"] = True
    return block


def _replace_images(x: Any, image: dict) -> Any:
    if isinstance(x, dict):
        if x.get("type") == "image":
            return image
        return {k: _replace_images(v, image) for k, v in x.items()}
    if isinstance(x, list):
        return [_replace_images(v, image) for v in x]
    return x


def _loggable(message: dict) -> dict:
    """Transcript copy without base64 image payloads."""
    return _replace_images(message, {"type": "image", "omitted": True})


def strip_images(messages: list[dict]) -> list[dict]:
    """The conversation without image payloads, for a checkpoint."""
    note = {"type": "text", "text": "[figure shown before the run was paused; see figures/]"}
    return _replace_images(messages, note)


class Budget:
    def __init__(self, max_steps: int, max_output_tokens: int):
        self.max_steps, self.max_output_tokens = max_steps, max_output_tokens
        self.steps = self.input_tokens = self.output_tokens = self.model_calls = 0

    def exceeded(self) -> str | None:
        if self.steps >= self.max_steps:
            return f"step budget of {self.max_steps} tool calls used"
        if self.output_tokens >= self.max_output_tokens:
            return f"output token budget of {self.max_output_tokens} used"
        return None

    def as_dict(self) -> dict:
        return {
            "steps": self.steps,
            "model_calls": self.model_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


NUDGE = (
    "Continue with the task using the tools. "
    "It is finished only when the required final tool has succeeded."
)


def run_loop(
    model: Model,
    *,
    system: str,
    tools: list[dict],
    handlers: dict[str, Callable[[dict], ToolOutput]],
    first_message: str,
    done: Callable[[], bool],
    budget: Budget,
    transcript: Any,
    write_text: Callable[[Any, str], None] | None = None,
    max_nudges: int = 2,
    should_stop: Callable[[], bool | str | None] | None = None,
    checkpoint: Callable[[dict], None] | None = None,
    resume: dict | None = None,
    steer: Callable[[], dict | None] | None = None,
) -> str:
    """Run until ``done()`` is true, the budget runs out, the model stops, or ``should_stop()``
    asks it to. Returns the reason it ended.

    ``transcript`` is a path-like (``pathlib.Path``, or a cloud path with ``read_text`` and
    ``exists``) for the JSON-lines transcript; ``write_text(path, text)`` writes it (default:
    ``path.write_text``).
    ``should_stop()`` returns ``"pause"`` to pause (the run can resume later) or anything else
    truthy to stop.
    ``checkpoint(state)`` is called after every completed turn with the conversation so far;
    passing that state back as ``resume`` continues the conversation where it left off.
    ``steer()`` is asked before every model call for notes from people following the run:
    ``{"text": ..., "notes": [...]}`` or None. The text is added to the end of the conversation,
    so the model reads it on that call.
    """
    write = write_text or (lambda path, text: path.write_text(text))
    if resume:
        messages: list[dict] = list(resume["messages"])
        nudges = resume.get("nudges", 0)
        lines = transcript.read_text().splitlines() if transcript.exists() else []
        lines.append(json.dumps({"at": time.time(), "resumed": True, "messages": len(messages)}))
    else:
        messages = [{"role": "user", "content": first_message}]
        lines = [json.dumps({"at": time.time(), "message": messages[0]})]
        nudges = 0

    def flush():
        write(transcript, "\n".join(lines) + "\n")

    while True:
        reason = budget.exceeded()
        if reason:
            flush()
            return f"budget: {reason}"
        request = should_stop() if should_stop else None
        if request:
            flush()
            if request == "pause":
                return "paused: pause requested by a user"
            return "stopped: stop requested by a user"
        note = steer() if steer else None
        if note:
            last = messages[-1]  # always the user's turn here: the question, a nudge, or results
            content = last["content"]
            blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
            messages[-1] = {**last, "content": [*blocks, {"type": "text", "text": note["text"]}]}
            lines.append(json.dumps({"at": time.time(), "steering": note["notes"]}))
        resp = model.create(system=system, tools=tools, messages=messages)
        budget.model_calls += 1
        budget.input_tokens += resp["usage"]["input_tokens"]
        budget.output_tokens += resp["usage"]["output_tokens"]
        assistant = {"role": "assistant", "content": resp["content"]}
        messages.append(assistant)
        lines.append(
            json.dumps(
                {
                    "at": time.time(),
                    "model": model.name,
                    "stop_reason": resp["stop_reason"],
                    "usage": resp["usage"],
                    "message": _loggable(assistant),
                }
            )
        )

        if resp["stop_reason"] == "refusal":
            flush()
            return "refusal: the model declined to continue"
        tool_uses = [b for b in resp["content"] if b.get("type") == "tool_use"]
        if not tool_uses:
            if done():
                flush()
                return "done"
            if nudges >= max_nudges or resp["stop_reason"] == "max_tokens":
                flush()
                return f"stopped: model ended its turn ({resp['stop_reason']}) before finishing"
            nudges += 1
            nudge = {"role": "user", "content": NUDGE}
            messages.append(nudge)
            lines.append(json.dumps({"at": time.time(), "message": nudge}))
            continue

        results = []
        for tu in tool_uses:
            budget.steps += 1
            handler = handlers.get(tu["name"])
            started = time.time()
            if handler is None:
                out = ToolOutput(f"unknown tool {tu['name']}", is_error=True)
            else:
                try:
                    out = handler(tu.get("input") or {})
                except Exception as e:  # a tool failure goes back to the model, not up the stack
                    out = ToolOutput(f"{type(e).__name__}: {e}", is_error=True)
            results.append(_result_block(tu["id"], out))
            entry = {
                "at": time.time(),
                "tool": tu["name"],
                "input": tu.get("input"),
                "is_error": out.is_error,
                "seconds": round(time.time() - started, 3),
                "output": out.text[:4000],
                "images": len(out.images),
            }
            if out.meta:
                entry["meta"] = out.meta
            lines.append(json.dumps(entry))
        messages.append({"role": "user", "content": results})
        flush()
        if checkpoint:
            checkpoint({"messages": strip_images(messages), "nudges": nudges})
        if done():
            return "done"
