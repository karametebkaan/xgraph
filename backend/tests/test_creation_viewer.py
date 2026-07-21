from xgraph_gateway.compute.duckdb_engine import DuckDBComputeEngine


def _engine(tmp_path):
    return DuckDBComputeEngine(meta_path=str(tmp_path / "meta.duckdb"))


# ── Task 1: xgraph_creations meta ledger ──────────────────────────────────

def test_record_and_get_creation(tmp_path):
    eng = _engine(tmp_path)
    assert eng.get_creation("g1") is None
    eng.record_creation("g1", "falkordb", "-- recipe text", "create")
    row = eng.get_creation("g1")
    assert row["graph"] == "g1"
    assert row["engine"] == "falkordb"
    assert row["statement"] == "-- recipe text"
    assert row["source"] == "create"
    assert row["ts"]  # ISO string present


def test_record_creation_upserts(tmp_path):
    eng = _engine(tmp_path)
    eng.record_creation("g1", "kinetica", "CREATE GRAPH g1 (...);", "create")
    eng.record_creation("g1", "kinetica", "CREATE OR REPLACE GRAPH g1 (...);", "create")
    row = eng.get_creation("g1")
    assert row["statement"] == "CREATE OR REPLACE GRAPH g1 (...);"  # latest wins


def test_clear_graph_metadata_drops_creation(tmp_path):
    eng = _engine(tmp_path)
    eng.record_creation("g1", "falkordb", "x", "create")
    eng.clear_graph_metadata("g1")
    assert eng.get_creation("g1") is None


# ── Task 2: render_create_recipe + /create recording + /graph_ddl fallback ─

from fastapi.testclient import TestClient
from xgraph_gateway.app import create_app, render_create_recipe
from xgraph_gateway.sessions import SessionStore
from xgraph_gateway.adapters.fake import FakeAdapter


def test_render_create_recipe_ddl_passthrough():
    assert render_create_recipe({"graph": "g", "ddl": "CREATE GRAPH g (...);"}) == "CREATE GRAPH g (...);"


def test_render_create_recipe_falkordb_spec():
    spec = {"graph": "banking", "tables": {"b2_nodes": "vertexes.parquet", "b2_edges": "edges.parquet"},
            "nodes": [{"sql": "SELECT id AS NODE FROM b2_nodes", "id": "NODE"}],
            "edges": [{"sql": "SELECT src AS SRC, dst AS DST FROM b2_edges", "source_key": "SRC", "target_key": "DST"}]}
    out = render_create_recipe(spec)
    assert "banking" in out
    assert "SELECT id AS NODE FROM b2_nodes" in out
    assert "SELECT src AS SRC, dst AS DST FROM b2_edges" in out


def _app(tmp_path):
    from xgraph_gateway.compute.duckdb_engine import DuckDBComputeEngine
    store = SessionStore(adapter_factory=lambda e, c=None: FakeAdapter(),
                        compute_factory=lambda e, c=None: DuckDBComputeEngine(meta_path=str(tmp_path / "m.duckdb")))
    return TestClient(create_app(adapter_factory=lambda e: FakeAdapter(),
                                compute=DuckDBComputeEngine(meta_path=str(tmp_path / "m2.duckdb")),
                                store=store))


def test_create_records_recipe_and_graph_ddl_returns_it(tmp_path):
    client = _app(tmp_path)
    sid = client.post("/connect", json={"graph": {"engine": "falkordb"}, "compute": {"engine": "duckdb"}}).json()["session"]
    spec = {"graph": "g1", "tables": {"t": "v.parquet"},
            "nodes": [{"sql": "SELECT id AS NODE FROM t", "id": "NODE"}], "edges": []}
    r = client.post("/create", json={"session": sid, "spec": spec})
    assert r.status_code == 200
    ddl = client.get("/graph_ddl", params={"session": sid, "graph": "g1"}).json()
    assert ddl["statement"] and "SELECT id AS NODE FROM t" in ddl["statement"]
    assert ddl["source"] == "xgraph:create-ledger"


def test_graph_ddl_synthesizes_when_unrecorded(tmp_path):
    # No recorded recipe + no live DDL (FakeAdapter) → synthesized from schema.
    client = _app(tmp_path)
    sid = client.post("/connect", json={"graph": {"engine": "falkordb"}, "compute": {"engine": "duckdb"}}).json()["session"]
    ddl = client.get("/graph_ddl", params={"session": sid, "graph": "never_built"}).json()
    assert ddl["source"] == "xgraph:schema-synthesized"
    assert ddl["statement"] and "synthesized from live schema" in ddl["statement"]
    # carries the live labels / rel types from get_schema
    assert "bank" in ddl["statement"] and "performed" in ddl["statement"]


def test_synthesize_recipe_unit():
    from xgraph_gateway.app import synthesize_recipe
    out = synthesize_recipe("g1", "falkordb",
                            {"labels": ["bank", "party"], "rel_types": ["performed"],
                             "counts": {"nodes": 2, "edges": 1}})
    assert "g1" in out and "falkordb" in out
    assert "bank" in out and "party" in out
    assert "performed" in out
    assert "nodes=2" in out and "edges=1" in out
    assert synthesize_recipe("g", "falkordb", None) == ""
