import pytest
from fastapi.testclient import TestClient

from xgraph_gateway import config
from xgraph_gateway.adapters.base import GraphEngineAdapter
from xgraph_gateway.adapters.fake import FakeAdapter
from xgraph_gateway.adapters.kinetica_adapter import (
    KineticaAdapter,
    edge_table_name,
    node_table_name,
)
from xgraph_gateway.app import create_app


# ---------------------------------------------------------------------------
# GraphEngineAdapter.creation_statement -- concrete ABC default (FalkorDB,
# FakeAdapter, and any future adapter that doesn't override it, all inherit
# this: FalkorDB has no server-side creation DDL, built incrementally).
# ---------------------------------------------------------------------------

def test_abc_default_creation_statement_is_none():
    out = GraphEngineAdapter.creation_statement(FakeAdapter(), "g")
    assert out == {"statement": None, "source": None}

def test_fake_adapter_inherits_default_creation_statement():
    assert FakeAdapter().creation_statement("demo_graph") == {"statement": None, "source": None}


# ---------------------------------------------------------------------------
# Gateway: GET /graph_ddl routes to the resolved adapter's creation_statement.
# ---------------------------------------------------------------------------

def _client():
    return TestClient(create_app(adapter_factory=lambda e: FakeAdapter()))

def test_graph_ddl_endpoint_synthesizes_from_schema():
    # No live DDL + no recorded recipe → synthesized from the adapter's schema
    # (FakeAdapter.get_schema returns labels/rel_types), so "how it was built"
    # always shows something instead of a bare null.
    r = _client().get("/graph_ddl", params={"engine": "fake", "graph": "g"})
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "xgraph:schema-synthesized"
    assert body["statement"] and "bank" in body["statement"]

def test_graph_ddl_endpoint_error_returns_envelope():
    def boom(e):
        class A(FakeAdapter):
            def creation_statement(self, graph):
                raise ValueError("bad graph")
        return A()
    c = TestClient(create_app(adapter_factory=boom))
    r = c.get("/graph_ddl", params={"engine": "fake", "graph": "g"})
    assert r.status_code == 400
    assert r.json()["error"]["message"] == "bad graph"


# ---------------------------------------------------------------------------
# KineticaAdapter.creation_statement -- fake db, no live connection.
# ---------------------------------------------------------------------------

class _FakeShowGraphDb:
    def __init__(self, resp):
        self._resp = resp
    def show_graph(self, graph_name, options=None):
        return self._resp

def _adapter_with_fake_db(resp):
    adapter = KineticaAdapter.__new__(KineticaAdapter)
    adapter._db = _FakeShowGraphDb(resp)
    return adapter

def test_kinetica_creation_statement_parses_original_request():
    resp = {"original_request": [
        '{"statement": "CREATE OR REPLACE DIRECTED GRAPH \\"g\\" ( NODES => INPUT_TABLES((SELECT NODE FROM g_nodes)) )"}'
    ]}
    adapter = _adapter_with_fake_db(resp)
    out = adapter.creation_statement("g")
    assert out["source"] == "kinetica:show_graph"
    assert "CREATE OR REPLACE DIRECTED GRAPH" in out["statement"]

def test_kinetica_creation_statement_malformed_original_request_never_raises():
    adapter = _adapter_with_fake_db({"original_request": ["not json"]})
    out = adapter.creation_statement("g")
    assert out == {"statement": None, "source": "kinetica:show_graph"}

def test_kinetica_creation_statement_empty_original_request_never_raises():
    adapter = _adapter_with_fake_db({})
    out = adapter.creation_statement("g")
    assert out == {"statement": None, "source": "kinetica:show_graph"}

def test_kinetica_creation_statement_show_graph_raises_never_propagates():
    class _BoomDb:
        def show_graph(self, graph_name, options=None):
            raise RuntimeError("unreachable")
    adapter = KineticaAdapter.__new__(KineticaAdapter)
    adapter._db = _BoomDb()
    out = adapter.creation_statement("g")
    assert out == {"statement": None, "source": "kinetica:show_graph"}


# ---------------------------------------------------------------------------
# Live (SKIP if unreachable / missing).
# ---------------------------------------------------------------------------

_KIN_TEST_GRAPH = "ddl_test_kin"

def _drop_kinetica_test_graph(adapter):
    try:
        adapter._db.delete_graph(graph_name=_KIN_TEST_GRAPH)
    except Exception:
        pass
    for table in (node_table_name(_KIN_TEST_GRAPH), edge_table_name(_KIN_TEST_GRAPH)):
        try:
            adapter._db.execute_sql(f"DROP TABLE IF EXISTS {table}")
        except Exception:
            pass

def _kinetica_adapter_or_skip():
    s = config.load_settings()
    if not s.kinetica_url:
        pytest.skip("KINETICA_URL not set")
    try:
        a = KineticaAdapter(s)
        a.list_graphs()
        return a
    except Exception as e:
        pytest.skip(f"Kinetica unreachable: {e}")

@pytest.fixture
def live_kinetica_adapter():
    a = _kinetica_adapter_or_skip()
    _drop_kinetica_test_graph(a)
    yield a
    _drop_kinetica_test_graph(a)

def test_live_kinetica_creation_statement_contains_ddl(live_kinetica_adapter):
    nodes = [{"id": "n1", "label": "Person", "name": "Ada Lovelace", "attrs": {}}]
    live_kinetica_adapter.ingest_elements(_KIN_TEST_GRAPH, nodes, [])

    out = live_kinetica_adapter.creation_statement(_KIN_TEST_GRAPH)
    assert out["source"] == "kinetica:show_graph"
    assert out["statement"] is not None
    assert "CREATE OR REPLACE DIRECTED GRAPH" in out["statement"].upper()
    assert _KIN_TEST_GRAPH in out["statement"]

def test_live_kinetica_banking_graph_creation_statement_if_present(live_kinetica_adapter):
    graphs = live_kinetica_adapter.list_graphs()
    banking = next((g for g in graphs if "banking_graph" in g), None)
    if not banking:
        pytest.skip("expero.banking_graph not present")
    out = live_kinetica_adapter.creation_statement(banking)
    assert out["statement"]
