import base64
import datetime as dt
import json

import numpy as np
import pytest

from penguineer.agent import (
    TOOLS,
    Budget,
    ReportError,
    ScriptedModel,
    ScriptedText,
    ToolOutput,
    fill,
    finding,
    review,
    run_loop,
)
from penguineer.agent.knowledge import KeywordIndex, document
from penguineer.agent.loop import _result_block
from penguineer.agent.tools import command_tools

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def loop(tmp_path, model, handlers, **kw):
    kw.setdefault("done", lambda: False)
    kw.setdefault("budget", Budget(10, 10**6))
    return run_loop(
        model,
        system="",
        tools=[],
        handlers=handlers,
        first_message="go",
        transcript=tmp_path / "t.jsonl",
        **kw,
    )


# ------------------------------------------------------------ loop


def test_budget_stops_the_loop(tmp_path):
    calls = []
    model = ScriptedModel([[("ping", {})]] * 10)
    ping = {"ping": lambda i: calls.append(1) or ToolOutput("pong")}
    ended = loop(tmp_path, model, ping, budget=Budget(3, 10**6))
    assert ended.startswith("budget") and len(calls) == 3


def test_tool_errors_go_back_to_the_model(tmp_path):
    def boom(_):
        raise RuntimeError("bad input")

    ended = loop(tmp_path, ScriptedModel([[("boom", {})]]), {"boom": boom}, max_nudges=0)
    assert "bad input" in (tmp_path / "t.jsonl").read_text()
    assert ended.startswith("stopped")


def test_pause_checkpoint_and_resume(tmp_path):
    states, hits = [], []
    model = ScriptedModel([[("ping", {})], [("ping", {})], [("ping", {})]])
    handlers = {"ping": lambda i: hits.append(1) or ToolOutput("pong", images=[PNG])}
    stop = iter([None, "pause"])
    ended = loop(
        tmp_path,
        model,
        handlers,
        should_stop=lambda: next(stop),
        checkpoint=states.append,
    )
    assert ended.startswith("paused") and len(hits) == 1
    # the checkpoint holds no image payloads
    assert "iVBOR" not in json.dumps(states[-1]) and "figure shown" in json.dumps(states[-1])
    ended = loop(tmp_path, model, handlers, resume=states[-1], done=lambda: len(hits) == 3)
    assert ended == "done" and len(hits) == 3
    lines = (tmp_path / "t.jsonl").read_text().splitlines()
    assert any(json.loads(line).get("resumed") for line in lines)


def test_steering_note_reaches_the_model(tmp_path):
    seen = []

    class Echo(ScriptedModel):
        def create(self, *, system, tools, messages):
            seen.append(json.dumps(messages[-1]))
            return super().create(system=system, tools=tools, messages=messages)

    notes = iter([{"text": "Note from L: skip March", "notes": ["skip March"]}, None])
    loop(
        tmp_path,
        Echo([[("ping", {})]]),
        {"ping": lambda i: ToolOutput("ok")},
        steer=lambda: next(notes, None),
        max_nudges=0,
    )
    assert "skip March" in seen[0]


# ------------------------------------------------------------ protocol


def test_review_parses_the_reviewer_answer():
    ok = review(
        ScriptedText('Here: {"ok": true, "issues": [], "notes": "fine"}'),
        plan={},
        analysis="",
        findings=[],
    )
    assert ok == {"ok": True, "issues": [], "notes": "fine", "model": "scripted"}
    bad = review(ScriptedText("not json"), plan={}, analysis="", findings=[])
    assert bad["ok"] is False and bad["notes"] == "not json"


def test_protocol_tools():
    assert set(TOOLS) == {"submit_plan", "record_finding", "finish_analysis", "build_report"}
    assert TOOLS["record_finding"]["input_schema"]["properties"]["verdict"]["enum"] == [
        "supported",
        "refuted",
        "inconclusive",
    ]


# ------------------------------------------------------------ findings

F = {"F1": {"value": 3.63, "unit": "percentage points"}, "F2": {"value": 0, "unit": "new recipes"}}


def test_placeholders_are_filled():
    assert (
        fill("Yield fell by {{F1}} on Line 2 from 3 to 9 August 2026.", F)
        == "Yield fell by 3.63 percentage points on Line 2 from 3 to 9 August 2026."
    )
    assert fill("{{F1.value}}", F) == "3.63"


@pytest.mark.parametrize(
    "text", ["Yield fell by 3.6 points.", "Yield fell 4%.", "Temp was 2 °C lower.", "{{F9}}"]
)
def test_typed_numbers_and_unknown_findings_rejected(text):
    with pytest.raises(ReportError):
        fill(text, F)


