"""to_argv(params) parsed by the generated CLI must give back the same params."""

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import Field, FilePath

import penguineer.commands  # noqa: F401
from penguineer.cli import parse_argv
from penguineer.core import CommandParams, CommandResult, Registry, default_registry, to_argv

HERE = Path(__file__)


class Color(StrEnum):
    red = "red"
    green = "green"


class RichParams(CommandParams):
    name: str
    count: int = Field(3, ge=0)
    ratio: float = 0.5
    flag: bool = False
    on: bool = True
    color: Color = Color.red
    items: list[str] = Field(default_factory=list)
    when: datetime | None = None
    src: FilePath = HERE
    out: Path | None = None


reg = Registry()
reg.command("demo rich", params=RichParams, result=CommandResult)(lambda p, c: CommandResult())

text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc")), min_size=1, max_size=12
).filter(lambda s: not s.startswith("-"))

rich_params = st.builds(
    RichParams,
    name=text,
    count=st.integers(0, 10**6),
    ratio=st.floats(allow_nan=False, allow_infinity=False),
    flag=st.booleans(),
    on=st.booleans(),
    color=st.sampled_from(Color),
    items=st.lists(text, max_size=3),
    when=st.none() | st.datetimes(timezones=st.just(UTC)),
    src=st.just(HERE),
    out=st.none() | text.map(Path),
)


@settings(max_examples=150, deadline=None)
@given(rich_params)
def test_round_trip(params):
    name, parsed = parse_argv(reg, to_argv(reg.get("demo rich"), params))
    assert name == "demo rich"
    assert parsed == params


def test_defaults_are_left_out():
    p = RichParams(name="x")
    assert to_argv(reg.get("demo rich"), p) == ["demo", "rich", "--name", "x"]


def test_round_trip_builtin_command(demo_csv):
    cmd = default_registry.get("data health")
    p = cmd.params(data=demo_csv, tags=["A", "B"], spike_z=6.5, start="2026-08-03T01:00:00+02:00")
    assert parse_argv(default_registry, to_argv(cmd, p)) == ("data health", p)
