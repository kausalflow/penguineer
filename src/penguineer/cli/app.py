"""Build a Typer app from a :class:`Registry`.

Typer does not take a Pydantic model as a parameter, so each command's function signature is
generated from its ``Params`` fields: every field becomes an option with its type, default and
help. Pydantic then validates the parsed values, so the CLI and the Python API report the same
errors.
"""

from __future__ import annotations

import inspect
import os
import sys
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import BaseModel, ValidationError

from penguineer.core.errors import PenguineerError, UsageError
from penguineer.core.fields import field_shape, is_path_type
from penguineer.core.registry import Command, Registry

_GROUP_HELP = {
    "data": "Check historian and LIMS exports.",
    "graph": "Draft, check and enrich the plant graph.",
    "bench": "Run the pipeline on benchmark plants with known faults.",
}


def _option_for(fname: str, cmd: Command) -> inspect.Parameter:
    field = cmd.params.model_fields[fname]
    shape = field_shape(field)
    base = shape.base
    opt: dict[str, Any] = {"help": field.description, "show_default": True}

    if isinstance(base, type) and issubclass(base, Enum):
        cli_type: Any = base
    elif is_path_type(base):
        cli_type = Path
        if shape.path_type == "file":
            opt.update(exists=True, dir_okay=False, readable=True)
        elif shape.path_type == "dir":
            opt.update(exists=True, file_okay=False)
    elif base in (int, float, bool, str):
        cli_type = base
    elif base in (datetime, date):
        # Parsed by Pydantic, so ISO 8601 with offsets round-trips exactly.
        cli_type = str
        opt["metavar"] = "ISO8601"
    else:
        cli_type = str

    if shape.is_list:
        cli_type = list[cli_type]
    if field.is_required():
        default: Any = inspect.Parameter.empty
    else:
        default = field.get_default(call_default_factory=True)
        if shape.is_list or default is None:
            default = None
            cli_type = cli_type | None
        elif isinstance(default, (datetime, date)):
            default = default.isoformat()
    return inspect.Parameter(
        fname,
        inspect.Parameter.KEYWORD_ONLY,
        default=default,
        annotation=Annotated[cli_type, typer.Option(**opt)],
    )


_COMMON = [
    inspect.Parameter(
        "json_output",
        inspect.Parameter.KEYWORD_ONLY,
        default=False,
        annotation=Annotated[
            bool, typer.Option("--json", help="Print the result as JSON on stdout.")
        ],
    ),
    inspect.Parameter(
        "project",
        inspect.Parameter.KEYWORD_ONLY,
        default=Path("."),
        annotation=Annotated[
            Path,
            typer.Option(help="Project folder; run records and outputs go under it."),
        ],
    ),
    inspect.Parameter(
        "seed",
        inspect.Parameter.KEYWORD_ONLY,
        default=0,
        annotation=Annotated[int, typer.Option(help="Random seed for reproducible runs.")],
    ),
]


def kwargs_to_params(cmd: Command, kwargs: dict[str, Any]) -> BaseModel:
    """Turn parsed CLI values into a validated ``Params`` (defaults left to Pydantic)."""
    data = {}
    for fname, value in kwargs.items():
        if fname not in cmd.params.model_fields:
            continue
        if value is None or (
            isinstance(value, (list, tuple))
            and not value
            and not cmd.params.model_fields[fname].is_required()
        ):
            continue
        data[fname] = list(value) if isinstance(value, tuple) else value
    return cmd.params.model_validate(data)


def _make_callback(registry: Registry, cmd: Command):
    def callback(**kwargs: Any) -> None:
        json_output = kwargs.pop("json_output")
        project = kwargs.pop("project")
        seed = kwargs.pop("seed")
        try:
            params = kwargs_to_params(cmd, kwargs)
        except ValidationError as e:
            typer.echo(f"penguineer {cmd.name}: invalid parameters\n{e}", err=True)
            raise typer.Exit(2) from None
        try:
            outcome = registry.execute(cmd.name, params, project=project, seed=seed)
        except PenguineerError as e:
            typer.echo(f"penguineer {cmd.name}: {e}", err=True)
            raise typer.Exit(e.exit_code) from None
        except Exception as e:
            if os.environ.get("PENGUINEER_DEBUG"):
                raise
            typer.echo(f"penguineer {cmd.name}: internal error: {e!r}", err=True)
            raise typer.Exit(4) from None
        result = outcome.result
        if json_output:
            typer.echo(result.model_dump_json(indent=2))
        else:
            typer.echo(result.summary())
            typer.echo(f"run record: {outcome.run_dir / 'run.json'}", err=True)
        code = result.exit_code()
        if code:
            raise typer.Exit(code)

    params = [_option_for(f, cmd) for f in cmd.params.model_fields] + _COMMON
    callback.__signature__ = inspect.Signature(params)  # type: ignore[attr-defined]
    callback.__name__ = cmd.words[-1].replace("-", "_")
    callback.__doc__ = cmd.help
    return callback


def build_app(registry: Registry) -> typer.Typer:
    app = typer.Typer(
        name="penguineer",
        help="Find the root cause of off-spec lots from historian and LIMS exports.",
        no_args_is_help=True,
        add_completion=False,
        pretty_exceptions_enable=False,
    )
    groups: dict[str, typer.Typer] = {}
    for cmd in registry:
        target = app
        for word in cmd.words[:-1]:
            if word not in groups:
                groups[word] = typer.Typer(no_args_is_help=True, help=_GROUP_HELP.get(word))
                app.add_typer(groups[word], name=word)
            target = groups[word]
        target.command(name=cmd.words[-1], help=cmd.help)(_make_callback(registry, cmd))

    @app.command(
        "mcp",
        help="Serve every command as a tool to an MCP client, over stdio (needs penguineer[mcp]).",
    )
    def _mcp(
        project: Annotated[
            Path,
            typer.Option(
                "--project", help="Default project folder for outputs and relative paths."
            ),
        ] = Path("."),
    ) -> None:
        try:
            from penguineer.mcp_server import serve
        except PenguineerError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(e.exit_code) from None
        serve(project)

    @app.callback(invoke_without_command=True)
    def _root(
        version: Annotated[
            bool, typer.Option("--version", help="Show the version and exit.", is_eager=True)
        ] = False,
    ) -> None:
        if version:
            from penguineer import __version__

            typer.echo(f"penguineer {__version__}")
            raise typer.Exit()

    return app


def parse_argv(registry: Registry, argv: list[str]) -> tuple[str, BaseModel]:
    """Parse a command line exactly as the CLI would, without running it."""
    cmd_obj = typer.main.get_command(build_app(registry))
    words: list[str] = []
    rest = list(argv)
    while hasattr(cmd_obj, "list_commands"):  # a command group
        if not rest:
            raise UsageError(f"incomplete command {' '.join(words)!r}")
        word = rest.pop(0)
        words.append(word)
        cmd_obj = cmd_obj.get_command(
            cmd_obj.make_context("penguineer", [], resilient_parsing=True), word
        )
        if cmd_obj is None:
            raise UsageError(f"unknown command {' '.join(words)!r}")
    parsed = cmd_obj.make_context(" ".join(words), rest)
    cmd = registry.get(" ".join(words))
    return cmd.name, kwargs_to_params(cmd, dict(parsed.params))


def main() -> None:
    import penguineer.commands  # noqa: F401  registers the built-in commands
    from penguineer.core.registry import default_registry

    app = build_app(default_registry)
    sys.exit(app(standalone_mode=True) or 0)
