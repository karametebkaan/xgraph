import json
import os

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
# GraphEngineAdapter.storage -- concrete ABC default (FalkorDB, FakeAdapter,
# and any future adapter that doesn't override it, all inherit this).
# ---------------------------------------------------------------------------

def test_abc_default_storage_is_graph_store_note():
    out = GraphEngineAdapter.storage(FakeAdapter(), "g")
    assert out == {
        "kind": "graph-store",
        "note": "This engine stores the graph itself — inspect it via Visualize / Ontology / Query.",
        "tables": [],
    }

def test_fake_adapter_inherits_default_storage():
    assert FakeAdapter().storage("demo_graph")["kind"] == "graph-store"
    assert FakeAdapter().storage("demo_graph")["tables"] == []


# ---------------------------------------------------------------------------
# Gateway: GET /storage routes to the resolved adapter's storage.
# ---------------------------------------------------------------------------

def _client():
    return TestClient(create_app(adapter_factory=lambda e: FakeAdapter()))

def test_storage_endpoint_returns_default_note():
    r = _client().get("/storage", params={"engine": "fake", "graph": "g"})
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "graph-store"
    assert body["tables"] == []
    assert "note" in body

def test_storage_endpoint_error_returns_envelope():
    def boom(e):
        class A(FakeAdapter):
            def storage(self, graph): raise ValueError("bad graph")
        return A()
    c = TestClient(create_app(adapter_factory=boom))
    r = c.get("/storage", params={"engine": "fake", "graph": "g"})
    assert r.status_code == 400
    assert r.json()["error"]["message"] == "bad graph"


# ---------------------------------------------------------------------------
# KineticaAdapter.storage -- fake db/source, no live connection.
# ---------------------------------------------------------------------------

class _FakeShowTableDb:
    """Fakes show_table for `_current_columns` -- keyed by table name so a
    missing table (no entry) reports no columns, mirroring a real
    `no_error_if_not_exists` response for a table that was never created."""
    def __init__(self, tables_cols):
        self._tables_cols = tables_cols  # {table_name: [col, ...]}

    def show_table(self, table_name, options=None):
        cols = self._tables_cols.get(table_name)
        if cols is None:
            return {"table_names": []}
        return {
            "table_names": [table_name],
            "type_schemas": [json.dumps({"fields": [{"name": c} for c in cols]})],
        }

class _FakeSource:
    """Fakes KineticaSource.rows(sql) -- returns canned rows for a table
    regardless of the exact SQL text (storage() only ever issues a bounded
    `SELECT * FROM <table> LIMIT 25`)."""
    def __init__(self, tables_rows):
        self._tables_rows = tables_rows  # {table_name: [ {col: val}, ... ]}

    def rows(self, sql):
        for table, rows in self._tables_rows.items():
            if table in sql:
                return iter(rows)
        return iter([])

def _adapter_with_fakes(tables_cols, tables_rows):
    adapter = KineticaAdapter.__new__(KineticaAdapter)
    adapter._db = _FakeShowTableDb(tables_cols)
    adapter._src = _FakeSource(tables_rows)
    return adapter

def test_kinetica_storage_returns_both_tables_with_columns_and_rows():
    node_table = node_table_name("g")
    edge_table = edge_table_name("g")
    tables_cols = {
        node_table: ["NODE", "LABEL", "name"],
        edge_table: ["edge_key", "NODE1", "NODE2", "LABEL"],
    }
    tables_rows = {
        node_table: [{"NODE": "n1", "LABEL": "Person", "name": "Ada"}],
        edge_table: [{"edge_key": "e1", "NODE1": "n1", "NODE2": "n2", "LABEL": "knows"}],
    }
    adapter = _adapter_with_fakes(tables_cols, tables_rows)
    out = adapter.storage("g")
    assert out["kind"] == "kinetica"
    assert len(out["tables"]) == 2
    node_entry = next(t for t in out["tables"] if t["name"] == node_table)
    assert node_entry["columns"] == ["NODE", "LABEL", "name"]
    assert node_entry["rows"] == [["n1", "Person", "Ada"]]
    edge_entry = next(t for t in out["tables"] if t["name"] == edge_table)
    assert edge_entry["columns"] == ["edge_key", "NODE1", "NODE2", "LABEL"]
    assert edge_entry["rows"] == [["e1", "n1", "n2", "knows"]]

