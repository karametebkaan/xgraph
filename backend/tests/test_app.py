from fastapi.testclient import TestClient
from xgraph_gateway.app import create_app
from xgraph_gateway.adapters.fake import FakeAdapter

def _client():
    return TestClient(create_app(adapter_factory=lambda e: FakeAdapter()))

def test_graphs_endpoint():
    r = _client().get("/graphs", params={"engine": "fake"})
    assert r.status_code == 200
    assert r.json() == ["demo_graph"]

def test_query_endpoint():
    r = _client().post("/query", json={"engine": "fake", "graph": "demo_graph",
                                       "cypher": "MATCH (n) RETURN n.NODE AS NODE"})
    assert r.status_code == 200
    body = r.json()
    assert body["columns"] == ["NODE"]
    assert ["b1"] in body["rows"]

def test_schema_endpoint():
    r = _client().get("/schema", params={"engine": "fake", "graph": "demo_graph"})
    assert r.status_code == 200
    assert "bank" in r.json()["labels"]

def test_bad_query_returns_error_envelope():
    def boom(e):
        class A(FakeAdapter):
            def run_query(self, *a, **k): raise ValueError("bad cypher")
        return A()
    c = TestClient(create_app(adapter_factory=boom))
    r = c.post("/query", json={"engine": "fake", "graph": "g", "cypher": "x"})
    assert r.status_code == 400
    assert r.json()["error"]["message"] == "bad cypher"
