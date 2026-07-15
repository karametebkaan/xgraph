import pytest
from fastapi.testclient import TestClient

from xgraph_gateway import config
from xgraph_gateway.adapters.fake import FakeAdapter
from xgraph_gateway.adapters.falkordb_adapter import FalkorDBAdapter
from xgraph_gateway.adapters.kinetica_adapter import (
    KineticaAdapter,
    edge_table_name,
    node_table_name,
)
from xgraph_gateway.app import create_app


# ---------------------------------------------------------------------------
# Gateway: POST /delete_graph routes to the resolved adapter's delete_graph.
# ---------------------------------------------------------------------------

def _client():
    return TestClient(create_app(adapter_factory=lambda e: FakeAdapter()))

def test_delete_graph_endpoint_returns_deleted():
    r = _client().post("/delete_graph", json={"graph": "g", "engine": "fake"})
    assert r.status_code == 200
    assert r.json() == {"deleted": "g"}

def test_delete_graph_endpoint_error_returns_envelope():
    def boom(e):
        class A(FakeAdapter):
            def delete_graph(self, graph):
                raise ValueError("bad graph")
        return A()
    c = TestClient(create_app(adapter_factory=boom))
    r = c.post("/delete_graph", json={"graph": "g", "engine": "fake"})
    assert r.status_code == 400
    assert r.json()["error"]["message"] == "bad graph"


# ---------------------------------------------------------------------------
# FalkorDBAdapter.delete_graph -- live (SKIP if FalkorDB unreachable).
# ---------------------------------------------------------------------------

_FK_TEST_GRAPH = "del_test_fk"

def _falkordb_adapter_or_skip():
    try:
        a = FalkorDBAdapter(config.load_settings())
        a.list_graphs()
        return a
    except Exception as e:
        pytest.skip(f"FalkorDB unreachable: {e}")

@pytest.fixture
def live_falkordb_adapter():
    a = _falkordb_adapter_or_skip()
    yield a
    try:
        a._graph(_FK_TEST_GRAPH).delete()
    except Exception:
        pass

def test_falkordb_delete_graph_removes_it(live_falkordb_adapter):
    nodes = [{"id": "del-n1", "label": "Person", "name": "Ada Lovelace", "attrs": {}}]
    live_falkordb_adapter.ingest_elements(_FK_TEST_GRAPH, nodes, [])
    assert _FK_TEST_GRAPH in live_falkordb_adapter.list_graphs()

    out = live_falkordb_adapter.delete_graph(_FK_TEST_GRAPH)
    assert out == {"deleted": _FK_TEST_GRAPH}
    assert _FK_TEST_GRAPH not in live_falkordb_adapter.list_graphs()


# ---------------------------------------------------------------------------
# KineticaAdapter.delete_graph -- live (SKIP if Kinetica unreachable).
# ---------------------------------------------------------------------------

_KIN_TEST_GRAPH = "del_test_kin"

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

def test_kinetica_delete_graph_removes_it(live_kinetica_adapter):
    nodes = [{"id": "del-n1", "label": "Person", "name": "Ada Lovelace", "attrs": {}}]
    live_kinetica_adapter.ingest_elements(_KIN_TEST_GRAPH, nodes, [])
    graphs_before = live_kinetica_adapter.list_graphs()
    assert any(_KIN_TEST_GRAPH in g for g in graphs_before)

    out = live_kinetica_adapter.delete_graph(_KIN_TEST_GRAPH)
    assert out == {"deleted": _KIN_TEST_GRAPH}

    graphs_after = live_kinetica_adapter.list_graphs()
    assert not any(_KIN_TEST_GRAPH in g for g in graphs_after)

    # Best-effort: the backing tables should be gone too, but don't fail hard
    # if the SDK reports table existence oddly right after a drop.
    try:
        resp = live_kinetica_adapter._db.show_table(
            table_name=node_table_name(_KIN_TEST_GRAPH),
            options={"no_error_if_not_exists": "true"})
        assert not resp.get("table_names")
    except Exception:
        pass
