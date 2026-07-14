import pytest
from fastapi.testclient import TestClient
from xgraph_gateway.app import create_app
from xgraph_gateway.adapters.fake import FakeAdapter
from xgraph_gateway import config

def test_fake_adapter_offset_returns_second_element():
    a = FakeAdapter()
    page0 = a.fetch_entities("demo_graph", 1, offset=0)
    page1 = a.fetch_entities("demo_graph", 1, offset=1)
    assert page0["nodes"] != page1["nodes"]
    assert len(page1["nodes"]) == 1
    assert page1["nodes"][0]["id"] == "w1"

def test_fake_adapter_offset_defaults_to_zero():
    a = FakeAdapter()
    assert a.fetch_entities("demo_graph", 1) == a.fetch_entities("demo_graph", 1, offset=0)

def _client():
    return TestClient(create_app(adapter_factory=lambda e: FakeAdapter()))

def test_entities_endpoint_paginates_with_offset():
    c = _client()
    r = c.get("/entities", params={"engine": "fake", "graph": "g", "limit": 1, "offset": 1})
    assert r.status_code == 200
    body = r.json()
    assert len(body["nodes"]) == 1
    assert body["nodes"][0]["id"] == "w1"

def test_entities_endpoint_offset_defaults_to_zero():
    c = _client()
    r = c.get("/entities", params={"engine": "fake", "graph": "g", "limit": 1})
    assert r.status_code == 200
    assert r.json()["nodes"][0]["id"] == "b1"

def _falkordb_or_skip():
    from xgraph_gateway.adapters.falkordb_adapter import FalkorDBAdapter
    try:
        a = FalkorDBAdapter(config.load_settings())
        a.list_graphs()
        return a
    except Exception as e:
        pytest.skip(f"FalkorDB unreachable: {e}")

def test_live_falkordb_fetch_entities_pages_advance():
    a = _falkordb_or_skip()
    if "banking_graph" not in a.list_graphs():
        pytest.skip("banking_graph not loaded")
    page0 = a.fetch_entities("banking_graph", 5, 0)
    page1 = a.fetch_entities("banking_graph", 5, 5)
    assert len(page0["nodes"]) == 5
    assert len(page1["nodes"]) == 5
    ids0 = {n["id"] for n in page0["nodes"]}
    ids1 = {n["id"] for n in page1["nodes"]}
    assert ids0 != ids1
