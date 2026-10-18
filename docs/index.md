# penguineer

penguineer finds the root cause of off-spec lots from historian and LIMS exports, on a laptop and offline. It is a KausalFlow open-source project under the Apache-2.0 license.

It reads files and writes files. The engines are deterministic Python, and an LLM is optional and never decides a ranking. Every command is defined once, as a Pydantic parameter model, a result model and a function. The CLI, the `--json` output and the Python API are all generated from that definition, so they cannot drift apart.

[Alchemist](https://github.com/kausalflow/alchemist) imports penguineer as a library and adds the UI, runners, project management, the data catalog, staging and sealing on top. penguineer has none of those itself.

!!! note "Status"
    This is an early release. `init`, `data health`, `graph check`, `graph draft`, `graph lags`, `lag`, `investigate`, `bench homeostat` and `mcp` work. `graph draft` reads tag names; its DEXPI import (`--pid`) is not built yet and exits with code 4 ("not implemented yet").

## Where to go next

- [Getting started](getting-started.md): install it and run `data health` on the demo plant.
- [Commands and run records](concepts/commands.md): how one definition becomes the CLI and the Python API.
- [Homeostat Live](guides/homeostat-live.md): run on a public simulated plant.
- [MCP server](guides/mcp.md): let an assistant such as Claude Code run the commands.
- [CLI reference](reference/cli.md): every command and option.
