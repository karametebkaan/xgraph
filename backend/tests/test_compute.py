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


def test_run_join_rows_sparse_wide_null_first_row():
    # Regression: a wide "union" table (one row per vertex, per-node-type
    # namespaced columns left NULL for other types -- e.g. Kinetica banking's
    # expero.vertexes) has attribute columns that are NULL in the first row and
    # a real string in a later row. Typing the column from row 0 alone made
    # DuckDB pick INTEGER for the bare NULL, so the later string blew up with
    # "Could not convert string '...' to INT32". Types must come from the first
    # NON-NULL value across all rows.
    from xgraph_gateway.compute.duckdb_engine import DuckDBComputeEngine
    import tempfile, os
    eng = DuckDBComputeEngine(meta_path=os.path.join(tempfile.mkdtemp(), "m.duckdb"))
    cypher_rows = [{"NODE": "sar1"}, {"NODE": "addr1"}]
    wide_rows = [
        {"NODE": "sar1", "street_address:address_line_1": None, "risk": 2.5},
        {"NODE": "addr1", "street_address:address_line_1": "7897 Sonny Creek", "risk": None},
    ]
    sql = ('SELECT cypher.NODE AS node, wide."street_address:address_line_1" AS addr '
           'FROM cypher JOIN wide ON cypher.NODE = wide.NODE ORDER BY node')
    out = eng.run_join_rows(cypher_rows, wide_rows, sql)
    assert {r["addr"] for r in out} == {None, "7897 Sonny Creek"}


def test_run_join_rows_numeric_column_stays_aggregatable():
    # A numeric column that is NULL in row 0 must still be typed numeric (not
    # VARCHAR) so aggregations like MAX work -- the fix infers type from the
    # first non-null value, which here is a float.
    from xgraph_gateway.compute.duckdb_engine import DuckDBComputeEngine
    import tempfile, os
    eng = DuckDBComputeEngine(meta_path=os.path.join(tempfile.mkdtemp(), "m.duckdb"))
    cypher_rows = [{"NODE": "a"}, {"NODE": "b"}, {"NODE": "c"}]
    wide_rows = [{"NODE": "a", "risk": None}, {"NODE": "b", "risk": 3.5}, {"NODE": "c", "risk": 1.0}]
    sql = "SELECT max(wide.risk) AS mx FROM cypher JOIN wide ON cypher.NODE = wide.NODE"
    out = eng.run_join_rows(cypher_rows, wide_rows, sql)
    assert out[0]["mx"] == 3.5
