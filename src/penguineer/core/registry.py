"""The command registry: one definition per command, many callers.

A command is a name, a Pydantic ``Params`` model, a Pydantic ``Result`` model and a
``run(params, ctx)`` function. The Typer CLI, ``--json`` output, ``penguineer.run`` and the
JSON Schemas callers build tools from are all generated from the registered command.
"""

from __future__ import annotations

import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError

from penguineer.core.context import LLMMode, ProgressCallback, RunContext
from penguineer.core.errors import PenguineerError, UsageError
from penguineer.core.fields import field_shape, format_value, is_path_type, option_name

RESERVED_FIELDS = frozenset({"json_output", "project", "seed"})


class CommandParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CommandResult(BaseModel):
    """Base for result models. Subclasses may override ``summary`` and ``exit_code``."""

    def summary(self) -> str:
        return self.model_dump_json(indent=2)

    def exit_code(self) -> int:
        return 0

    def output_paths(self) -> list[Path]:
        return []


P = TypeVar("P", bound=BaseModel)
R = TypeVar("R", bound=CommandResult)


@dataclass(frozen=True)
class Command(Generic[P, R]):
    name: str
    params: type[P]
    result: type[R]
    func: Callable[[P, RunContext], R]
    help: str

    @property
    def words(self) -> list[str]:
        return self.name.split()


class Registry:
    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}

    def command(
        self, name: str, *, params: type[BaseModel], result: type[CommandResult]
    ) -> Callable[[Callable], Callable]:
        """Register ``func(params, ctx) -> result`` under ``name`` (e.g. ``"data health"``)."""

        def decorator(func: Callable) -> Callable:
            if name in self._commands:
                raise ValueError(f"command {name!r} is already registered")
            clash = RESERVED_FIELDS & set(params.model_fields)
            if clash:
                raise ValueError(f"{name}: params use reserved field names {sorted(clash)}")
            help_text = (func.__doc__ or "").strip()
            self._commands[name] = Command(name, params, result, func, help_text)
            return func

        return decorator

    def get(self, name: str) -> Command:
        try:
            return self._commands[name]
        except KeyError:
            raise UsageError(
                f"unknown command {name!r}; known: {', '.join(sorted(self._commands))}"
            ) from None

    def __iter__(self):
        return iter(sorted(self._commands.values(), key=lambda c: c.name))

    def schemas(self) -> dict[str, dict[str, Any]]:
        """JSON Schemas of every command's Params and Result, keyed by command name."""
        return {
            c.name: {
                "params": c.params.model_json_schema(),
                "result": c.result.model_json_schema(),
            }
            for c in self
        }

    def to_argv(self, name: str, params: BaseModel) -> list[str]:
        return to_argv(self.get(name), params)

    def execute(
        self,
        name: str,
        params: BaseModel | dict[str, Any] | None = None,
        *,
        project: Path | str = ".",
        seed: int = 0,
        cache_dir: Path | None = None,
        progress: ProgressCallback | None = None,
        cancelled: Callable[[], bool] | None = None,
        write_record: bool = True,
    ) -> RunOutcome:
        """Validate params, run the command and write ``runs/<run_id>/run.json``."""
        cmd = self.get(name)
        if not isinstance(params, cmd.params):
            raw = params.model_dump() if isinstance(params, BaseModel) else (params or {})
            try:
                params = cmd.params.model_validate(raw)
            except ValidationError as e:
                raise UsageError(f"{name}: invalid parameters\n{e}") from e

        project_root = Path(project).resolve()
        ctx_kwargs: dict[str, Any] = {}
        if progress is not None:
            ctx_kwargs["progress"] = progress
        if cancelled is not None:
            ctx_kwargs["cancelled"] = cancelled
        llm = getattr(params, "llm", LLMMode.none)
        ctx = RunContext(
            project_root=project_root,
            run_id=new_run_id(),
            seed=seed,
            llm=LLMMode(llm),
            cache_dir=cache_dir,
            **ctx_kwargs,
        )
        record = RunRecord.start(cmd, params, ctx)
        started = time.perf_counter()
        try:
            result = cmd.func(params, ctx)
        except BaseException as e:
            record.note_llm(ctx)
            record.finish(
                started,
                status="error",
                exit_code=e.exit_code if isinstance(e, PenguineerError) else 4,
                error=str(e) if isinstance(e, PenguineerError) else traceback.format_exc(),
            )
            if write_record:
                record.write(ctx.run_dir)
            raise
        record.note_llm(ctx)
        record.finish(started, status="ok", exit_code=result.exit_code())
        record.outputs = [str(p) for p in result.output_paths()]
        if write_record:
            record.write(ctx.run_dir)
        return RunOutcome(result=result, record=record, run_dir=ctx.run_dir)


