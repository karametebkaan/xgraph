import os
import pytest
from xgraph_gateway import llm


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    # Start each test from a clean override + a known-empty LLM env.
    llm._OVERRIDE = {}
    for k in ("CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_API_KEY", "ANTHROPIC_VERTEX_PROJECT_ID",
              "CLOUD_ML_REGION", "XGRAPH_LLM_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
              "XGRAPH_LLM_MECHANISM", "XGRAPH_LLM_PROVIDER", "XGRAPH_LLM_FAST_MODEL",
              "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI",
              "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"):
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


def test_vertex_model_id_maps_dated_haiku_to_at_form():
    # Vertex publisher model IDs use an @-version suffix for DATED models; the
    # `claude` CLI maps this internally but the SDK passes the string verbatim,
    # so the SDK-on-Vertex route must translate: haiku's dated id needs @, the
    # undated opus alias is used as-is.
    assert llm._vertex_model_id("claude-haiku-4-5-20251001") == "claude-haiku-4-5@20251001"
    assert llm._vertex_model_id("claude-opus-4-8") == "claude-opus-4-8"
    assert llm._vertex_model_id("") == ""


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


def test_mechanism_defaults_to_cli():
    assert llm.resolve_llm_config()["mechanism"] == "cli"


def test_env_mechanism_sdk_overrides_default(monkeypatch):
    # backend/.env can pin the mechanism (e.g. sdk for the fast in-process route)
    # so the gateway defaults to it without a UI switch. Precedence: override > env > default.
    monkeypatch.setenv("XGRAPH_LLM_MECHANISM", "sdk")
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "proj-x")
    cfg = llm.resolve_llm_config()
    assert cfg["mechanism"] == "sdk"
    assert cfg["sources"]["mechanism"] == "env"
    llm.validate_llm_config(cfg)  # sdk+vertex is a valid combo


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


def test_default_provider_is_anthropic():
    cfg = llm.resolve_llm_config()
    assert cfg["provider"] == "anthropic"
    assert cfg["sources"]["provider"] == "default"
    # backward-compat: anthropic tiers unchanged
    assert cfg["model"] == "claude-opus-4-8"
    assert cfg["fast_model"] == "claude-haiku-4-5-20251001"


def test_gemini_provider_defaults_and_forces_sdk():
    eff = llm.set_llm_config({"provider": "gemini", "auth": "apikey", "api_key": "AIza-x"})
    assert eff["provider"] == "gemini"
    assert eff["mechanism"] == "sdk"                  # gemini is SDK-only by default
    assert eff["model"] == "gemini-2.5-pro"
    assert eff["fast_model"] == "gemini-2.5-flash"
    assert llm.fast_model() == "gemini-2.5-flash"     # fast tier is provider-aware


def test_gemini_cli_route_rejected():
    with pytest.raises(ValueError):
        llm.set_llm_config({"provider": "gemini", "mechanism": "cli", "auth": "apikey", "api_key": "k"})


def test_unknown_provider_rejected():
    with pytest.raises(ValueError):
        llm.set_llm_config({"provider": "bogus", "auth": "apikey", "api_key": "k"})


def test_provider_env_and_override_precedence(monkeypatch):
    monkeypatch.setenv("XGRAPH_LLM_PROVIDER", "gemini")
    cfg = llm.resolve_llm_config()
    assert cfg["provider"] == "gemini"
    assert cfg["sources"]["provider"] == "env"
    # override beats env
    eff = llm.set_llm_config({"provider": "anthropic"})
    assert eff["provider"] == "anthropic"
    assert llm.resolve_llm_config()["sources"]["provider"] == "override"


def test_gemini_env_auth_inference(monkeypatch):
    monkeypatch.setenv("XGRAPH_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-env")
    cfg = llm.resolve_llm_config()
    assert cfg["auth"] == "apikey"
    assert cfg["api_key"] == "AIza-env"
    assert cfg["sources"]["auth"] == "env"


def test_gemini_vertex_requires_project():
    with pytest.raises(ValueError):
        llm.set_llm_config({"provider": "gemini", "auth": "vertex"})  # no project
    eff = llm.set_llm_config({"provider": "gemini", "auth": "vertex", "project": "proj-g", "region": "us-central1"})
    assert eff["auth"] == "vertex" and eff["project"] == "proj-g"


def test_fast_model_override_beats_provider_default():
    llm.set_llm_config({"provider": "gemini", "auth": "apikey", "api_key": "k", "fast_model": "gemini-2.5-flash-lite"})
    assert llm.fast_model() == "gemini-2.5-flash-lite"


def test_status_includes_provider_and_hides_key():
    llm.set_llm_config({"provider": "gemini", "auth": "apikey", "api_key": "AIza-secret"})
    st = llm.llm_status()
    assert st["provider"] == "gemini"
    assert st["has_api_key"] is True
    assert "api_key" not in st
