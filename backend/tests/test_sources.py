import duckdb
from xgraph_gateway.sources.base import SourceReader
from xgraph_gateway.sources.duckdb_source import DuckDBSourceReader

def _parquet(tmp_path):
    p = tmp_path / "e.parquet"
    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT * FROM (VALUES ('b1','bank'),('w1','wire')) AS v(id,label)")
    con.execute(f"COPY t TO '{p}' (FORMAT parquet)"); con.close()
    return str(p)

def test_duckdb_source_reads_via_view(tmp_path):
    r = DuckDBSourceReader()
    assert isinstance(r, SourceReader)
    rows = r.read({"tables": {"expero.vertexes": _parquet(tmp_path)},
                   "sql": "SELECT id AS node_id, label FROM expero.vertexes ORDER BY id"})
    assert rows == [{"node_id": "b1", "label": "bank"}, {"node_id": "w1", "label": "wire"}]