@dataclass
class RunOutcome:
    result: CommandResult
    record: RunRecord
    run_dir: Path


def new_run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


def to_argv(cmd: Command, params: BaseModel) -> list[str]:
    """Canonical command line for validated params: defaults left out, field order kept."""
    argv = list(cmd.words)
    for fname, field in type(params).model_fields.items():
        value = getattr(params, fname)
        if not field.is_required() and value == field.get_default(call_default_factory=True):
            continue
        flag = option_name(fname)
        shape = field_shape(field)
        if shape.base is bool and not shape.is_list:
            argv.append(flag if value else "--no-" + flag[2:])
        elif shape.is_list:
            for item in value:
                argv += [flag, format_value(item)]
        elif value is not None:
            argv += [flag, format_value(value)]
    return argv


class InputFile(BaseModel):
    path: str
    sha256: str
    bytes: int


class RunRecord(BaseModel):
    """What ``runs/<run_id>/run.json`` holds. Alchemist seals it into its run manifest."""

    run_id: str
    command: str
    params: dict[str, Any]
    argv: list[str]
    penguineer_version: str
    plantgraph_version: str
    python_version: str
    seed: int
    llm: str
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_prompts: list[str] = []
    inputs: list[InputFile]
    outputs: list[str] = []
    started_at: str
    duration_s: float | None = None
    status: str = "running"
    exit_code: int | None = None
    error: str | None = None

    @classmethod
    def start(cls, cmd: Command, params: BaseModel, ctx: RunContext) -> RunRecord:
        import platform

        import plantgraph

        from penguineer import __version__

        return cls(
            run_id=ctx.run_id,
            command=cmd.name,
            params=params.model_dump(mode="json"),
            argv=["penguineer", *to_argv(cmd, params)],
            penguineer_version=__version__,
            plantgraph_version=plantgraph.__version__,
            python_version=platform.python_version(),
            seed=ctx.seed,
            llm=str(ctx.llm),
            inputs=[_hash_file(p) for p in _input_files(params)],
            started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )

    def note_llm(self, ctx: RunContext) -> None:
        self.llm_provider = ctx.llm_provider
        self.llm_model = ctx.llm_model
        self.llm_prompts = list(ctx.llm_prompts)

    def finish(self, started: float, *, status: str, exit_code: int, error: str | None = None):
        self.duration_s = round(time.perf_counter() - started, 3)
        self.status = status
        self.exit_code = exit_code
        self.error = error

    def write(self, run_dir: Path) -> Path:
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / "run.json"
        path.write_text(self.model_dump_json(indent=2) + "\n")
        return path


def _input_files(params: BaseModel) -> list[Path]:
    files: list[Path] = []
    for fname, field in type(params).model_fields.items():
        if not is_path_type(field_shape(field).base):
            continue
        value = getattr(params, fname)
        for p in value if isinstance(value, (list, tuple)) else [value]:
            if p is not None and Path(p).is_file():
                files.append(Path(p))
    return files


def _hash_file(path: Path) -> InputFile:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return InputFile(path=str(path), sha256=h.hexdigest(), bytes=path.stat().st_size)


default_registry = Registry()
command = default_registry.command
