"""Headline acceptance test for column promotion: a mid-traversal filter on a
promoted column. Self-builds a tiny wide graph + Parquet so it runs without the
full 622k banking graph, but exercises the exact mechanism the banking-graph
browser acceptance uses. SKIPs if FalkorDB is unreachable.
"""
import duckdb
import pytest
from xgraph_gateway import config
from xgraph_gateway.adapters.falkordb_adapter import FalkorDBAdapter


def _adapter_or_skip():
    # Mirror the live-FalkorDB helper in test_extract_ask_live.py: the adapter
    # builds its own connection from config.load_settings(); skip if unreachable.
    s = config.load_settings()
    try:
        a = FalkorDBAdapter(s)
        a.list_graphs()  # cheap round-trip; skip if unreachable
        return a
    except Exception as e:
        pytest.skip(f"FalkorDB unreachable: {e}")


def _wide_parquet(tmp_path):
    p = str(tmp_path / "wide.parquet")
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE t AS SELECT * FROM (VALUES "
        "('n1', 'Acme'), ('n2', 'Beta'), ('n3', NULL)"
        ') AS v("NODE", "party:party_name")')
    con.execute(f"COPY t TO '{p}' (FORMAT PARQUET)")
    con.close()
    return p


def test_promote_then_mid_traversal_filter_on_promoted_column(tmp_path):
    a = _adapter_or_skip()
    graph = "xgraph_promote_test"
    # Build a tiny skinny graph: nodes carry only NODE (no party_name yet),
    # labeled :party like a wide-source (falcor create) graph -- NOT :Entity.
    if graph in a.list_graphs():
        a._graph(graph).delete()
    g = a._graph(graph)
    g.query("CREATE (:party {NODE:'n1'}), (:party {NODE:'n2'}), (:party {NODE:'n3'})",
            timeout=60000)

    # Before promotion, a mid-traversal filter on the wide column matches
    # nothing (FalkorDB returns NULL for the absent property -- the whole gap).
    before = g.query(
        "MATCH (n:party) WHERE n.`party:party_name` = 'Acme' RETURN n.NODE",
        timeout=60000).result_set
    assert before == []

    # Promote the whole column from the wide Parquet.
    res = a.promote_columns(graph, _wide_parquet(tmp_path),
                            key="NODE", columns=["party:party_name"])
    assert res["promoted"] == ["party:party_name"]
    assert res["nodes_matched"] == 2      # n1, n2 matched; n3 null-skipped
    assert res["properties_set"] == 2

    # After promotion the SAME mid-traversal filter now works.
    after = g.query(
        "MATCH (n:party) WHERE n.`party:party_name` = 'Acme' RETURN n.NODE",
        timeout=60000).result_set
    assert [r[0] for r in after] == ["n1"]

    # A node whose source cell was null has no such property (null-skip).
    n3 = g.query(
        "MATCH (n:party {NODE:'n3'}) RETURN n.`party:party_name` IS NULL",
        timeout=60000).result_set
    assert n3[0][0] is True

    g.delete()  # cleanup
