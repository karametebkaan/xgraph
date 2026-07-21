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