def test_kinetica_storage_no_backing_tables_returns_empty_with_note():
    adapter = _adapter_with_fakes({}, {})
    out = adapter.storage("expero.banking_graph")
    assert out == {"kind": "kinetica", "tables": [],
                   "note": "No extract backing tables for this graph."}

def test_kinetica_storage_never_raises_on_bad_graph_name():
    adapter = _adapter_with_fakes({}, {})
    out = adapter.storage("")
    assert out["kind"] == "kinetica"
    assert out["tables"] == []

def test_kinetica_storage_one_table_missing_still_returns_the_other():
    node_table = node_table_name("g")
    edge_table = edge_table_name("g")
    tables_cols = {node_table: ["NODE", "LABEL", "name"]}  # edges table absent
    tables_rows = {node_table: [{"NODE": "n1", "LABEL": "Person", "name": "Ada"}]}
    adapter = _adapter_with_fakes(tables_cols, tables_rows)
    out = adapter.storage("g")
    assert len(out["tables"]) == 1
    assert out["tables"][0]["name"] == node_table


# ---------------------------------------------------------------------------
# DuckDBComputeEngine.preview_source + GET /source_preview.
# ---------------------------------------------------------------------------

from xgraph_gateway.compute.duckdb_engine import DuckDBComputeEngine

def test_preview_source_returns_columns_and_rows(tmp_path):
    import duckdb
    p = tmp_path / "v.parquet"
    con = duckdb.connect()
    con.execute("""CREATE TABLE t AS SELECT * FROM (VALUES
        ('b1', 10.5), ('b2', 3.0)) AS v(NODE, amount)""")
    con.execute(f"COPY t TO '{p}' (FORMAT parquet)")
    con.close()

    eng = DuckDBComputeEngine()
    out = eng.preview_source(str(p))
    assert out["columns"] == ["NODE", "amount"]
    assert out["rows"] == [["b1", 10.5], ["b2", 3.0]]

def test_preview_source_rejects_quote_in_path():
    eng = DuckDBComputeEngine()
    with pytest.raises(ValueError):
        eng.preview_source("evil'; DROP TABLE x; --")

def test_source_preview_endpoint(tmp_path):
    import duckdb
    p = tmp_path / "v.parquet"
    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT * FROM (VALUES ('n1')) AS v(NODE)")
    con.execute(f"COPY t TO '{p}' (FORMAT parquet)")
    con.close()

    r = _client().get("/source_preview", params={"source": str(p)})
    assert r.status_code == 200
    body = r.json()
    assert body["columns"] == ["NODE"]
    assert body["rows"] == [["n1"]]


# ---------------------------------------------------------------------------
# Live (SKIP if unreachable / missing).
# ---------------------------------------------------------------------------

_KIN_TEST_GRAPH = "storage_test_kin"

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

def test_live_kinetica_storage_returns_extract_tables(live_kinetica_adapter):
    nodes = [{"id": "n1", "label": "Person", "name": "Ada Lovelace", "attrs": {}}]
    live_kinetica_adapter.ingest_elements(_KIN_TEST_GRAPH, nodes, [])

    out = live_kinetica_adapter.storage(_KIN_TEST_GRAPH)
    assert out["kind"] == "kinetica"
    assert len(out["tables"]) == 2
    node_table = next(t for t in out["tables"] if t["name"] == node_table_name(_KIN_TEST_GRAPH))
    assert "NODE" in node_table["columns"]
    assert "LABEL" in node_table["columns"]
    assert len(node_table["rows"]) <= 25

def test_live_source_preview_vertexes_parquet():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    data_path = os.path.join(repo_root, "data", "vertexes.parquet")
    if not os.path.exists(data_path):
        pytest.skip("vertexes.parquet not present")
    eng = DuckDBComputeEngine()
    out = eng.preview_source("vertexes.parquet")
    assert "NODE" in out["columns"]
    assert len(out["rows"]) <= 25
