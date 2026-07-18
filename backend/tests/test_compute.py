import duckdb
from decimal import Decimal
from xgraph_gateway import config
from xgraph_gateway.compute.duckdb_engine import ComputeEngine

def _wide(tmp_path):
    p = tmp_path / "v.parquet"
    con = duckdb.connect()
    con.execute("""CREATE TABLE t AS SELECT * FROM (VALUES
        ('b1','Acme', 10.5),('b2','Beta', 3.0)) AS v(NODE, name, amount)""")
    con.execute(f"COPY t TO '{p}' (FORMAT parquet)"); con.close()
    return str(p)

def test_hydrate_attaches_and_coerces(tmp_path):
    eng = ComputeEngine()
    out = eng.hydrate([{"NODE": "b1", "risk": 1}], _wide(tmp_path), key="NODE")
    assert out[0]["name"] == "Acme"
    assert out[0]["risk"] == 1
    assert isinstance(out[0]["amount"], float) and not isinstance(out[0]["amount"], Decimal)

def test_run_sql_coerces(tmp_path):
    eng = ComputeEngine()
    rows = eng.run_sql(f"SELECT * FROM '{_wide(tmp_path)}' ORDER BY NODE")
    assert rows[0]["NODE"] == "b1"
    assert isinstance(rows[0]["amount"], float)


def test_run_join_rows_joins_two_in_memory_relations():
    from xgraph_gateway.compute.duckdb_engine import DuckDBComputeEngine
    import tempfile, os
    eng = DuckDBComputeEngine(meta_path=os.path.join(tempfile.mkdtemp(), "m.duckdb"))
    cypher_rows = [{"NODE": "a"}, {"NODE": "b"}]
    wide_rows = [{"NODE": "a", "city": "NYC"}, {"NODE": "b", "city": "SF"}, {"NODE": "c", "city": "LA"}]
    sql = ("SELECT wide.city AS city, COUNT(*) AS n FROM cypher "
           "JOIN wide ON cypher.NODE = wide.NODE GROUP BY wide.city ORDER BY city")
    out = eng.run_join_rows(cypher_rows, wide_rows, sql)
    assert {r["city"] for r in out} == {"NYC", "SF"}  # 'c' not in cypher -> excluded
