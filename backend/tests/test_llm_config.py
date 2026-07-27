import os
import pytest
from xgraph_gateway import llm


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    # Start each test from a clean override + a known-empty LLM env.
    llm._OVERRIDE = {}
    for k in ("CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_API_KEY", "ANTHROPIC_VERTEX_PROJECT_ID",
              "CLOUD_ML_REGION", "XGRAPH_LLM_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL"):
        monkeypatch.delenv(k, raising=False)
    yield
    llm._OVERRIDE = {}


def test_default_is_cli_login_when_nothing_set():
    cfg = llm.resolve_llm_config()
    assert cfg["mechanism"] == "cli"
    assert cfg["auth"] == "cli-login"
    assert cfg["model"] == "claude-opus-4-8"
    assert cfg["sources"]["auth"] == "default"


def test_fast_model_is_haiku_and_status_exposes_both():
    assert llm.fast_model() == "claude-haiku-4-5-20251001"
    st = llm.llm_status()
    assert st["model"] == "claude-opus-4-8"          # default/build tier
    assert st["fast_model"] == "claude-haiku-4-5-20251001"  # ask/explain tier


def test_warmup_warms_the_fast_tier(monkeypatch):
    # Ask/Explain run on the fast (Haiku) tier, so warmup MUST fire a call on
    # fast_model() — otherwise the first Explain pays a cold Vertex start on a
    # model the warmup never touched.
    calls = []
    monkeypatch.setattr(llm, "_llm", lambda prompt, model=None, **kw: calls.append(model))
    monkeypatch.setenv("XGRAPH_LLM_WARMUP", "1")
    llm.warmup()
    assert llm.fast_model() in calls          # the interactive tier is warmed
    assert calls[0] == llm.fast_model()       # and warmed FIRST (latency-sensitive)


def test_warmup_noop_when_disabled(monkeypatch):
    calls = []
    monkeypatch.setattr(llm, "_llm", lambda *a, **kw: calls.append(a))
    monkeypatch.setenv("XGRAPH_LLM_WARMUP", "0")
    llm.warmup()
    assert calls == []


def test_env_vertex_becomes_default_auth(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "proj-x")
    monkeypatch.setenv("CLOUD_ML_REGION", "global")
    cfg = llm.resolve_llm_config()
    assert cfg["auth"] == "vertex"
    assert cfg["project"] == "proj-x"
    assert cfg["region"] == "global"
    assert cfg["sources"]["auth"] == "env"


def test_override_beats_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "proj-x")
    eff = llm.set_llm_config({"mechanism": "cli", "auth": "cli-login"})
    assert eff["auth"] == "cli-login"
    assert llm.resolve_llm_config()["sources"]["auth"] == "override"


def test_invalid_combo_rejected():
    with pytest.raises(ValueError):
        llm.set_llm_config({"mechanism": "sdk", "auth": "cli-login"})


def test_apikey_requires_key():
    with pytest.raises(ValueError):
        llm.set_llm_config({"mechanism": "cli", "auth": "apikey"})
    eff = llm.set_llm_config({"mechanism": "cli", "auth": "apikey", "api_key": "sk-test"})
    assert eff["api_key"] == "sk-test"


def test_vertex_requires_project():
    with pytest.raises(ValueError):
        llm.set_llm_config({"mechanism": "cli", "auth": "vertex"})  # no project anywhere


def test_status_hides_key():
    llm.set_llm_config({"mechanism": "cli", "auth": "apikey", "api_key": "sk-secret"})
    st = llm.llm_status()
    assert st["has_api_key"] is True
    assert "api_key" not in st
    assert st["auth"] == "apikey"


def test_failed_set_restores_previous_override():
    llm.set_llm_config({"mechanism": "cli", "auth": "cli-login"})
    with pytest.raises(ValueError):
        llm.set_llm_config({"mechanism": "sdk", "auth": "cli-login"})
    assert llm.resolve_llm_config()["auth"] == "cli-login"  # unchanged
