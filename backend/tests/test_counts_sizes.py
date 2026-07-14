import pytest
from fastapi.testclient import TestClient
from xgraph_gateway.app import create_app
from xgraph_gateway.adapters.fake import FakeAdapter
from xgraph_gateway.adapters.kinetica_adapter import _counts_from_show_graph
from xgraph_gateway import config
from xgraph_gateway.adapters.kinetica_adapter import KineticaAdapter

# ---------------------------------------------------------------------------
# _counts_from_show_graph -- pure, unit-testable (canned labeljson response).
# ---------------------------------------------------------------------------

_CANNED_LABELJSON = (
    '{"node_labels": ['
    '{"labels": ["bank"], "count": 2130},'
    '{"labels": ["wire_message"], "count": 56800}'
    '], "edge_labels": ['
    '{"labels": ["performed"], "count": 113600},'
    '{"labels": ["manages"], "count": 40470}'
    ']}'
)

def test_counts_from_show_graph_sums_label_counts():
    resp = {"info": {"labeljson": _CANNED_LABELJSON}}
    assert _counts_from_show_graph(resp) == {"nodes": 58930, "edges": 154070}

def test_counts_from_show_graph_missing_labeljson_returns_zeros():
    assert _counts_from_show_graph({"info": {}}) == {"nodes": 0, "edges": 0}
    assert _counts_from_show_graph({}) == {"nodes": 0, "edges": 0}

def test_counts_from_show_graph_unparseable_labeljson_returns_zeros():
    resp = {"info": {"labeljson": "not json"}}
    assert _counts_from_show_graph(resp) == {"nodes": 0, "edges": 0}

# ---------------------------------------------------------------------------
# get_schema gains "counts" -- fake adapter, no live call.
# ---------------------------------------------------------------------------

def test_fake_get_schema_includes_counts():
    sch = FakeAdapter().get_schema("demo_graph")
    assert sch["counts"] == {"nodes": 2, "edges": 1}

def test_kinetica_get_schema_includes_counts_from_capturing_db():
    class _CapturingDb:
        def show_graph(self, graph_name="", options=None):
            return {"info": {"dot": "digraph {}", "labeljson": _CANNED_LABELJSON}}
    adapter = KineticaAdapter.__new__(KineticaAdapter)
    adapter._db = _CapturingDb()
    sch = adapter.get_schema("expero.banking_graph")
    assert sch["counts"] == {"nodes": 58930, "edges": 154070}

# ---------------------------------------------------------------------------
# graph_sizes -- fake adapter and gateway endpoint.
# ---------------------------------------------------------------------------

def test_fake_graph_sizes():
    assert FakeAdapter().graph_sizes() == {"demo_graph": {"nodes": 2, "edges": 1}}

def test_kinetica_graph_sizes_zips_parallel_lists():
    class _CapturingDb:
        def show_graph(self, graph_name=""):
            return {"graph_names": ["expero.banking_graph", "kgr.kg"],
                    "num_nodes": [622032, 1139], "num_edges": [845752, 1464]}
    adapter = KineticaAdapter.__new__(KineticaAdapter)
    adapter._db = _CapturingDb()
    assert adapter.graph_sizes() == {
        "expero.banking_graph": {"nodes": 622032, "edges": 845752},
        "kgr.kg": {"nodes": 1139, "edges": 1464},
    }

def test_kinetica_graph_sizes_show_graph_failure_returns_empty_dict():
    class _BoomDb:
        def show_graph(self, graph_name=""):
            raise RuntimeError("network down")
    adapter = KineticaAdapter.__new__(KineticaAdapter)
    adapter._db = _BoomDb()
    assert adapter.graph_sizes() == {}

def _client():
    return TestClient(create_app(adapter_factory=lambda e: FakeAdapter()))

def test_graph_sizes_endpoint():
    r = _client().get("/graph_sizes", params={"engine": "fake"})
    assert r.status_code == 200
    assert r.json() == {"demo_graph": {"nodes": 2, "edges": 1}}

def test_graph_sizes_endpoint_error_envelope():
    def boom(e):
        class A(FakeAdapter):
            def graph_sizes(self): raise ValueError("bad sizes")
        return A()
    c = TestClient(create_app(adapter_factory=boom))
    r = c.get("/graph_sizes", params={"engine": "fake"})
    assert r.status_code == 400
    assert r.json()["error"]["message"] == "bad sizes"

# ---------------------------------------------------------------------------
# Live (skip if Kinetica unreachable).
# ---------------------------------------------------------------------------

def _live_kinetica_or_skip():
    s = config.load_settings()
    if not s.kinetica_url:
        pytest.skip("KINETICA_URL not set")
    try:
        a = KineticaAdapter(s)
        a.list_graphs()
        return a
    except Exception as e:
        pytest.skip(f"Kinetica unreachable: {e}")

def test_live_kinetica_get_schema_counts_nodes_positive():
    a = _live_kinetica_or_skip()
    sch = a.get_schema("expero.banking_graph")
    assert sch["counts"]["nodes"] > 0

def test_live_kinetica_graph_sizes_has_banking_graph():
    a = _live_kinetica_or_skip()
    sizes = a.graph_sizes()
    assert isinstance(sizes, dict)
    assert "expero.banking_graph" in sizes
    assert sizes["expero.banking_graph"]["nodes"] > 0
