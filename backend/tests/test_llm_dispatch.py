import json
import types
import pytest
from xgraph_gateway import llm


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    llm._OVERRIDE = {}
    for k in ("CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_API_KEY", "ANTHROPIC_VERTEX_PROJECT_ID",
              "CLOUD_ML_REGION", "XGRAPH_LLM_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "XGRAPH_LLM",
              "XGRAPH_LLM_PROVIDER", "XGRAPH_LLM_FAST_MODEL", "GEMINI_API_KEY", "GOOGLE_API_KEY",
              "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(llm.shutil, "which", lambda _: "/usr/bin/claude")
    yield
    llm._OVERRIDE = {}


def _fake_run(captured):
    def run(cmd, **kw):
        captured["cmd"] = cmd
        captured["env"] = kw.get("env")
        return types.SimpleNamespace(returncode=0, stdout=json.dumps({"is_error": False, "result": "hi"}), stderr="")
    return run


def test_cli_vertex_sets_vertex_env(monkeypatch):
    cap = {}
    monkeypatch.setattr(llm.subprocess, "run", _fake_run(cap))
    llm.set_llm_config({"mechanism": "cli", "auth": "vertex", "project": "proj-x", "region": "global"})
    assert llm._llm("hello") == "hi"
    assert cap["env"]["CLAUDE_CODE_USE_VERTEX"] == "1"
    assert cap["env"]["ANTHROPIC_VERTEX_PROJECT_ID"] == "proj-x"
    assert cap["env"]["CLOUD_ML_REGION"] == "global"
    assert "ANTHROPIC_API_KEY" not in cap["env"]


def test_cli_apikey_sets_key_and_drops_vertex(monkeypatch):
    cap = {}
    monkeypatch.setattr(llm.subprocess, "run", _fake_run(cap))
    llm.set_llm_config({"mechanism": "cli", "auth": "apikey", "api_key": "sk-test"})
    llm._llm("hello")
    assert cap["env"]["ANTHROPIC_API_KEY"] == "sk-test"
    assert "CLAUDE_CODE_USE_VERTEX" not in cap["env"]


def test_cli_login_drops_both(monkeypatch):
    cap = {}
    monkeypatch.setattr(llm.subprocess, "run", _fake_run(cap))
    llm.set_llm_config({"mechanism": "cli", "auth": "cli-login"})
    llm._llm("hello")
    assert "ANTHROPIC_API_KEY" not in cap["env"]
    assert "CLAUDE_CODE_USE_VERTEX" not in cap["env"]


def test_sdk_vertex_builds_anthropic_vertex(monkeypatch):
    calls = {}

    class FakeMsg:
        def __init__(self): self.content = [types.SimpleNamespace(type="text", text="sdk-hi")]

    class FakeClient:
        def __init__(self, **kw): calls["init"] = kw
        @property
        def messages(self):
            outer = self
            return types.SimpleNamespace(create=lambda **kw: (calls.__setitem__("create", kw), FakeMsg())[1])

    fake_anthropic = types.SimpleNamespace(Anthropic=FakeClient, AnthropicVertex=FakeClient)
    monkeypatch.setitem(__import__("sys").modules, "anthropic", fake_anthropic)
    llm.set_llm_config({"mechanism": "sdk", "auth": "vertex", "project": "proj-x", "region": "global"})
    assert llm._llm("hello") == "sdk-hi"
    assert calls["init"] == {"project_id": "proj-x", "region": "global"}


def test_stub_still_raises(monkeypatch):
    monkeypatch.setenv("XGRAPH_LLM", "stub")
    with pytest.raises(RuntimeError):
        llm._llm("x")


def _fake_genai(calls, text):
    """Build a fake `google.genai` module whose Client records init/gen kwargs and
    returns a fixed text. Mirrors the real `from google import genai` surface."""
    class FakeModels:
        def generate_content(self, **kw):
            calls["gen"] = kw
            return types.SimpleNamespace(text=text)

    class FakeClient:
        def __init__(self, **kw):
            calls["init"] = kw
            self.models = FakeModels()

    fake_types = types.SimpleNamespace(
        GenerateContentConfig=lambda **kw: ("gcc", kw))
    fake_genai = types.SimpleNamespace(Client=FakeClient, types=fake_types)
    return fake_genai, fake_types


def test_gemini_apikey_text_path(monkeypatch):
    calls = {}
    fake_genai, fake_types = _fake_genai(calls, "gemini-hi")
    sys = __import__("sys")
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)
    llm.set_llm_config({"provider": "gemini", "auth": "apikey", "api_key": "AIza-k"})
    assert llm._llm("hello") == "gemini-hi"
    assert calls["init"] == {"api_key": "AIza-k"}       # apikey client construction
    assert calls["gen"]["model"] == "gemini-2.5-pro"    # Build-tier default model


def test_gemini_schema_path_extracts_json(monkeypatch):
    calls = {}
    # response has prose around the JSON — the regex fallback must still parse it
    fake_genai, fake_types = _fake_genai(calls, 'here you go: {"answer": 42} done')
    sys = __import__("sys")
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)
    llm.set_llm_config({"provider": "gemini", "auth": "apikey", "api_key": "AIza-k"})
    out = llm._llm("q", schema={"type": "object"})
    assert out == {"answer": 42}


def test_gemini_vertex_client_construction(monkeypatch):
    calls = {}
    fake_genai, fake_types = _fake_genai(calls, "ok")
    sys = __import__("sys")
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)
    llm.set_llm_config({"provider": "gemini", "auth": "vertex", "project": "proj-g", "region": "us-central1"})
    llm._llm("hello")
    assert calls["init"]["vertexai"] is True
    assert calls["init"]["project"] == "proj-g"
    assert calls["init"]["location"] == "us-central1"


def test_extract_json_ignores_trailing_prose():
    # SDK/API completions often append an explanation after the JSON object.
    # Greedy `{.*}` over-captures to the last brace -> json.loads "Extra data".
    text = ('{\n  "cypher": "MATCH (n) RETURN n"\n}\n\n'
            'This query returns all nodes {see docs}.')
    assert llm._extract_json(text) == {"cypher": "MATCH (n) RETURN n"}


def test_extract_json_handles_markdown_fences():
    text = '```json\n{"answer": "42"}\n```'
    assert llm._extract_json(text) == {"answer": "42"}


def test_extract_json_skips_leading_prose_brace():
    # A stray brace before the real object must not abort extraction.
    text = 'Note: use {curly} braces. {"answer": "ok"}'
    assert llm._extract_json(text) == {"answer": "ok"}


def test_extract_json_empty_when_no_object():
    assert llm._extract_json("no json here") == {}