def test_finding_values_hash_the_same_way():
    a = finding("F1", statement="s", value=np.float64(-0.0))
    b = finding("F1", statement="s", value=0.0)
    assert a["value"] == 0.0 and a["value_sha256"] == b["value_sha256"]
    c = finding("F1", statement="s", value=1.0000000000001)
    assert c["value_sha256"] == finding("F1", statement="s", value=1.0)["value_sha256"]
    with pytest.raises(ValueError, match="verdict"):
        finding("F1", statement="s", verdict="maybe")


# ------------------------------------------------------------ knowledge

SOP = """---
id: SOP-114
title: Melt temperature window
version: 3
plant: P01
review_date: 2027-02-01
---
Intro text.
## Window
Below 229 degC the melt is not fully homogenised, which causes voids.
## Other
Line speed limits.
"""


def test_knowledge_search():
    meta, secs = document(SOP, "sop")
    assert meta["doc"] == "SOP-114" and [h for h, _ in secs] == [
        "Melt temperature window",
        "Window",
        "Other",
    ]
    passages = [{**meta, "heading": h, "text": t} for h, t in secs]
    index = KeywordIndex(passages)
    hits = index.search("homogenised voids", today=dt.date(2026, 10, 1))
    assert hits[0]["section"] == "Window" and hits[0]["stale"] is False
    assert index.search("voids", today=dt.date(2027, 3, 1))[0]["stale"] is True
    assert index.search("voids", keep=lambda p: p["plant"] == "P02") == []


# ------------------------------------------------------------ tools


def test_command_tools():
    tools = command_tools(
        {"pg_investigate": "investigate"}, file_input="A query id.", note="Runs in a kernel."
    )
    inv = tools["pg_investigate"]
    props = inv["input_schema"]["properties"]
    assert {"llm", "llm_model", "llm_dry_run", "out"}.isdisjoint(props)
    assert props["data"]["type"] == "string" and props["data"]["description"].endswith(
        "A query id."
    )
    assert inv["description"].endswith("Runs in a kernel.")


# ------------------------------------------------------------ models


def test_loop_through_pydantic_ai(tmp_path):
    """The tool loop works against a Pydantic AI model (FunctionModel stands in for a provider)."""
    pytest.importorskip("pydantic_ai")
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel

    from penguineer.agent.models import PydanticAIModel

    seen = {}

    def fn(messages, info):
        parts = [p for m in messages for p in getattr(m, "parts", [])]
        returns = [p for p in parts if isinstance(p, ToolReturnPart)]
        seen["tools"] = [t.name for t in info.function_tools]
        if not returns:
            return ModelResponse(parts=[ToolCallPart("ping", {"n": 1}, tool_call_id="c1")])
        return ModelResponse(parts=[TextPart("done")])

    hit = []
    tools = [
        {
            "name": "ping",
            "description": "p",
            "input_schema": {"type": "object", "properties": {"n": {"type": "integer"}}},
        }
    ]
    model = PydanticAIModel(FunctionModel(fn), name="test:fn", max_tokens=1000)
    end = run_loop(
        model,
        system="s",
        tools=tools,
        handlers={"ping": lambda a: hit.append(a) or ToolOutput("pong")},
        first_message="go",
        done=lambda: bool(hit),
        budget=Budget(5, 1000),
        transcript=tmp_path / "t.jsonl",
    )
    assert end == "done" and hit == [{"n": 1}] and seen["tools"] == ["ping"]


def test_message_conversion_keeps_images_and_thinking():
    pytest.importorskip("pydantic_ai")
    from penguineer.agent.models import from_pydantic_response, to_pydantic_messages

    msgs = [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "content": "hm", "signature": "sig"},
                {"type": "tool_use", "id": "t1", "name": "run_python", "input": {"code": "1"}},
            ],
        },
        {"role": "user", "content": [_result_block("t1", ToolOutput("ok", images=[PNG]))]},
    ]
    out = to_pydantic_messages("sys", msgs)
    assert [type(m).__name__ for m in out] == ["ModelRequest", "ModelResponse", "ModelRequest"]
    assert out[1].parts[0].signature == "sig"
    ret = out[2].parts[0]
    assert ret.tool_name == "run_python" and len(ret.content) == 2
    back = from_pydantic_response(out[1])
    assert back["stop_reason"] == "tool_use" and back["content"][1]["input"] == {"code": "1"}


def test_retries_only_transient_errors():
    from penguineer.agent.models import with_retries

    class Throttled(Exception):
        status_code = 429

    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise Throttled()
        return "ok"

    assert with_retries(flaky, budget_seconds=600, sleep=lambda s: None) == "ok"
    with pytest.raises(KeyError):
        with_retries(lambda: {}["x"], budget_seconds=600, sleep=lambda s: None)
