import os
import pytest
from xgraph_gateway.adapters import kinetica_adapter as ka
from xgraph_gateway import config


# ── Task 1: pure load_data_sql builder + helpers ──────────────────────────

def test_load_data_sql_remote_with_data_source():
    sql = ka.load_data_sql("myschema.airports", "s3://bkt/a.parquet", "parquet", "my_s3")
    assert "LOAD DATA INTO myschema.airports" in sql
    assert "FROM FILE PATHS 's3://bkt/a.parquet'" in sql
    assert "FORMAT PARQUET" in sql
    assert "WITH OPTIONS (DATA SOURCE = 'my_s3')" in sql
    assert sql.rstrip().endswith(";")


def test_load_data_sql_local_no_data_source():
    sql = ka.load_data_sql("t", "kifs://u/a.csv", "csv", None)
    assert "FROM FILE PATHS 'kifs://u/a.csv'" in sql
    assert "FORMAT CSV" in sql
    assert "DATA SOURCE" not in sql


def test_load_data_sql_rejects_bad_format():
    with pytest.raises(ValueError):
        ka.load_data_sql("t", "a.parquet", "exe", None)


def test_load_data_sql_rejects_bad_table_ident():
    with pytest.raises(Exception):  # MappingError from safe_ident
        ka.load_data_sql("bad name!", "a.parquet", "parquet", None)


def test_load_data_sql_escapes_quotes_in_path():
    sql = ka.load_data_sql("t", "s3://b/o'x.parquet", "parquet", None)
    assert "'s3://b/o''x.parquet'" in sql  # doubled quote


def test_detect_format():
    assert ka._detect_format("/x/a.CSV") == "csv"
    assert ka._detect_format("s3://b/a.parquet?x=1") == "parquet"
    assert ka._detect_format("a.jsonl") == "json"
    assert ka._detect_format("a.unknown") == "parquet"


def test_derive_table_name():
    assert ka._derive_table_name("s3://b/My File.parquet") == "My_File"
    assert ka._derive_table_name("/p/2020data.csv") == "t_2020data"


def test_base_register_file_not_implemented():
    from xgraph_gateway.adapters.base import GraphEngineAdapter
    class A(GraphEngineAdapter):
        def list_graphs(self): return []
        def get_schema(self, g, options=None): return {}
        def run_query(self, g, c, timeout=60000): return {}
        def fetch_entities(self, g, limit, offset=0): return {}
        def get_record(self, g, i): return {}
        def load_graph(self, spec): return {}
        def graph_sizes(self): return {}
    with pytest.raises(NotImplementedError):
        A().register_file("a.parquet")


# ── Task 2: KineticaAdapter.register_file (live-skip) ─────────────────────

def _kinetica_or_skip():
    try:
        from xgraph_gateway.adapters.kinetica_adapter import KineticaAdapter
        a = KineticaAdapter(config.load_settings())
        a.list_graphs()
        return a
    except Exception as e:
        pytest.skip(f"Kinetica unreachable: {e}")


def test_kinetica_register_file_live():
    # Requires a file Kinetica can read. Configure via env:
    #   XG_TEST_LOAD_PATH   (e.g. kifs://… or s3://…)  [required]
    #   XG_TEST_DATA_SOURCE (name)                     [optional, for remote]
    path = os.environ.get("XG_TEST_LOAD_PATH")
    if not path:
        pytest.skip("set XG_TEST_LOAD_PATH to run the live LOAD DATA test")
    a = _kinetica_or_skip()
    tbl = "xg_test_import_tmp"
    try:
        res = a.register_file(path, table=tbl,
                              data_source=os.environ.get("XG_TEST_DATA_SOURCE"))
        assert res["name"] == tbl and res["type"] == "table"
        assert isinstance(res["columns"], list) and res["columns"]
        assert any(t["name"] == tbl or t["name"].endswith("." + tbl) for t in a.list_tables())
    finally:
        try:
            a._execute_ddl(f"DROP TABLE IF EXISTS {tbl};")
        except Exception:
            pass


# ── Task 3: /register_file routing (Fake, headless) ───────────────────────

from fastapi.testclient import TestClient
from xgraph_gateway.app import create_app
from xgraph_gateway.sessions import SessionStore
from xgraph_gateway.adapters.fake import FakeAdapter
from xgraph_gateway.compute.duckdb_engine import DuckDBComputeEngine


def test_register_file_routes_kinetica_to_adapter(tmp_path):
    store = SessionStore(
        adapter_factory=lambda e, c=None: FakeAdapter(),
        compute_factory=lambda e, c=None: DuckDBComputeEngine(meta_path=str(tmp_path / "m.duckdb")))
    client = TestClient(create_app(
        adapter_factory=lambda e: FakeAdapter(),
        compute=DuckDBComputeEngine(meta_path=str(tmp_path / "m2.duckdb")),
        store=store))
    sid = client.post("/connect", json={"graph": {"engine": "kinetica"},
                                        "compute": {"engine": "duckdb"}}).json()["session"]
    r = client.post("/register_file", json={"session": sid, "path": "s3://b/a.parquet",
                                            "table": "airports", "data_source": "my_s3"})
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "airports" and body["type"] == "table"
    assert body["columns"] == ["NODE", "AMOUNT"]
