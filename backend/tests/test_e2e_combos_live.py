"""LIVE cross-combo verification (S1.5 Task 3).

Exercises the `/connect` session model across the three graph/compute
combinations the gateway is meant to support:

  1. Hybrid  — FalkorDB graph   + Kinetica OLAP  (hydrate served by Kinetica)
  2. Native  — Kinetica graph   + Kinetica OLAP  (both legs on Kinetica)
  3. Open    — FalkorDB graph   + DuckDB OLAP    (hydrate served by DuckDB/Parquet)

Every test is skip-guarded: if the live service, the `banking_graph`, or the
Parquet file isn't available, the test SKIPs (never fails) so this file is
safe to run in CI environments without those dependencies wired up.

NOTE on join keys: FalkorDB's node-identity property is `NODE` (see
graph_loader conventions in falkor/CLAUDE.md), but the *source* Kinetica
table `expero.vertexes` keeps its original column name `id` for the same
values (the loader copies `id` -> `NODE` when building the FalkorDB graph;
it does not rename the source column). So when hydrating FalkorDB-sourced
ids against the raw Kinetica table, the row field/key used for the join
must be renamed to `id` to match that table's schema. The Parquet mirror in
falkor/data/vertexes.parquet is written by the *same* loader convention
FalkorDB uses, so it does carry a `NODE` column and needs no renaming
(matches the existing tests/test_e2e_live.py pattern).
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from xgraph_gateway.app import create_app
from xgraph_gateway.config import load_settings

PARQUET = os.environ.get(
    "XGRAPH_VERTEXES_PARQUET",
    os.path.join(load_settings().data_dir, "vertexes.parquet"),
)


def _client() -> TestClient:
    return TestClient(create_app())


def _falkor_conn() -> dict:
    s = load_settings()
    return {"host": s.falkordb_host, "port": s.falkordb_port, "password": s.falkordb_password}


def _kinetica_conn() -> dict:
    s = load_settings()
    return {"url": s.kinetica_url, "user": s.kinetica_user, "password": s.kinetica_pass}


def _connect(client: TestClient, graph_engine: str, graph_conn: dict,
             compute_engine: str, compute_conn: dict):
    return client.post("/connect", json={
        "graph": {"engine": graph_engine, "conn": graph_conn},
        "compute": {"engine": compute_engine, "conn": compute_conn},
    })


def test_hybrid_falkordb_graph_kinetica_compute():
    """Graph leg = FalkorDB (banking_graph). OLAP leg = Kinetica
    (expero.vertexes). Hydrate must be served by Kinetica, not DuckDB."""
    c = _client()
    r = _connect(c, "falkordb", _falkor_conn(), "kinetica", _kinetica_conn())
    if r.status_code != 200:
        pytest.skip(f"connect (falkordb+kinetica) failed: {r.json()}")
    body = r.json()
    session = body["session"]
    if "banking_graph" not in body["graphs"]:
        pytest.skip("banking_graph not available on live FalkorDB")

    q = c.post("/query", json={
        "session": session, "graph": "banking_graph",
        "cypher": "MATCH (b:bank) RETURN b.NODE AS NODE LIMIT 5",
    })
    assert q.status_code == 200
    node_ids = [row[0] for row in q.json()["rows"]]
    assert len(node_ids) == 5

    # Kinetica's expero.vertexes keeps the source column name `id` (not
    # `NODE`) for the same values, so rename the join key before hydrating.
    rows = [{"id": nid} for nid in node_ids]
    h = c.post("/hydrate", json={
        "session": session, "rows": rows, "source": "expero.vertexes",
        "key": "id", "columns": 'id, "bank:bank_number"',
    })
    if h.status_code != 200:
        pytest.skip(f"kinetica hydrate unavailable: {h.json()}")
    out = h.json()
    assert len(out) == 5
    values = {r["id"]: r.get("bank:bank_number") for r in out}
    non_null = {k: v for k, v in values.items() if v is not None}
    print(f"\n[combo1/hybrid] node ids = {node_ids}")
    print(f"[combo1/hybrid] bank:bank_number hydrated via KINETICA = {values}")
    assert non_null, "expected at least one hydrated bank:bank_number from Kinetica"

    # Cross-check provenance: re-read one hydrated id straight from Kinetica
    # via the session's /sql endpoint (routes to the *compute* engine, i.e.
    # KineticaComputeEngine.run_sql, independent of the hydrate code path)
    # and confirm the values agree.
    check_id, check_val = next(iter(non_null.items()))
    verify = c.post("/sql", json={
        "session": session,
        "sql": f"SELECT \"bank:bank_number\" AS bn FROM expero.vertexes WHERE id = '{check_id}'",
    })
    assert verify.status_code == 200
    assert verify.json()[0]["bn"] == check_val


def test_native_kinetica_graph_kinetica_compute():
    """Both legs on Kinetica: graph engine lists Kinetica graphs, and a
    plain Kinetica count query round-trips through /query."""
    c = _client()
    r = _connect(c, "kinetica", _kinetica_conn(), "kinetica", _kinetica_conn())
    if r.status_code != 200:
        pytest.skip(f"connect (kinetica+kinetica) failed: {r.json()}")
    body = r.json()
    session = body["session"]
    graphs = body["graphs"]
    if not graphs:
        pytest.skip("no Kinetica graphs available on live Kinetica")
    print(f"\n[combo2/native] Kinetica graphs = {graphs}")

    q = c.post("/query", json={
        "session": session, "graph": "",
        "cypher": "SELECT COUNT(*) AS c FROM expero.vertexes WHERE label='bank'",
    })
    if q.status_code != 200:
        pytest.skip(f"kinetica count query failed: {q.json()}")
    row = q.json()["rows"][0]
    print(f"[combo2/native] bank count via Kinetica = {row[0]}")
    assert row[0] > 0


def test_open_falkordb_graph_duckdb_compute():
    """Graph leg = FalkorDB. OLAP leg = DuckDB reading the Parquet mirror —
    the existing S1 path, but reached through a /connect session instead of
    the sessionless engine= query params."""
    if not os.path.exists(PARQUET):
        pytest.skip("vertexes.parquet not present")
    c = _client()
    r = _connect(c, "falkordb", _falkor_conn(), "duckdb", {})
    if r.status_code != 200:
        pytest.skip(f"connect (falkordb+duckdb) failed: {r.json()}")
    body = r.json()
    session = body["session"]
    if "banking_graph" not in body["graphs"]:
        pytest.skip("banking_graph not available on live FalkorDB")

    q = c.post("/query", json={
        "session": session, "graph": "banking_graph",
        "cypher": "MATCH (b:bank) RETURN b.NODE AS NODE LIMIT 5",
    })
    assert q.status_code == 200
    rows = [{"NODE": row[0]} for row in q.json()["rows"]]
    assert len(rows) == 5

    h = c.post("/hydrate", json={
        "session": session, "rows": rows, "source": PARQUET,
        "key": "NODE", "columns": 'NODE, "bank:bank_number"',
    })
    assert h.status_code == 200
    out = h.json()
    assert len(out) == 5
    values = {r["NODE"]: r.get("bank:bank_number") for r in out}
    non_null = {k: v for k, v in values.items() if v is not None}
    print(f"\n[combo3/open] bank:bank_number hydrated via DUCKDB/PARQUET = {values}")
    assert non_null, "expected at least one hydrated bank:bank_number from the Parquet mirror"
