import json

import pytest

import penguineer
from penguineer.core import (
    CommandParams,
    CommandResult,
    NotImplementedYet,
    Registry,
    UsageError,
)


def test_python_api_runs_data_health_and_writes_record(demo_csv, tmp_path):
    outcome = penguineer.execute("data health", data=demo_csv, project=tmp_path)
    record = json.loads((outcome.run_dir / "run.json").read_text())
    assert record["status"] == "ok" and record["exit_code"] == 0
    assert record["argv"] == ["penguineer", "data", "health", "--data", str(demo_csv)]
    assert record["inputs"][0]["sha256"] and record["inputs"][0]["bytes"] > 0
    assert record["outputs"] == [str(outcome.run_dir / "health.json")]
    assert outcome.run_dir.parent == tmp_path / "runs"


def test_run_returns_the_result_model(demo_csv, tmp_path):
    result = penguineer.run("data health", data=str(demo_csv), project=tmp_path)
    assert result.n_tags == 5


def test_invalid_params_are_usage_errors(tmp_path):
    with pytest.raises(UsageError):
        penguineer.run("data health", data=tmp_path / "missing.csv", project=tmp_path)
    with pytest.raises(UsageError):
        penguineer.run("no such command", project=tmp_path)


def test_failed_run_still_writes_record(demo_csv, demo_graph, tmp_path):
    with pytest.raises(NotImplementedYet):
        penguineer.run("graph draft", tags=demo_csv, pid=demo_graph, project=tmp_path)
    (record_path,) = (tmp_path / "runs").glob("*/run.json")
    record = json.loads(record_path.read_text())
    assert record["status"] == "error" and record["exit_code"] == 4


def test_reserved_and_duplicate_names():
    reg = Registry()

    class Bad(CommandParams):
        seed: int = 1

    with pytest.raises(ValueError, match="reserved"):
        reg.command("x", params=Bad, result=CommandResult)(lambda p, c: None)

    class Ok(CommandParams):
        n: int = 1

    reg.command("x", params=Ok, result=CommandResult)(lambda p, c: CommandResult())
    with pytest.raises(ValueError, match="already"):
        reg.command("x", params=Ok, result=CommandResult)(lambda p, c: None)


def test_health_json_is_reproducible(demo_csv, demo_graph, tmp_path):
    a = penguineer.execute("data health", data=demo_csv, graph=demo_graph, project=tmp_path)
    b = penguineer.execute("data health", data=demo_csv, graph=demo_graph, project=tmp_path)
    assert a.run_dir != b.run_dir
    assert (a.run_dir / "health.json").read_bytes() == (b.run_dir / "health.json").read_bytes()
