# Using an LLM

Two commands can use an LLM, and both check what it returns before using it:

- `investigate` can have it write the summary at the top of its report. The LLM only writes
  prose: the hypotheses, their ranking, scores and confidence labels are the same with or
  without it.
- `graph draft` can have it propose the relations its rules could not infer, as inferred edges
  for the engineer to confirm ([Asking an LLM for the rest](../concepts/plant-graph.md#asking-an-llm-for-the-rest)).

Without `--llm`, or with `--llm none` (the default), penguineer makes no network call: the
summary comes from a template and the graph draft from the rules.

```bash
penguineer investigate --lot L0803-A --lims lims.csv --data historian.parquet \
  --llm local --llm-model <model>
```

## Choose a model

| `--llm` | Where it goes | Needs |
|---|---|---|
| `none` | Nowhere: the template summary | Nothing |
| `local` | Any OpenAI-compatible endpoint on the machine or the plant network: Ollama, llama.cpp, vLLM | `llm.base_url` (default `http://localhost:11434/v1`, Ollama's) |
| `api` | Anthropic's or OpenAI's API | `pip install "penguineer[llm-api]"` and `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` |

penguineer has no default model: which model may read a plant's results is the plant's decision.
Name it with `--llm-model`, `PENGUINEER_LLM_MODEL`, or in `penguineer.yaml`:

```yaml
llm:
  mode: local
  model: <model>                        # as the endpoint names it
  base_url: http://localhost:11434/v1   # local only
  provider: anthropic                   # api only: anthropic or openai
```

`--llm` and `--llm-model` override the environment (`PENGUINEER_LLM_MODEL`,
`PENGUINEER_LLM_BASE_URL`, `PENGUINEER_LLM_PROVIDER`), which overrides the file. A local
endpoint that needs a key reads it from `PENGUINEER_LLM_API_KEY`.

## What leaves the machine

For `graph draft`, the LLM receives the plant's equipment, streams and valves, the relations
already drafted, and the open tags' names, units and descriptions. For `investigate`, it receives
the results of `report.json`, never the historian or LIMS data: the quality
event, the reference lots' lab values, the hypotheses with their tests and evidence IDs, the
evidence titles, the ruled-out candidates and the notes. `--llm-dry-run` writes the exact request
to `llm-request.json` in the report folder and prints it, without sending anything, so it can be
shown to OT security first:

```bash
penguineer investigate --lot L0803-A --lims lims.csv --data historian.parquet \
  --llm api --llm-model <model> --llm-dry-run
```

A dry run needs neither the SDK nor a key.

## How the summary is checked

The LLM answers in a fixed structure: an overview, and up to three claims, each naming one
hypothesis's cause, a confidence and the evidence IDs behind it. penguineer keeps the answer only
if

- every claim names a hypothesis's cause, with a confidence no higher than its label;
- every claim cites at least one evidence ID, and every cited ID exists in the report;
- no sentence names a tag or node of the plant graph that the hypotheses, their paths and the
  ruled-out list do not mention;
- no sentence states a higher confidence than the label allows, or calls anything certain,
  proven or definite.

Otherwise the report shows the template summary and says why the LLM's was not used, as it does
when the endpoint cannot be reached. The report names the provider, the model and the prompt
version of a summary it kept.

## Reproducibility

Prompts are versioned files in the package (`penguineer/llm/prompts/summary-1.md`,
`relations-1.md`). Answers are
cached in `.penguineer/cache/llm/` by a hash of the prompt version, the provider, the model and
the request, so re-running a report on the same results gives the same text without a second
call. The run record stores the provider, the model and the prompts used (`llm_provider`,
`llm_model`, `llm_prompts`).
