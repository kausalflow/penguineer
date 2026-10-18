import json

from penguineer.bench.gate import check, main
from penguineer.bench.score import BenchSummary, wilson


def summary(h3=(120, 160), wrong=(5, 160), catch=(38, 40), errors=0, n=160):
    return BenchSummary(
        n_scenarios=n,
        hit_at_1=wilson(100, n),
        hit_at_3=wilson(*h3),
        wrong_confident_rate=wilson(*wrong),
        data_fault_catch_rate=wilson(*catch),
        errors=errors,
        by_fault_type={},
    )


def test_gate_passes_and_fails_on_each_threshold():
    ok, lines = check(summary())
    assert ok and all(not line.startswith("FAIL") for line in lines)
    for bad in (
        summary(h3=(100, 160)),  # 62%
        summary(wrong=(16, 160)),  # exactly 10% is not below 10%
        summary(catch=(35, 40)),  # 88%
        summary(errors=1),
        summary(n=120, h3=(100, 120), wrong=(1, 120)),
    ):
        ok, lines = check(bad)
        assert not ok and any(line.startswith("FAIL") for line in lines)


def test_gate_command(tmp_path, capsys):
    f = tmp_path / "bench.json"
    f.write_text(json.dumps({"split": "hidden", "summary": summary().model_dump(mode="json")}))
    assert main([str(f)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("release gate on the hidden split: PASSED")
    # The summary only: no scenario, bundle or seed.
    assert "bundles" not in out and "seed" not in out
