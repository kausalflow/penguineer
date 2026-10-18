import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

import penguineer
from penguineer.core import LLMMode, UsageError
from penguineer.llm import (
    CachedProvider,
    LLMConfig,
    LLMError,
    load_config,
    load_prompt,
    make_provider,
)
from penguineer.llm.provider import AnthropicAPI, DryRun, OpenAICompatible
from penguineer.report.model import Claim, Report
from penguineer.report.summary import SummaryDraft, check, template_summary, write_summary

DEMO = Path(__file__).resolve().parents[1] / "examples" / "offspec"


def _investigate(project: Path, **kw):
    return penguineer.run(
        "investigate",
        lot="L0803-A",
        lims=DEMO / "lims.csv",
        data=DEMO / "historian.csv",
        graph=DEMO / "graph.yaml",
        lots=DEMO / "lots.csv",
        project=project,
        **kw,
    )


@pytest.fixture(scope="module")
def report(tmp_path_factory) -> Report:
    result = _investigate(tmp_path_factory.mktemp("demo"))
    return Report.model_validate_json(Path(result.report_json).read_text())


PLANT = {"TI-101.PV", "PT-100.PV", "PI-103.PV", "FI-102.PV", "TI-104.PV", "E-101", "R-101", "V-9"}


def grounded(r: Report) -> SummaryDraft:
    """A draft an honest LLM could write from the input: one claim per hypothesis."""
    claims = [
        Claim(
            cause=h.cause_tag or h.cause_node,
            confidence=h.confidence,
            evidence=h.evidence[:1],
            sentence=f"{h.cause_tag or h.cause_node} moved {h.direction} along "
            f"{' to '.join(h.graph_path)}.",
        )
        for h in r.hypotheses[:3]
    ]
    return SummaryDraft(overview=f"{r.event.property} went off spec.", claims=claims)


# ------------------------------------------------------------ config and providers


def test_config_flags_beat_env_beat_file(tmp_path, monkeypatch):
    (tmp_path / "penguineer.yaml").write_text(
        "llm:\n  mode: none\n  model: file-model\n  base_url: http://plant:8000/v1\n"
    )
    monkeypatch.delenv("PENGUINEER_LLM_MODEL", raising=False)
    c = load_config(tmp_path, LLMMode.local)
    assert (c.mode, c.model, c.base_url) == (LLMMode.local, "file-model", "http://plant:8000/v1")
    monkeypatch.setenv("PENGUINEER_LLM_MODEL", "env-model")
    assert load_config(tmp_path, LLMMode.local).model == "env-model"
    assert load_config(tmp_path, LLMMode.local, "flag-model").model == "flag-model"


def test_no_model_no_default(tmp_path, monkeypatch):
    monkeypatch.delenv("PENGUINEER_LLM_MODEL", raising=False)
    assert make_provider(load_config(tmp_path, LLMMode.none)) is None
    with pytest.raises(UsageError, match="needs a model"):
        make_provider(load_config(tmp_path, LLMMode.local))


