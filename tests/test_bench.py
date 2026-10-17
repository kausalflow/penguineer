import json
import os
import shutil
from pathlib import Path

import pytest

import penguineer
from penguineer.bench.export import plant_graph
from penguineer.bench.plant import BASE, FAULTS, scenario_config
from penguineer.bench.score import names_cause, run_scenario, summarize, wilson
from penguineer.core import UsageError
from penguineer.graph.check import check_graph
from penguineer.io.homeostat import Truth, bundles_in, read_bundle
from penguineer.report.model import Hypothesis

DEMO = Path(__file__).resolve().parents[1] / "examples" / "offspec"


def _bundle(folder: Path, root_cause: list[str], data_fault: bool = False) -> Path:
    """A scenario bundle made from the off-spec demo plant, whose answer is known."""
    folder.mkdir(parents=True)
    for name in ("historian.csv", "lims.csv", "lots.csv", "graph.yaml"):
        shutil.copy(DEMO / name, folder / name)
    truth = Truth(
        scenario=folder.name,
        fault_type="fouling" if not data_fault else "sensor",
        kind="process",
        data_fault=data_fault,
        root_cause=root_cause,
        onset="2026-08-03T05:00:00Z",
        lot="L0803-A",
    )
    (folder / "truth.json").write_text(truth.model_dump_json())
    return folder


def _h(kind: str, node: str, tag: str | None = None) -> Hypothesis:
    return Hypothesis(
        rank=1,
        kind=kind,
        cause_node=node,
        cause_tag=tag,
        direction="low",
        graph_path=[node],
        dead_time_min=0,
        tests=[],
        score=0.5,
        confidence="low",
        summary="",
    )


def test_wilson_interval():
    r = wilson(21, 30)
    assert r.value == 0.7 and 0.5 < r.low < 0.53 and 0.83 < r.high < 0.86
    assert wilson(0, 0).value is None


def test_a_process_hypothesis_must_name_the_tag():
    roots = {"R-301", "TIC-301"}
    # A tag that merely sits on the faulty equipment does not name the cause...
    assert not names_cause(_h("process", "R-301", "TT-301"), roots)
    # ...but a loop, a hidden change or a measurement fault is a claim about its node.
    assert names_cause(_h("lost_control", "TIC-301", "TT-301"), roots)
    assert names_cause(_h("unmeasured_disturbance", "R-301"), roots)
    assert names_cause(_h("process", "M-101", "AT-101"), {"AT-101"})


def test_the_bench_plant_graph_checks_clean():
    scenario = {
        "units": [
            {"id": "MX-101", "template": "mixer", "volume": 2.0},
            {"id": "TIC-201", "template": "heat_exchanger"},
            {"id": "TIC-301", "template": "reactor", "volume": 5.0},
        ]
    }
    graph = plant_graph(scenario)
    issues, _ = check_graph(graph)
    assert not [i for i in issues if i.level == "error"], issues
    reactor = next(e for e in graph.edges if e.from_id == "R-301")
    assert reactor.dead_time_min == 25.0  # 5 m3 at 20 m3/h, plus the 10-minute back-mixing
    tags = graph.tag_by_name()
    for fault in FAULTS.values():
        for name in fault.root_cause:
            assert name in tags or any(n.id == name for n in graph.nodes), (fault.name, name)


def test_scenario_configs_inject_one_fault():
    for fault in FAULTS.values():
        cfg = scenario_config(fault, 7, 3)
        assert cfg["family"] == {"plants": 3} and len(cfg["interventions"]) == 1
        assert "interventions" not in BASE


def test_bench_scores_the_demo_plant(tmp_path):
    _bundle(tmp_path / "a-hit", ["TI-101.PV", "E-101"])
    _bundle(tmp_path / "b-miss", ["PT-100.PV"])
    paths = bundles_in(tmp_path)
    assert [p.name for p in paths] == ["a-hit", "b-miss"]
    hit, inv = run_scenario(paths[0])
    assert hit.rank == 1 and hit.top == "TI-101.PV" and inv is not None
    miss, _ = run_scenario(paths[1])
    # The decoy is in the list but not first; it is wrong-confident only if the top is high.
    assert miss.rank != 1
    summary = summarize([hit, miss])
    assert summary.hit_at_1.value == 0.5
    assert summary.wrong_confident_rate.value == (0.5 if hit.top_confidence == "high" else 0.0)


def test_bench_command_on_bundles(tmp_path):
    bundles = tmp_path / "bundles"
    _bundle(bundles / "one", ["TI-101.PV"])
    result = penguineer.run(
        "bench homeostat", bundles=bundles, project=tmp_path, out=tmp_path / "out"
    )
    assert result.n_scenarios == 1 and result.hit_at_1 == 1.0
    report = json.loads(result.report.read_text())
    assert report["scenarios"][0]["rank"] == 1
    assert "hit@3" in result.summary()


def test_read_bundle_needs_the_files(tmp_path):
    (tmp_path / "x").mkdir()
    with pytest.raises(UsageError, match="not a scenario bundle"):
        read_bundle(tmp_path / "x")


@pytest.mark.skipif(
    os.environ.get("PENGUINEER_BENCH") != "1", reason="set PENGUINEER_BENCH=1 to run Homeostat"
)
def test_export_one_scenario(tmp_path):
    from penguineer.bench.export import export_split

    index = export_split(tmp_path, split_seed=1000, per_fault=1, faults=["reactor_setpoint"])
    assert index["scenarios"] == ["reactor_setpoint-000"]
    b = read_bundle(tmp_path / "reactor_setpoint-000")
    assert b.truth.kind == "setpoint_change" and "AT-301" not in set(b.historian["tag"])
    score, _ = run_scenario(tmp_path / "reactor_setpoint-000")
    assert score.rank == 1
