import json
from pathlib import Path

import pytest
from plantgraph import Edge

import penguineer
from penguineer.core import LLMMode, UsageError
from penguineer.graph.check import check_graph
from penguineer.graph.draft import TagRow, draft_from_tags
from penguineer.graph.propose import (
    ProposedEdge,
    RelationsDraft,
    open_items,
    propose_relations,
    request_input,
)
from penguineer.llm import LLMError

ROWS = [
    TagRow("TI-104.PV", "Reactor R-101 temperature", "degC"),
    TagRow("TI-201", "Heater E-201 outlet", "degC"),
    TagRow("PI-110", "Reactor pressure", "bar"),
    TagRow("FT-102", "Feed flow", "m3/h"),
    TagRow("FIC-102.SP", unit="m3/h"),
    TagRow("FV-102", "Feed valve position", "%"),
    TagRow("GRADE"),
]


def edge(type_, a, b, confidence="medium", **kw):
    data = {"type": type_, "from": a, "to": b, "confidence": confidence, "reason": "r", **kw}
    return ProposedEdge.model_validate(data)


class Fake:
    name, model = "local", "m"

    def __init__(self, answers):
        self.answers = list(answers)
        self.seen = []

    def payload(self, prompt, schema):
        return {}

    def structured(self, prompt, schema):
        self.seen.append(json.loads(prompt.user))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return RelationsDraft(edges=answer)


def test_what_is_open_and_what_the_llm_sees():
    draft = draft_from_tags(ROWS)
    tags, actuators, no_piping = open_items(draft.graph)
    # TI-104.PV and TI-201 name their equipment; GRADE is no instrument; FV-102 is the valve.
    assert [t.tag for t in tags] == ["PI-110", "FT-102"]
    assert actuators == ["FV-102-valve"] and no_piping
    seen = request_input(draft.graph, tags, actuators, no_piping)
    assert set(seen) == {"nodes", "edges", "tags_to_place", "actuators_to_place", "ask_piping"}
    assert {n["id"] for n in seen["nodes"]} >= {"R-101", "FV-102-valve"}
    # Names, units and descriptions only: no data, no ranges, no confidences.
    assert all(
        set(t) <= {"id", "tag", "variable", "unit", "description"} for t in seen["tags_to_place"]
    )


def test_proposals_are_checked_before_they_are_kept():
    draft = draft_from_tags(ROWS)
    open_ids = {t.id for t in open_items(draft.graph)[0]}
    llm = Fake(
        [
            [
                edge("measures", "PI-110", "R-101", "high", measured_variable="pressure"),
                edge("manipulates", "FV-102-valve", "R-101"),
                edge("flows_to", "E-201", "R-101"),
                edge("flows_to", "R-101", "E-201"),  # closes a cycle
                edge("measures", "TI-104.PV", "E-201"),  # already placed
                edge("measures", "FT-102", "X-9"),  # invented
                edge("measures", "R-101", "E-201"),  # wrong types
            ]
        ]
    )
    out = propose_relations(draft, llm)
    kept = {(e.type, e.from_id, e.to_id) for e in out.kept}
    assert kept == {
        ("measures", "PI-110", "R-101"),
        ("manipulates", "FV-102-valve", "R-101"),
        ("flows_to", "E-201", "R-101"),
    }
    assert all(e.source == "llm" and e.provenance == "inferred" for e in out.kept)
    assert max(e.confidence for e in out.kept) == 0.5  # below every rule-based edge
    reasons = " | ".join(out.rejected)
    for why in ("would close a cycle", "was not asked about", "X-9 is not a node", "cannot go"):
        assert why in reasons, reasons
    assert "PI-110" in open_ids and out.requests == 1
    # No structural error and no cycle (the incomplete loop is the tag list's).
    codes = {i.code for i in check_graph(draft.graph)[0] if i.level == "error"}
    assert codes <= {"loop_incomplete"}


def test_no_piping_question_when_the_draft_has_piping():
    draft = draft_from_tags(ROWS)
    draft.graph.edges.append(Edge(id="f1", type="flows_to", **{"from": "E-201", "to": "R-101"}))
    llm = Fake([[edge("flows_to", "R-101", "E-201")]])
    out = propose_relations(draft, llm)
    assert llm.seen[0]["ask_piping"] is False
    assert out.kept == [] and "has piping already" in out.rejected[0]


def test_an_llm_failure_keeps_the_rule_based_draft():
    draft = draft_from_tags(ROWS)
    before = list(draft.graph.edges)
    out = propose_relations(draft, Fake([LLMError("no answer")]))
    assert out.failed == "no answer" and draft.graph.edges == before


def test_command_dry_run_sends_nothing(tmp_path):
    tags = tmp_path / "tags.csv"
    tags.write_text(
        "tag,description,unit\n"
        + "\n".join(f"{r.tag},{r.description or ''},{r.unit or ''}" for r in ROWS)
        + "\n"
    )
    result = penguineer.run(
        "graph draft",
        tags=tags,
        project=tmp_path,
        llm=LLMMode.api,
        llm_model="m",
        llm_dry_run=True,
    )
    (request,) = json.loads(Path(result.llm_payloads).read_text())
    assert request["prompt"] == "relations@1"
    assert result.n_llm == 0 and Path(result.graph).exists()
    assert "not sent" in result.summary()
    record = json.loads(next((tmp_path / "runs").glob("*/run.json")).read_text())
    assert record["llm_prompts"] == ["relations@1"]
    with pytest.raises(UsageError, match="needs --llm"):
        penguineer.run("graph draft", tags=tags, project=tmp_path, force=True, llm_dry_run=True)


def test_command_with_an_unreachable_llm(tmp_path):
    (tmp_path / "penguineer.yaml").write_text("llm:\n  base_url: http://127.0.0.1:9/v1\n")
    tags = tmp_path / "tags.csv"
    tags.write_text("tag,description\nPI-110,Reactor pressure\n")
    result = penguineer.run(
        "graph draft", tags=tags, project=tmp_path, llm=LLMMode.local, llm_model="m"
    )
    assert any("gave no answer" in n for n in result.notes)
