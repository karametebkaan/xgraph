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

def test_promote_columns_happy_path():
    r = _client().post("/promote_columns", json={
        "engine": "fake", "graph": "g1", "source": "vertexes.parquet",
        "key": "NODE", "columns": ["party:party_name", "amount"]})
    assert r.status_code == 200
    body = r.json()
    assert body["promoted"] == ["party:party_name", "amount"]
    assert body["source"] == "vertexes.parquet"
    assert body["key"] == "NODE"

def test_promote_columns_empty_columns_is_400():
    r = _client().post("/promote_columns", json={
        "engine": "fake", "graph": "g1", "source": "vertexes.parquet", "columns": []})
    assert r.status_code == 400
    assert r.json()["error"]["code"]  # uniform error envelope

def test_ask_fallback_when_not_answered(monkeypatch):
    from xgraph_gateway import nlcypher
    monkeypatch.setattr(nlcypher, "generate_cypher", lambda *a, **k: "MATCH (n) RETURN n LIMIT 1")
    monkeypatch.setattr(nlcypher, "validate_cypher", lambda *a, **k: (True, ""))
    def fake_synthesize(question, cols, rows_, llm=None, cypher=None, return_meta=False):
        if return_meta:
            return {"answer": "no age here", "answered_from_results": False}
        return "no age here"
    monkeypatch.setattr(nlcypher, "synthesize", fake_synthesize)
    monkeypatch.setattr(nlcypher, "general_knowledge_answer", lambda q, llm=None: "70 years old.")
    c = _client()  # FakeAdapter.run_query returns rows
    r = c.post("/ask", json={"engine": "fake", "graph": "g", "question": "how old is X?"})
    body = r.json()
    assert body["answered_from_results"] is False
    assert body["fallback_answer"] == "70 years old."


def test_bad_query_returns_error_envelope():
    def boom(e):
        class A(FakeAdapter):
            def run_query(self, *a, **k): raise ValueError("bad cypher")
        return A()
    c = TestClient(create_app(adapter_factory=boom))
    r = c.post("/query", json={"engine": "fake", "graph": "g", "cypher": "x"})
    assert r.status_code == 400
    assert r.json()["error"]["message"] == "bad cypher"
