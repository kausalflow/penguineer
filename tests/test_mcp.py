import shutil
import sys
from pathlib import Path

import anyio
import pytest

pytest.importorskip("mcp")

from mcp import Client, StdioServerParameters  # noqa: E402

import penguineer  # noqa: E402
from penguineer.mcp_server import build_server, path_fields, tool_name  # noqa: E402

DEMO = Path(__file__).resolve().parents[1] / "examples" / "demo"


def _project(tmp_path: Path) -> Path:
    for name in ("historian.csv", "graph.yaml"):
        shutil.copy(DEMO / name, tmp_path / name)
    return tmp_path


def test_every_command_is_a_tool(tmp_path):
    async def main():
        async with Client(build_server(tmp_path)) as client:
            return (await client.list_tools()).tools

    tools = anyio.run(main)
    assert sorted(t.name for t in tools) == sorted(
        name.replace(" ", "_") for name in penguineer.schemas()
    )
    health = next(t for t in tools if t.name == "data_health")
    assert {"data", "project", "seed"} <= set(health.input_schema["properties"])
    assert "data" in health.input_schema["required"]
    assert health.output_schema["title"] == "DataHealthResult"


def test_a_call_returns_the_result_and_the_summary(tmp_path):
    project = _project(tmp_path)

    async def main():
        async with Client(build_server(project)) as client:
            # A relative path is read against the project, not the server's folder.
            return await client.call_tool("graph_check", {"graph": "graph.yaml"})

    result = anyio.run(main)
    assert not result.is_error
    expected = penguineer.run("graph check", graph=project / "graph.yaml", project=project)
    assert result.structured_content == expected.model_dump(mode="json")
    assert result.content[0].text.startswith(expected.summary().splitlines()[0])
    assert list((project / "runs").glob("*/run.json"))


def test_errors_carry_the_exit_code(tmp_path):
    async def main():
        async with Client(build_server(tmp_path)) as client:
            missing = await client.call_tool("graph_check", {"graph": "nope.yaml"})
            unknown = await client.call_tool("no_such_tool", {})
            return missing, unknown

    missing, unknown = anyio.run(main)
    assert missing.is_error and "exit code 2" in missing.content[0].text
    assert unknown.is_error and "unknown tool" in unknown.content[0].text


def test_progress_reaches_the_client(tmp_path):
    project = _project(tmp_path)
    seen = []

    async def progress(done, total, message):
        seen.append((done, total, message))

    async def main():
        async with Client(build_server(project)) as client:
            return await client.call_tool(
                "graph_lags",
                {"data": "historian.csv", "graph": "graph.yaml"},
                progress_callback=progress,
            )

    result = anyio.run(main)
    assert not result.is_error and seen


def test_path_fields():
    from penguineer.core import default_registry

    cmd = default_registry.get("investigate")
    assert {"lims", "data", "graph", "lots", "out"} <= path_fields(cmd)
    assert "lot" not in path_fields(cmd)
    assert tool_name(cmd) == "investigate"


def test_over_stdio(tmp_path):
    project = _project(tmp_path)
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "penguineer", "mcp", "--project", str(project)],
    )

    async def main():
        async with Client(server) as client:
            tools = await client.list_tools()
            result = await client.call_tool("graph_check", {"graph": "graph.yaml"})
            return tools, result

    tools, result = anyio.run(main)
    assert "investigate" in {t.name for t in tools.tools}
    assert not result.is_error
