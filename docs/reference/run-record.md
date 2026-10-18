# Run record

Every call writes `runs/<run_id>/run.json` under its project folder, from the CLI or Python and also when it fails. The run ID is a UTC timestamp plus six random hex characters, for example `20261017T195308Z-510ada`.

| Field | What |
|---|---|
| `run_id` | The run ID |
| `command` | The command name, for example `data health` |
| `params` | The validated params as JSON |
| `argv` | The equivalent command line, from `to_argv` |
| `penguineer_version`, `plantgraph_version`, `python_version` | Versions |
| `seed` | The random seed |
| `llm` | The LLM mode: `none`, `local` or `api` |
| `llm_provider`, `llm_model`, `llm_prompts` | The provider (`local`, `anthropic` or `openai`), the model and the prompt versions a run used, when it used an LLM |
| `inputs` | Every input file: `path`, `sha256` and `bytes` |
| `outputs` | Paths of the files the command wrote |
| `started_at`, `duration_s` | When it started and how long it took |
| `status`, `exit_code`, `error` | `ok` or `error`, the exit code and the error message |

Alchemist seals the run record into its own run manifest.
