import pytest
from fastapi.testclient import TestClient
from xgraph_gateway.app import create_app
from xgraph_gateway.adapters.fake import FakeAdapter
from xgraph_gateway import llm


@pytest.fixture
def client(monkeypatch):
    llm._OVERRIDE = {}
    for k in ("CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_API_KEY", "ANTHROPIC_VERTEX_PROJECT_ID",
              "CLOUD_ML_REGION", "XGRAPH_LLM_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL"):
        monkeypatch.delenv(k, raising=False)
    yield TestClient(create_app(adapter_factory=lambda e: FakeAdapter()))
    llm._OVERRIDE = {}


def test_status_shape_and_no_key(client):
    r = client.get("/llm_status")
    assert r.status_code == 200
    body = r.json()
    assert set(["mechanism", "auth", "model", "has_api_key", "sources"]).issubset(body)
    assert "api_key" not in body


def test_config_roundtrip_vertex(client):
    r = client.post("/llm_config", json={"mechanism": "cli", "auth": "vertex",
                                         "project": "proj-x", "region": "global"})
    assert r.status_code == 200
    assert r.json()["auth"] == "vertex"
    # persists for the next status read (gateway-global)
    assert client.get("/llm_status").json()["project"] == "proj-x"


def test_config_apikey_not_echoed(client):
    r = client.post("/llm_config", json={"mechanism": "sdk", "auth": "apikey", "api_key": "sk-secret"})
    assert r.status_code == 200
    assert r.json()["has_api_key"] is True
    assert "sk-secret" not in r.text


def test_invalid_combo_400(client):
    r = client.post("/llm_config", json={"mechanism": "sdk", "auth": "cli-login"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "ValueError"


def test_apikey_without_key_400(client):
    r = client.post("/llm_config", json={"mechanism": "cli", "auth": "apikey"})
    assert r.status_code == 400
