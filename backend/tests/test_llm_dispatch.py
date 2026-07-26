import json
import types
import pytest
from xgraph_gateway import llm


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    llm._OVERRIDE = {}
    for k in ("CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_API_KEY", "ANTHROPIC_VERTEX_PROJECT_ID",
              "CLOUD_ML_REGION", "XGRAPH_LLM_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "XGRAPH_LLM"):
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
