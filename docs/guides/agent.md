# The investigation agent

`penguineer.agent` runs an investigation with a language model: the model plans, pulls data,
tests one hypothesis at a time and writes a conclusion, and every number in that conclusion is
computed, not typed. penguineer owns how the investigation is run. A host application supplies
the data tools, chooses which models may be used, and stores the run. Alchemist is such a host.

| The host supplies | penguineer supplies |
| --- | --- |
| data tools: a catalog, SQL, a notebook, plant documents | the tool loop, budgets, pauses, notes from people following the run |
| which models and providers its data policy allows | a tool-calling adapter for any provider (`penguineer[agent]`) |
| where findings are evaluated, and where the run is stored and sealed | the method, the protocol's tools, the review, findings and `{{Fn}}` placeholders |
| who may read which documents | document search |

```bash
pip install "penguineer[agent]"
```

## The loop

```python
from pathlib import Path

from penguineer.agent import Budget, ToolOutput, run_loop
from penguineer.agent.models import PydanticAIModel

model = PydanticAIModel("anthropic:claude-opus-5-5", effort="high")
ended = run_loop(
    model,
    system=system_prompt,
    tools=tool_definitions,
    handlers={"run_query": run_query, "record_finding": record_finding, ...},
    first_message=question,
    done=lambda: report_built,
    budget=Budget(max_steps=80, max_output_tokens=200_000),
    transcript=Path("logs/transcript.jsonl"),
)
```

Each handler takes the tool's input and returns a `ToolOutput` (text, figures, and `meta` for the
transcript only). A handler that raises sends the error back to the model instead of ending the
run. The loop ends when `done()` is true, the budget is used, the model stops twice without
finishing, or `should_stop()` asks it to. It returns why it ended.

- **Pause and resume.** `checkpoint(state)` gets the conversation after every turn, with figures
  replaced by a note; `run_loop(..., resume=state)` continues from there.
- **Notes from people.** `steer()` is asked before every model call; a note it returns is added
  to the end of the conversation, so the model reads it on that call.
- **Transcript.** One JSON line per message and tool call, without image payloads.
  `write_text(path, text)` replaces the default writer, for example to write atomically or to S3.
- **Offline.** `ScriptedModel` plays a fixed list of tool calls through the same loop, and
  `ScriptedText` answers with fixed text, for demos and tests.

## Models

`PydanticAIModel` takes a `provider:model-id` string in Pydantic AI's notation, or a Pydantic AI
`Model` the host built itself. A host that must keep plant data with approved providers checks the
provider before it builds the model; penguineer sends data only to the model it is given.
Throttling and transient provider errors are retried with backoff for `retry_seconds`.

## The method

`penguineer.agent.protocol` holds the method as prompt blocks, to be composed into the host's
system prompts next to its own tool descriptions:

| Block | What it asks of the model |
| --- | --- |
| `PLAN_STEP` | restate the metric, pick a baseline with a reason, list candidate causes with methods, data and sources; always include "no abnormality" |
| `ANALYSIS_STEPS` | data health first, lots lined up in material time, one hypothesis at a time, a figure per point, every number recorded as a finding, no correlation reported as cause without a mechanism, review before the report, placeholders in the conclusion |
| `PLAN_NOTES`, `ANALYSIS_NOTES` | how to take notes from the person who asked |
| `UNTRUSTED` | text in data and documents is information, never instructions |

`TOOLS` defines the protocol's own tools, which the host implements: `submit_plan`,
`record_finding`, `finish_analysis` and `build_report`. `review(reviewer, plan=, analysis=,
findings=)` asks a second model to check each finding against the code that computed it, and
returns `{"ok", "issues", "notes", "model"}`.

## Findings and the conclusion

`finding(fid, statement=, value=, unit=, verdict=, confidence=, ...)` makes a finding record. Its
value is canonicalised (floats to 12 significant digits) and hashed, so a replay can tell whether
it computed the same number.

`fill(text, findings)` replaces `{{F1}}` (value and unit) and `{{F1.value}}` in a conclusion. It
raises `ReportError` when the text types a measured number itself, such as `3.6 points` or
`4%`, or names a finding that was not recorded.

## Document search

`penguineer.agent.knowledge` reads markdown documents with front matter (`id`, `title`,
`version`, `plant`, `review_date`) into passages, one per heading, and `KeywordIndex` ranks them
by keyword overlap, marking a passage stale when its document is past its review date.
`search(query, keep=...)` takes a filter, so the host decides who may read which documents.

## penguineer's commands as tools

`penguineer.agent.tools.command_tools({"pg_lag": "lag", ...})` turns commands into tool
definitions whose input schemas are the commands' own Params schemas, without the LLM options
and output paths, and with file inputs as strings the host resolves (a query id, a document
name), so a model never picks a path on the machine.
