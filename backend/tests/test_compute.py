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
