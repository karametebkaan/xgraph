"""Banking use case: Explain with a post-join focus over the real hydrate source.

Reproduces the workbench scenario shown in the README — a SAR→TIN→party→address
traversal returns skinny rows (party ids in `c_node`); the Explain focus "who has
the most SAR activity (number of paths) using the party_name" post-joins the wide
`party:party_name` attribute (which is NOT in the graph) and counts paths per party.

The count is computed by DuckDB over the real `vertexes.parquet`, so the ranking is
deterministic and asserted directly — no LLM in the assertions. These tests SKIP
(not fail) if the wide file is absent, matching the other live-data tests.
"""
from __future__ import annotations
import os

import duckdb
import pytest
from fastapi.testclient import TestClient

from xgraph_gateway import nlcypher
from xgraph_gateway.app import create_app
from xgraph_gateway.adapters.fake import FakeAdapter
from xgraph_gateway.compute.duckdb_engine import ComputeEngine

# The banking hydrate source (the repo's wide vertex Parquet under data/).
# Override with XGRAPH_HYDRATE_SOURCE if it lives elsewhere.
from xgraph_gateway.config import load_settings
SOURCE = os.environ.get(
    "XGRAPH_HYDRATE_SOURCE",
    os.path.join(load_settings().data_dir, "vertexes.parquet"),
)

# The post-join the workbench generates for this focus: pull the wide party name
# for each party id in the result and count paths per name (colon in the column
# name must be quoted in DuckDB).
BANKING_JOIN_SQL = (
    'SELECT w."party:party_name" AS party_name, COUNT(*) AS number_of_paths '
    "FROM cypher c JOIN wide w ON c.c_node = w.NODE "
    'GROUP BY w."party:party_name" ORDER BY number_of_paths DESC'
)

CYPHER = (
    "MATCH p=(a:sar)-[ab:created_for]->(b:tin)<-[bc:represented_by]-(c:party)"
    "-[cd:located_at]->(d:street_address) "
    "RETURN a.NODE as a_node, b.NODE as b_node, ab.LABEL as ab_label, "
    "c.NODE as c_node, bc.LABEL as bc_label, d.NODE as d_node, cd.LABEL as cd_label, p"
)


def _real_parties(n=3):
    """Return n (NODE, party_name) pairs from the real wide table, or skip."""
    if not os.path.exists(SOURCE):
        pytest.skip(f"hydrate source not present: {SOURCE}")
    con = duckdb.connect()
    try:
        return con.execute(
            f'SELECT "NODE", "party:party_name" FROM \'{SOURCE}\' '
            "WHERE label = 'party' AND \"party:party_name\" IS NOT NULL LIMIT ?",
            [n],
        ).fetchall()
    finally:
        con.close()


def _sar_path_rows(parties, multiplicities):
    """Build a SAR-path-shaped result: party i appears in `multiplicities[i]` paths."""
    rows = []
    for (node, _name), m in zip(parties, multiplicities):
        for i in range(m):
            rows.append([f"sar-{node}-{i}", f"tin-{node}", "created_for",
                         node, "represented_by", f"addr-{node}-{i}", "located_at"])
    return rows


COLUMNS = ["a_node", "b_node", "ab_label", "c_node", "bc_label", "d_node", "cd_label"]


def test_postjoin_ranks_parties_by_path_count_over_real_hydrate_source():
    parties = _real_parties(3)
    mult = [3, 2, 1]
    rows = [dict(zip(COLUMNS, r)) for r in _sar_path_rows(parties, mult)]

    out = ComputeEngine().run_join(rows, SOURCE, BANKING_JOIN_SQL)

    # Deterministic ranking: names ordered by descending path count.
    assert [(r["party_name"], r["number_of_paths"]) for r in out] == [
        (parties[0][1], 3), (parties[1][1], 2), (parties[2][1], 1)
    ]


def test_explain_endpoint_banking_sar_party_name(monkeypatch):
    parties = _real_parties(3)
    mult = [3, 2, 1]
    rows = _sar_path_rows(parties, mult)
    focus = "who has the most SAR activity (number of paths) using the party_name"

    # Pin the NL->SQL step to the join the workbench produces for this focus; the
    # post-join itself runs for real against vertexes.parquet.
    def fake_generate_join_sql(f, cyp, result_columns, wide_columns, llm=None):
        assert f == focus
        assert cyp == CYPHER
        assert "c_node" in result_columns
        assert "party:party_name" in wide_columns
        return BANKING_JOIN_SQL

    captured = {}
    def fake_synthesize(question, cols, rows_, llm=None, cypher=None):
        captured["cols"], captured["rows"] = cols, rows_
        return f"{rows_[0][0]} has the most SAR activity with {rows_[0][1]} paths."

    monkeypatch.setattr(nlcypher, "generate_join_sql", fake_generate_join_sql)
    monkeypatch.setattr(nlcypher, "synthesize", fake_synthesize)

    app = create_app(adapter_factory=lambda e: FakeAdapter(), compute=ComputeEngine())
    r = TestClient(app).post("/explain", json={
        "question": focus, "columns": COLUMNS, "rows": rows,
        "cypher": CYPHER, "source": SOURCE,
    })

    assert r.status_code == 200
    body = r.json()
    assert body["hydrated"] is True
    assert body["join_sql"] == BANKING_JOIN_SQL
    assert body["columns"] == ["party_name", "number_of_paths"]
    assert body["rows"][0] == [parties[0][1], 3]
    # synthesize received the aggregated table, not the raw skinny rows.
    assert captured["cols"] == ["party_name", "number_of_paths"]
    assert body["answer"].startswith(parties[0][1])
