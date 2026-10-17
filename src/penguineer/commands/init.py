from __future__ import annotations

from pathlib import Path

import yaml
from plantgraph import PlantGraph
from pydantic import Field

from penguineer.core import CommandParams, CommandResult, RunContext, UsageError, command
from penguineer.io.graph import save_graph

CONFIG_NAME = "penguineer.yaml"
FOLDERS = ("data", "runs", "reports", ".penguineer/cache")


class InitParams(CommandParams):
    unit: str | None = Field(None, description="Unit name (default: the folder name)")
    timezone: str = Field("UTC", description="Time zone of naive timestamps in exports")
    force: bool = Field(False, description="Overwrite an existing penguineer.yaml")


class InitResult(CommandResult):
    project: str
    unit: str
    created: list[str]

    def output_paths(self) -> list[Path]:
        return [Path(self.project) / c for c in self.created]

    def summary(self) -> str:
        made = "\n".join(f"  {c}" for c in self.created) or "  (nothing new)"
        return f"initialized project for unit {self.unit!r} in {self.project}\n{made}"


@command("init", params=InitParams, result=InitResult)
def init(p: InitParams, ctx: RunContext) -> InitResult:
    """Create a project folder for one unit: penguineer.yaml, an empty graph.yaml, folders."""
    root = ctx.project_root
    config = root / CONFIG_NAME
    if config.exists() and not p.force:
        raise UsageError(f"{config} already exists; pass --force to overwrite it")
    unit = p.unit or root.name
    created: list[str] = []
    for folder in FOLDERS:
        path = root / folder
        if not path.exists():
            path.mkdir(parents=True)
            created.append(folder + "/")
    config.write_text(
        yaml.safe_dump(
            {
                "unit": unit,
                "timezone": p.timezone,
                "data": {"historian": None, "lims": None, "lots": None},
                "llm": {"mode": "none"},
            },
            sort_keys=False,
        )
    )
    created.append(CONFIG_NAME)
    graph = root / "graph.yaml"
    if not graph.exists():
        save_graph(PlantGraph(unit=unit), graph)
        created.append("graph.yaml")
    ignore = root / ".gitignore"
    if not ignore.exists():
        ignore.write_text(".penguineer/\nruns/\ndata/\n")
        created.append(".gitignore")
    return InitResult(project=str(root), unit=unit, created=created)
