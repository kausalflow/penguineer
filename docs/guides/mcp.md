# MCP server

`penguineer mcp` serves every command as a tool to an [MCP](https://modelcontextprotocol.io)
client, such as Claude Code, Claude Desktop or an IDE assistant. The assistant can then check a
graph, look at data health, fit a lag or investigate a lot by itself, and read the same results
you get from `--json`.

```bash
pip install "penguineer[mcp]"
```

The server talks over stdin and stdout to the client that starts it. It opens no port and has
no web UI.

## Add it to a client

Claude Code:

```bash
claude mcp add penguineer -- penguineer mcp --project /path/to/my-unit
```

A client configured with a JSON file (Claude Desktop, and most others):

```json
{
  "mcpServers": {
    "penguineer": {
      "command": "penguineer",
      "args": ["mcp", "--project", "/path/to/my-unit"]
    }
  }
}
```

`--project` is the default project folder. Each call writes its outputs and its run record under
it, as the CLI does, and relative paths in a call are read against it. A call can name another
folder with its own `project` argument.

## The tools

One tool per command, named after it with underscores:

| Tool | Command |
|---|---|
| `init` | `penguineer init` |
| `data_health` | `penguineer data health` |
| `graph_check` | `penguineer graph check` |
| `graph_draft` | `penguineer graph draft` (not built yet) |
| `graph_lags` | `penguineer graph lags` |
| `lag` | `penguineer lag` |
| `investigate` | `penguineer investigate` |
| `bench_homeostat` | `penguineer bench homeostat` (needs `penguineer[bench]` too) |

Each tool's input schema is the command's parameters, the same as in the
[CLI reference](../reference/cli.md), plus `project` and `seed`. Its output schema is the
command's result. A call returns the result as structured content, and the CLI summary with the
path of the run record as text:

```text
graph check: 13 nodes, 11 edges, 0 errors, 0 warnings
confirm queue (1 inferred edges):
  f3: R-101 -flows_to-> S-product (confidence 0.60, 1 qualities depend on it)
run record: /path/to/my-unit/runs/20261018T001610Z-b414d1/run.json
```

```json
{
  "graph": "/path/to/my-unit/graph.yaml",
  "n_nodes": 13,
  "n_edges": 11,
  "errors": 0,
  "warnings": 0,
  "issues": [],
  "confirm_queue": [
    {"edge_id": "f3", "type": "flows_to", "from_id": "R-101", "to_id": "S-product",
     "confidence": 0.6, "dependent_qualities": 1}
  ]
}
```

A failed call returns an error with the CLI's exit code and message, for example
`graph check failed (exit code 2): ...` for a missing file. Commands that report progress
(`graph lags`, `investigate`, `bench homeostat`) send it as MCP progress notifications when the
client asks for them.

## How it runs

- One call at a time, like the CLI: a second call waits for the first to finish.
- The engines are the same deterministic code as the CLI. The server adds no LLM, and the
  assistant only sees what the tools return.
- Tools write only under the project folder, except `graph lags --in-place`, which writes back
  to the graph you name, and an `--out` folder you name.
