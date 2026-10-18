"""``penguineer mcp``: every registered command as a tool for MCP clients, over stdio.

Each command becomes one tool, named after it with underscores (``data health`` ->
``data_health``). Its input schema is the command's Params schema plus ``project`` and ``seed``;
its output schema is the Result schema, so a client gets the same JSON as ``--json``, with the
CLI summary as the text. Calls run one at a time, in a worker thread, exactly as
:func:`penguineer.execute` runs them: each writes its run record under the project.

Relative paths in the arguments are read against the project folder, not the folder the client
started the server in. Nothing listens on a port: the server talks only to the client that
started it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import anyio
    import anyio.from_thread
    import anyio.to_thread
    import mcp_types as mt
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server
except ImportError as e:  # pragma: no cover - exercised without the extra installed
    from penguineer.core import UsageError

    raise UsageError('penguineer mcp needs the MCP SDK: pip install "penguineer[mcp]"') from e

from penguineer.core import Command, PenguineerError, Registry, default_registry
from penguineer.core.fields import field_shape, is_path_type

INSTRUCTIONS = """\
penguineer finds the root cause of off-spec lots from a plant's historian and LIMS exports, \
offline and deterministically. Typical order: `init` a project folder, `data_health` on the \
historian export, `graph_check` the plant graph, `graph_lags` or `lag` for dead times, then \
`investigate` a lot. Every tool writes its outputs and a run record under `project`; relative \
paths are read against it. Results are the same JSON as the CLI's --json."""


def tool_name(cmd: Command) -> str:
    return cmd.name.replace(" ", "_")


def path_fields(cmd: Command) -> set[str]:
    """Params fields that hold a path or a list of paths."""
    return {
        name for name, f in cmd.params.model_fields.items() if is_path_type(field_shape(f).base)
    }


def input_schema(cmd: Command) -> dict[str, Any]:
    schema = cmd.params.model_json_schema()
    props = schema.setdefault("properties", {})
    props["project"] = {
        "type": "string",
        "description": "Project folder: outputs and the run record go under it, and relative "
        "paths are read against it (default: the server's --project)",
    }
    props["seed"] = {"type": "integer", "default": 0, "description": "Random seed"}
    return schema


def tool(cmd: Command) -> mt.Tool:
    first, _, _ = cmd.help.partition("\n")
    return mt.Tool(
        name=tool_name(cmd),
        title=f"penguineer {cmd.name}",
        description=cmd.help,
        input_schema=input_schema(cmd),
        output_schema=cmd.result.model_json_schema(),
        annotations=mt.ToolAnnotations(
            title=first.rstrip(".") or cmd.name, open_world_hint=False, destructive_hint=False
        ),
    )


def _resolve(value: Any, root: Path) -> Any:
    if isinstance(value, str) and value and not Path(value).expanduser().is_absolute():
        return str(root / value)
    if isinstance(value, list):
        return [_resolve(v, root) for v in value]
    return value


def build_server(project: Path, registry: Registry = default_registry) -> Server:
    """The MCP server for ``registry``; ``project`` is the default project folder."""
    from penguineer import __version__

    default_root = project.expanduser().resolve()
    commands = {tool_name(c): c for c in registry}
    lock = anyio.Lock()

    async def list_tools(ctx, params) -> mt.ListToolsResult:
        return mt.ListToolsResult(tools=[tool(c) for c in commands.values()])

    async def call_tool(ctx, params: mt.CallToolRequestParams) -> mt.CallToolResult:
        cmd = commands.get(params.name)
        if cmd is None:
            return _error(f"unknown tool {params.name!r}; known: {', '.join(sorted(commands))}")
        args = dict(params.arguments or {})
        root = Path(args.pop("project", None) or default_root).expanduser()
        root = root if root.is_absolute() else default_root / root
        seed = int(args.pop("seed", 0) or 0)
        for name in path_fields(cmd) & set(args):
            args[name] = _resolve(args[name], root)

        def progress(done: int, total: int, message: str) -> None:
            # A no-op unless the client asked for progress.
            anyio.from_thread.run(ctx.session.report_progress, done, total, message)

        def run():
            return registry.execute(cmd.name, args, project=root, seed=seed, progress=progress)

        async with lock:  # one command at a time, as in the CLI
            try:
                outcome = await anyio.to_thread.run_sync(run)
            except PenguineerError as e:
                return _error(f"{cmd.name} failed (exit code {e.exit_code}): {e}")
        result = outcome.result
        text = f"{result.summary()}\nrun record: {outcome.run_dir / 'run.json'}"
        return mt.CallToolResult(
            content=[mt.TextContent(type="text", text=text)],
            structured_content=result.model_dump(mode="json"),
        )

    return Server(
        "penguineer",
        version=__version__,
        instructions=INSTRUCTIONS,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


def _error(message: str) -> mt.CallToolResult:
    return mt.CallToolResult(content=[mt.TextContent(type="text", text=message)], is_error=True)


def serve(project: Path) -> None:
    """Serve over stdin and stdout until the client closes them."""
    server = build_server(project)

    async def main() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(main)


__all__ = ["build_server", "input_schema", "path_fields", "serve", "tool", "tool_name"]
