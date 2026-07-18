from fastapi.testclient import TestClient
from xgraph_gateway.app import create_app
from xgraph_gateway.adapters.fake import FakeAdapter
from xgraph_gateway.sessions import SessionStore

class _FakeCompute:
    def __init__(self, tag="fake-compute"):
        self.tag = tag
    def hydrate(self, rows, source, key="NODE", columns="*"):
        return [{"used": self.tag, "source": source}]
    def run_sql(self, sql):
        return [{"used": self.tag, "sql": sql}]

def _adapter_factory(engine, conn=None):
    return FakeAdapter()

def _compute_factory(engine, conn=None):
    return _FakeCompute(tag=f"{engine}:{conn}")

def _client():
    store = SessionStore(adapter_factory=_adapter_factory, compute_factory=_compute_factory)
    return TestClient(create_app(adapter_factory=lambda e: FakeAdapter(), store=store))

def test_connect_returns_session_and_graphs():
    r = _client().post("/connect", json={
        "graph": {"engine": "fake", "conn": {"host": "h"}},
        "compute": {"engine": "duckdb", "conn": None},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["session"] == "s1"
    assert body["graphs"] == ["demo_graph"]

def test_query_routes_to_session_adapter():
    c = _client()
    session = c.post("/connect", json={
        "graph": {"engine": "fake", "conn": None},
        "compute": {"engine": "duckdb", "conn": None},
    }).json()["session"]
    r = c.post("/query", json={"session": session, "graph": "demo_graph",
                                "cypher": "MATCH (n) RETURN n.NODE AS NODE"})
    assert r.status_code == 200
    body = r.json()
    assert body["columns"] == ["NODE"]
    assert ["b1"] in body["rows"]

def test_hydrate_routes_to_session_compute():
    c = _client()
    session = c.post("/connect", json={
        "graph": {"engine": "fake", "conn": None},
        "compute": {"engine": "duckdb", "conn": "cx"},
    }).json()["session"]
    r = c.post("/hydrate", json={"session": session, "rows": [{"NODE": "b1"}],
                                 "source": "some.parquet"})
    assert r.status_code == 200
    body = r.json()
    assert body[0]["used"] == "duckdb:cx"
    assert body[0]["source"] == "some.parquet"

def test_backcompat_graphs_endpoint_no_session():
    r = _client().get("/graphs", params={"engine": "fake"})
    assert r.status_code == 200
    assert r.json() == ["demo_graph"]

def test_backcompat_query_endpoint_no_session():
    r = _client().post("/query", json={"engine": "fake", "graph": "demo_graph",
                                       "cypher": "MATCH (n) RETURN n.NODE AS NODE"})
    assert r.status_code == 200
    body = r.json()
    assert body["columns"] == ["NODE"]
    assert ["b1"] in body["rows"]

def test_unknown_session_falls_back_to_engine():
    # A stale session (e.g. after a gateway restart cleared the in-memory store)
    # degrades gracefully to the request's engine instead of hard-failing.
    r = _client().get("/graphs", params={"session": "s999", "engine": "fake"})
    assert r.status_code == 200
    assert r.json() == ["demo_graph"]

def test_unknown_session_on_query_falls_back_to_engine():
    c = _client()
    r = c.post("/query", json={"session": "s999", "engine": "fake", "graph": "g", "cypher": "x"})
    assert r.status_code == 200
    assert "columns" in r.json()  # ran on the fallback FakeAdapter
