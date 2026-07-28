from decimal import Decimal
import duckdb
import pytest
from xgraph_gateway.compute.duckdb_engine import DuckDBComputeEngine


def _make_parquet(tmp_path):
    # A wide source: NODE key + a colon-named wide column + a DECIMAL column
    # + a NULL cell, written to Parquet so the reader exercises the real path.
    p = str(tmp_path / "vertexes.parquet")
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE t AS SELECT * FROM (VALUES "
        "('b1', 'Acme', CAST(10.5 AS DECIMAL(10,2))), "
        "('b2', 'Beta', CAST(3.0 AS DECIMAL(10,2))), "
        "('b3', NULL,   CAST(7.0 AS DECIMAL(10,2)))"
        ') AS v("NODE", "party:party_name", "amount")')
    con.execute(f"COPY t TO '{p}' (FORMAT PARQUET)")
    con.close()
    return p


def test_read_columns_projects_key_and_requested_columns(tmp_path):
    p = _make_parquet(tmp_path)
    rows = DuckDBComputeEngine().read_columns(
        p, key="NODE", columns=["party:party_name", "amount"])
    by = {r["NODE"]: r for r in rows}
    assert set(by) == {"b1", "b2", "b3"}
    assert by["b1"]["party:party_name"] == "Acme"
    # DECIMAL coerced to float (never Decimal handed to the FalkorDB client)
    assert isinstance(by["b1"]["amount"], float)
    assert not isinstance(by["b1"]["amount"], Decimal)
    # null cell survives as None (null-stripping happens later, in the builder)
    assert by["b3"]["party:party_name"] is None


def test_read_columns_empty_columns_raises(tmp_path):
    p = _make_parquet(tmp_path)
    with pytest.raises(ValueError):
        DuckDBComputeEngine().read_columns(p, key="NODE", columns=[])
