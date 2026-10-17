import json

from typer.testing import CliRunner

import penguineer.commands  # noqa: F401
from penguineer.cli import build_app
from penguineer.core import default_registry

runner = CliRunner()
app = build_app(default_registry)


def invoke(*args):
    return runner.invoke(app, [str(a) for a in args])


def test_data_health_end_to_end(demo_csv, demo_graph, tmp_path):
    res = invoke(
        "data", "health", "--data", demo_csv, "--graph", demo_graph, "--project", tmp_path, "--json"
    )
    assert res.exit_code == 0, res.output
    out = json.loads(res.stdout)
    found = {(f["tag"], f["check"]) for f in out["findings"]}
    assert found == {
        ("TI-101.PV", "flatline"),
        ("FI-102.PV", "gap"),
        ("AI-201.PV", "spike"),
        ("PI-103.PV", "out_of_range"),
    }
    assert out["counts"] == {"info": 0, "warning": 3, "critical": 1}


def test_data_health_text_summary(demo_csv, tmp_path):
    res = invoke("data", "health", "--data", demo_csv, "--project", tmp_path)
    assert res.exit_code == 0, res.output
    assert "findings:" in res.stdout and "health.json" in res.stdout
    assert "out-of-range check skipped" in res.stdout


def test_missing_file_is_exit_2(tmp_path):
    res = invoke("data", "health", "--data", tmp_path / "nope.csv", "--project", tmp_path)
    assert res.exit_code == 2


def test_invalid_value_is_exit_2(demo_csv, tmp_path):
    res = invoke("data", "health", "--data", demo_csv, "--gap-factor", "0.5", "--project", tmp_path)
    assert res.exit_code == 2


def test_stub_commands_exit_4(demo_csv, tmp_path):
    res = invoke(
        "investigate", "--lot", "L1", "--lims", demo_csv, "--data", demo_csv, "--project", tmp_path
    )
    assert res.exit_code == 4
    assert "not implemented yet" in res.output
    assert invoke("graph", "draft", "--tags", demo_csv, "--project", tmp_path).exit_code == 4
    assert invoke("bench", "homeostat", "--project", tmp_path).exit_code == 4


def test_graph_check_exit_codes(demo_graph, tmp_path):
    assert invoke("graph", "check", "--graph", demo_graph, "--project", tmp_path).exit_code == 0
    bad = tmp_path / "bad.yaml"
    bad.write_text("nodes: []\nedges:\n  - {id: e, type: flows_to, from: a, to: b}\n")
    res = invoke("graph", "check", "--graph", bad, "--project", tmp_path)
    assert res.exit_code == 1 and "dangling_edge" in res.stdout


def test_init_creates_project(tmp_path):
    project = tmp_path / "my-unit"
    res = invoke("init", "--project", project)
    assert res.exit_code == 0, res.output
    assert (project / "penguineer.yaml").exists() and (project / "graph.yaml").exists()
    assert (
        invoke("graph", "check", "--graph", project / "graph.yaml", "--project", project).exit_code
        == 0
    )
    assert invoke("init", "--project", project).exit_code == 2


def test_version():
    res = invoke("--version")
    assert res.exit_code == 0 and res.stdout.startswith("penguineer ")