def test_api_needs_its_key(monkeypatch):
    pytest.importorskip("anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config = LLMConfig(mode=LLMMode.api, model="m", provider="anthropic")
    with pytest.raises(UsageError, match="ANTHROPIC_API_KEY"):
        make_provider(config)
    # A dry run sends nothing, so it needs neither the SDK nor a key.
    assert isinstance(make_provider(config, dry_run=True), DryRun)


class _Endpoint(BaseHTTPRequestHandler):
    answer = None  # set per test: a function of the request body
    requests: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append((self.path, body))
        status, content = type(self).answer(body)
        data = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def endpoint():
    """A local OpenAI-compatible endpoint; set ``_Endpoint.answer`` to script it."""
    _Endpoint.requests = []
    server = HTTPServer(("127.0.0.1", 0), _Endpoint)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/v1"
    server.shutdown()


def test_local_endpoint_round_trip(endpoint):
    _Endpoint.answer = lambda body: (200, json.dumps({"overview": "x", "claims": []}))
    provider = OpenAICompatible(model="m", base_url=endpoint, timeout_s=5)
    prompt = load_prompt("summary", 1).with_input({"a": 1})
    draft = provider.structured(prompt, SummaryDraft)
    assert draft.overview == "x"
    path, body = _Endpoint.requests[0]
    assert path == "/v1/chat/completions"
    assert body["model"] == "m" and body["response_format"]["type"] == "json_schema"
    assert body["messages"][1]["content"] == prompt.user

    _Endpoint.answer = lambda body: (200, "not json")
    with pytest.raises(LLMError, match="does not fit"):
        provider.structured(prompt, SummaryDraft)
    _Endpoint.answer = lambda body: (500, "")
    with pytest.raises(LLMError, match="HTTP 500"):
        provider.structured(prompt, SummaryDraft)
    with pytest.raises(LLMError):
        OpenAICompatible(model="m", base_url="http://127.0.0.1:9", timeout_s=2).structured(
            prompt, SummaryDraft
        )


def test_anthropic_answers_through_the_forced_tool():
    calls = []

    def create(**payload):
        calls.append(payload)
        block = SimpleNamespace(type="tool_use", input={"overview": "y", "claims": []})
        return SimpleNamespace(content=[SimpleNamespace(type="text"), block])

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    provider = AnthropicAPI(model="m", client=client)
    draft = provider.structured(load_prompt("summary", 1).with_input({}), SummaryDraft)
    assert draft.overview == "y"
    assert calls[0]["tool_choice"] == {"type": "tool", "name": "answer"}
    assert calls[0]["tools"][0]["input_schema"]["title"] == "SummaryDraft"


def test_cache_answers_the_second_call(tmp_path):
    class Counting:
        name, model, n = "local", "m", 0

        def payload(self, prompt, schema):
            return {"user": prompt.user}

        def structured(self, prompt, schema):
            self.n += 1
            return schema(overview=f"call {self.n}", claims=[])

    inner = Counting()
    cached = CachedProvider(inner, tmp_path)
    prompt = load_prompt("summary", 1).with_input({"lot": "A"})
    assert cached.structured(prompt, SummaryDraft).overview == "call 1"
    assert cached.structured(prompt, SummaryDraft).overview == "call 1"
    assert cached.structured(prompt.with_input({"lot": "B"}), SummaryDraft).overview == "call 2"
    assert inner.n == 2


# ------------------------------------------------------------ the grounded summary


def test_template_passes_its_own_check(report):
    s = template_summary(report)
    assert s.source == "template" and "TI-101.PV" in s.overview
    draft = SummaryDraft(overview=s.overview, claims=[c for c in s.claims if c.evidence])
    assert check(draft, report, PLANT) == []


def test_grounded_draft_is_kept(report):
    draft = grounded(report)
    assert check(draft, report, PLANT) == []

    class Fixed:
        name, model = "local", "m"

        def payload(self, prompt, schema):
            return {}

        def structured(self, prompt, schema):
            return draft

    s = write_summary(report, Fixed(), PLANT)
    assert s.source == "llm" and s.prompt == "summary@1" and s.fallback is None


@pytest.mark.parametrize(
    "change, problem",
    [
        (lambda d: d.claims[0].__setattr__("cause", "V-9"), "not a hypothesis"),
        (lambda d: d.claims[0].__setattr__("evidence", ["E999"]), "E999, which does not exist"),
        (lambda d: d.claims[0].__setattr__("evidence", []), "cites no evidence"),
        (lambda d: d.claims[1].__setattr__("confidence", "high"), "the label is"),
        (lambda d: d.__setattr__("overview", "V-9 tripped."), "names V-9"),
        (lambda d: d.__setattr__("overview", "It is certainly TI-101.PV."), "'certainly'"),
        (lambda d: d.__setattr__("overview", "See E042."), "E042, which does not exist"),
        (
            lambda d: d.claims[1].__setattr__("sentence", "PT-100.PV, with high confidence."),
            "high confidence above the label",
        ),
    ],
)
def test_ungrounded_drafts_are_rejected(report, change, problem):
    assert report.hypotheses[1].confidence != "high"
    draft = grounded(report)
    change(draft)
    problems = check(draft, report, PLANT)
    assert any(problem in p for p in problems), problems


def test_a_rejected_draft_falls_back_to_the_template(report):
    class Inventive:
        name, model = "local", "m"

        def payload(self, prompt, schema):
            return {}

        def structured(self, prompt, schema):
            return SummaryDraft(overview="V-9 tripped.", claims=[])

    s = write_summary(report, Inventive(), PLANT)
    assert s.source == "template" and "names V-9" in s.fallback
    assert s.overview == template_summary(report).overview


# ------------------------------------------------------------ the command


def test_investigate_with_a_local_llm(tmp_path, endpoint):
    def answer(body):
        data = json.loads(body["messages"][1]["content"])
        return 200, grounded_from_input(data["hypotheses"]).model_dump_json()

    _Endpoint.answer = answer
    (tmp_path / "penguineer.yaml").write_text(f"llm:\n  base_url: {endpoint}\n")
    result = _investigate(tmp_path, llm=LLMMode.local, llm_model="plant-model")
    assert result.summary_source == "llm"
    report = Report.model_validate_json(Path(result.report_json).read_text())
    assert report.summary.model == "plant-model"
    assert "checked against them" in Path(result.report_html).read_text()
    record = json.loads(next((tmp_path / "runs").glob("*/run.json")).read_text())
    assert (record["llm"], record["llm_provider"], record["llm_model"]) == (
        "local",
        "local",
        "plant-model",
    )
    assert record["llm_prompts"] == ["summary@1"]
    # What was sent is the results only, never the time series.
    sent = json.loads(_Endpoint.requests[0][1]["messages"][1]["content"])
    assert set(sent) == INPUT_KEYS
    # The same results again come from the cache.
    _investigate(tmp_path, llm=LLMMode.local, llm_model="plant-model")
    assert len(_Endpoint.requests) == 1


INPUT_KEYS = {"event", "regime", "reference_lots", "hypotheses", "evidence", "ruled_out", "notes"}


def grounded_from_input(hs) -> SummaryDraft:
    return SummaryDraft(
        overview="The lot went off spec.",
        claims=[
            Claim(
                cause=h["cause_tag"] or h["cause_node"],
                confidence=h["confidence"],
                evidence=h["evidence"][:1],
                sentence="It moved.",
            )
            for h in hs[:3]
            if h["evidence"]
        ],
    )


def test_unreachable_llm_falls_back(tmp_path):
    (tmp_path / "penguineer.yaml").write_text("llm:\n  base_url: http://127.0.0.1:9/v1\n")
    result = _investigate(tmp_path, llm=LLMMode.local, llm_model="m")
    assert result.summary_source == "template"
    report = Report.model_validate_json(Path(result.report_json).read_text())
    assert report.summary.fallback and "127.0.0.1:9" in report.summary.fallback
    assert "was not used" in Path(result.report_md).read_text()


def test_dry_run_sends_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = _investigate(tmp_path, llm=LLMMode.api, llm_model="m", llm_dry_run=True)
    sent = json.loads(Path(result.llm_payloads).read_text())
    assert sent[0]["endpoint"] == "https://api.anthropic.com/v1/messages"
    assert sent[0]["prompt"] == "summary@1"
    user = json.loads(sent[0]["body"]["messages"][0]["content"])
    assert set(user) == INPUT_KEYS
    assert result.summary_source == "template"
    assert "not sent" in result.summary()
    assert not (tmp_path / ".penguineer" / "cache" / "llm").exists()


def test_dry_run_needs_an_llm(tmp_path):
    with pytest.raises(UsageError, match="needs --llm"):
        _investigate(tmp_path, llm_dry_run=True)
