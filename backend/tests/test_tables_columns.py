import duckdb
import pytest
from fastapi.testclient import TestClient
from xgraph_gateway.app import create_app
from xgraph_gateway.adapters.fake import FakeAdapter
from xgraph_gateway.compute.duckdb_engine import DuckDBComputeEngine
from xgraph_gateway import config


def _client():
    return TestClient(create_app(adapter_factory=lambda e: FakeAdapter()))


# ── /tables + /columns via FakeAdapter (headless) ─────────────────────────

def test_tables_lists_relations():
    r = _client().get("/tables", params={"engine": "fake"})
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    names = [t["name"] for t in body]
    assert "expero.vertexes" in names
    assert all("type" in t for t in body)


def test_columns_lists_column_names():
    r = _client().get("/columns", params={"engine": "fake", "table": "expero.vertexes"})
    assert r.status_code == 200
    assert r.json() == ["NODE", "NODE_LABEL", "AMOUNT"]


def test_columns_unknown_table_returns_empty_list():
    r = _client().get("/columns", params={"engine": "fake", "table": "missing"})
    assert r.status_code == 200
    assert r.json() == []


def test_columns_requires_table_param():
    # FastAPI returns 422 when a required query param is absent.
    r = _client().get("/columns", params={"engine": "fake"})
    assert r.status_code == 422


def test_grammar_returns_builder_shape():
    r = _client().get("/grammar", params={"engine": "fake"})
    assert r.status_code == 200
    g = r.json()
    assert "NODES" in g and "EDGES" in g
    assert g["NODES"]["configurations"][0]["required"] == ["NODE"]
    assert "NODE_LABEL" in g["NODES"]["optional"]


# ── DuckDB / FalkorDB column introspection (embedded, no skip) ─────────────

def test_describe_relation_returns_columns(tmp_path):
    p = tmp_path / "v.parquet"
    con = duckdb.connect()
    con.execute(
        "COPY (SELECT 1 AS node, 'bank' AS node_label, 12.5 AS amount) "
        f"TO '{p}' (FORMAT PARQUET)"
    )
    con.close()
    cols = DuckDBComputeEngine().describe_relation(str(p))
    assert cols == ["node", "node_label", "amount"]


def test_describe_relation_missing_file_returns_empty():
    assert DuckDBComputeEngine().describe_relation("/no/such/file.parquet") == []


def test_falkordb_list_columns_reads_parquet(tmp_path):
    from xgraph_gateway.adapters.falkordb_adapter import FalkorDBAdapter
    p = tmp_path / "e.parquet"
    con = duckdb.connect()
    con.execute(f"COPY (SELECT 1 AS src, 2 AS dst) TO '{p}' (FORMAT PARQUET)")
    con.close()
    # Construct without connecting (list_columns doesn't touch FalkorDB).
    a = FalkorDBAdapter.__new__(FalkorDBAdapter)
    cols = a.list_columns(str(p))
    assert cols == ["src", "dst"]


def test_falkordb_list_tables_is_empty():
    from xgraph_gateway.adapters.falkordb_adapter import FalkorDBAdapter
    a = FalkorDBAdapter.__new__(FalkorDBAdapter)
    assert a.list_tables() == []


# ── Kinetica (live-skip) ──────────────────────────────────────────────────

def _kinetica_or_skip():
    try:
        from xgraph_gateway.adapters.kinetica_adapter import KineticaAdapter
        a = KineticaAdapter(config.load_settings())
        a.list_graphs()
        return a
    except Exception as e:
        pytest.skip(f"Kinetica unreachable: {e}")


def test_kinetica_list_tables_shape():
    a = _kinetica_or_skip()
    tables = a.list_tables()
    assert isinstance(tables, list)
    for t in tables:
        assert set(t.keys()) >= {"name", "type"}
    # Collections (schemas) are expanded into their child tables, never
    # surfaced themselves — the builder filters type=='collection' out.
    assert all(t["type"] != "collection" for t in tables)
    # Expanded children are fully qualified as schema.table.
    if tables:
        assert any("." in t["name"] for t in tables)


def test_kinetica_list_columns_of_first_table():
    a = _kinetica_or_skip()
    tables = [t for t in a.list_tables() if t["type"] == "table"]
    if not tables:
        pytest.skip("no base tables present")
    cols = a.list_columns(tables[0]["name"])
    assert isinstance(cols, list)


def test_kinetica_graph_grammar_shape():
    a = _kinetica_or_skip()
    g = a.graph_grammar()
    assert isinstance(g, dict)
    # Live grammar carries NODES/EDGES with multiple configurations each.
    assert "NODES" in g and "EDGES" in g
    assert len(g["NODES"]["configurations"]) >= 3
    for cfg in g["NODES"]["configurations"]:
        assert cfg["required"] and "label" in cfg
