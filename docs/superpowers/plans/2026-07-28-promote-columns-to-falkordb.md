# Promote Source Columns into FalkorDB Properties — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an explicit "promote" operation that reads whole columns from a wide source (Parquet/CSV/table) and materializes them as properties on the matching existing FalkorDB nodes, so those attributes become mid-traversal filterable (`WHERE n.\`party:party_name\` = 'Acme'`).

**Architecture:** A new additive code path — never touching the text-extraction upsert path. The gateway reads `(key + columns)` for every source row via a new whole-column DuckDB reader, builds a null-stripped `attrs` map per row, and writes it to FalkorDB in 5000-row batches with a **MATCH-only** `SET n += r.attrs` (label-agnostic, keyed on the node-key property; never MERGE, so no nodes are created). Surfaced as `POST /promote_columns`, a `gateway.js` client method, and a Query-panel control gated to `engine === 'falkordb'`.

**Tech Stack:** Python 3 / FastAPI gateway (`backend/`), DuckDB (embedded), FalkorDB (RESP/openCypher via `falkordb-py`), single-file React 18 UMD + Babel-standalone frontend (`frontend/XGraph.html`, no build step), `gateway.js` UMD client (Node-testable).

## Global Constraints

- **NO REGRESSION to the text-extraction → FalkorDB path.** `_upsert_statements`, `build_ingest_cypher`, `ingest_elements`, and the `n += r.attrs` MERGE upsert in `falkordb_adapter.py` — and their existing tests — MUST NOT be modified. Promotion is a **new additive sibling** code path only. A dedicated step re-runs the extract/upsert tests to prove no regression.
- **Property naming is VERBATIM** the source column name (e.g. `party:party_name`). Keys travel inside the `$rows[].attrs` parameter map (never interpolated), so a colon is safe with no escaping on the write side. Reading them in Cypher requires backticks: `` n.`party:party_name` ``.
- **MATCH-only, label-agnostic, keyed on the node key (default `NODE`).** Never MERGE — promotion creates no nodes. Must NOT constrain on `:Entity` (wide-source graphs label nodes `:party` etc.).
- **Null cells are skipped** — a null source value is not written as a property; a row whose key is null or whose every requested cell is null contributes no write.
- **FalkorDB only.** Kinetica raises an explicit "not supported" error (it materializes real typed columns at extract time). Other engines / the base contract default: unsupported.
- **Batch size 5000** (matches the existing FalkorDB sink/ingest batching).
- **DuckDB returns DECIMAL as Python `Decimal`** — coerce to `float` via `graph_loader.duckdb_source.coerce_row` before handing to FalkorDB.
- **No commits under `xgraph/` unless the user has said to commit.** The repo default is local-only. Each task's "Commit" step assumes the user has lifted the rule for this feature; if not lifted, stage nothing and treat the step as a no-op checkpoint.
- **Acceptance test (headline, user-mandated):** promote a real wide column onto the **banking graph in live FalkorDB**, then run a mid-traversal Cypher `WHERE` filter on that promoted, backtick-quoted property and confirm it filters correctly (was previously silently NULL). Automated as a skippable live test; also documented as a manual browser acceptance step.
- **Frontend edits** to the ~10,000-line `XGraph.html` are anchored search-and-replace against verbatim strings; validate with the Babel transpile check + a `curl` 200; defer real behavior to the browser. Bump `EXPLORER_VERSION`.

---

## File Structure

- `backend/xgraph_gateway/compute/duckdb_engine.py` — **modify**: add `read_columns(source, key, columns)` whole-column reader (hydrate projection read minus the id filter).
- `backend/xgraph_gateway/adapters/falkordb_adapter.py` — **modify (additive only)**: add module-level `build_promote_cypher(...)` and `FalkorDBAdapter.promote_columns(...)`. Do not touch `_upsert_statements`/`build_ingest_cypher`/`ingest_elements`.
- `backend/xgraph_gateway/adapters/base.py` — **modify**: declare `promote_columns(...)` on the contract with a default that raises "unsupported".
- `backend/xgraph_gateway/adapters/kinetica_adapter.py` — **modify**: override `promote_columns(...)` to raise the explicit Kinetica-unsupported error.
- `backend/xgraph_gateway/adapters/fake.py` — **modify**: add a canned `promote_columns(...)` so the gateway happy-path test can exercise the route.
- `backend/xgraph_gateway/app.py` — **modify**: add `POST /promote_columns` route.
- `frontend/gateway.js` — **modify**: add `promoteColumns(graph, source, key, columns)` client method.
- `frontend/XGraph.html` — **modify**: Query-panel "Promote columns" control (gated to `engine === 'falkordb'`) + `EXPLORER_VERSION` bump.
- Tests:
  - `backend/tests/test_compute_read_columns.py` — **create** (unit, `read_columns`).
  - `backend/tests/test_promote_cypher.py` — **create** (unit, `build_promote_cypher`).
  - `backend/tests/test_promote_columns_live.py` — **create** (live FalkorDB, skippable; banking-graph acceptance).
  - `backend/tests/test_app.py` (or the existing gateway test module) — **modify**: add `/promote_columns` gateway tests via `FakeAdapter`.
  - `frontend/tests/test_client.mjs` — **modify**: add a `promoteColumns()` client test.

---

### Task 1: Whole-column DuckDB reader (`read_columns`)

**Files:**
- Modify: `backend/xgraph_gateway/compute/duckdb_engine.py`
- Test: `backend/tests/test_compute_read_columns.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks. Reuses already-imported `resolve_data_path`, `coerce_row`, and the module-level `duckdb`.
- Produces: `DuckDBComputeEngine.read_columns(source, key="NODE", columns=None) -> list[dict]` — one dict per source row containing the key column plus each requested column, Decimals coerced to float. Raises `ValueError` on empty `columns` or an unsafe (single-quote-bearing) resolved path.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_compute_read_columns.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_compute_read_columns.py -v`
Expected: FAIL — `AttributeError: 'DuckDBComputeEngine' object has no attribute 'read_columns'`.

- [ ] **Step 3: Write minimal implementation**

Add this method to `DuckDBComputeEngine` (place it right after `hydrate`, near line 270, so it sits with the other read methods). Do not modify `hydrate` or `describe_source`.

```python
    def read_columns(self, source, key="NODE", columns=None):
        """Read (key + columns) for EVERY row of a wide source, so those
        columns can be PROMOTED onto graph nodes. Unlike `hydrate`, there is
        no `WHERE key IN (...)` id filter -- the whole column is read
        (projection pushdown keeps unused columns off the read). Returns a
        list of dicts (key + each requested column); Decimals are coerced to
        float via coerce_row. `columns` is a list of source column names; the
        key is always included and de-duplicated. Column identifiers may
        contain non-identifier characters (e.g. 'party:party_name'), so each
        is double-quoted; the resolved path is single-quoted with an
        injection guard (mirroring describe_source)."""
        cols = [c for c in (columns or []) if c and c != key]
        if not cols:
            raise ValueError("columns must be a non-empty list")
        path = resolve_data_path(source)
        if "'" in str(path):
            raise ValueError(f"unsafe source path: {path!r}")
        select = ", ".join('"' + c.replace('"', '""') + '"'
                           for c in [key] + cols)
        con = duckdb.connect()
        try:
            cur = con.execute(f"SELECT {select} FROM '{path}'")
            out_cols = [d[0] for d in cur.description]
            return [coerce_row(out_cols, r) for r in cur.fetchall()]
        finally:
            con.close()
```

> If `resolve_data_path`, `coerce_row`, or `duckdb` are not already imported at the top of `duckdb_engine.py`, they are (confirmed: `hydrate`/`describe_source`/`run_join` use all three). Do not add duplicate imports.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_compute_read_columns.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add backend/xgraph_gateway/compute/duckdb_engine.py backend/tests/test_compute_read_columns.py
git commit -m "feat(promote): whole-column DuckDB reader (read_columns) for column promotion"
```

---

### Task 2: MATCH-only promote statement builder (`build_promote_cypher`)

**Files:**
- Modify: `backend/xgraph_gateway/adapters/falkordb_adapter.py` (additive — new module-level function only)
- Test: `backend/tests/test_promote_cypher.py` (create)

**Interfaces:**
- Consumes: `safe_ident` (already imported at `falkordb_adapter.py:14`).
- Produces: module-level `build_promote_cypher(rows, key="NODE", columns=None, batch_size=5000) -> list[tuple[str, dict]]`. Each tuple is `(cypher, params)` where `cypher` is a MATCH-only `UNWIND $rows AS r MATCH (n {<key>: r.id}) SET n += r.attrs RETURN count(n) AS matched` and `params` is `{"rows": [{"id": <key value>, "attrs": {col: non-null value, ...}}, ...]}`. Rows with a null key or an all-null attrs map are dropped. Property keys inside `attrs` are the verbatim source column names.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_promote_cypher.py
from xgraph_gateway.adapters.falkordb_adapter import build_promote_cypher


def test_builder_is_match_only_never_merge():
    stmts = build_promote_cypher(
        [{"NODE": "b1", "party:party_name": "Acme"}],
        key="NODE", columns=["party:party_name"])
    assert len(stmts) == 1
    cypher, params = stmts[0]
    assert "MATCH (n {NODE: r.id})" in cypher
    assert "SET n += r.attrs" in cypher
    assert "MERGE" not in cypher            # never creates nodes
    assert "RETURN count(n)" in cypher      # for nodes_matched accounting


def test_verbatim_key_travels_in_params_not_query_text():
    stmts = build_promote_cypher(
        [{"NODE": "b1", "party:party_name": "Acme"}],
        key="NODE", columns=["party:party_name"])
    cypher, params = stmts[0]
    # colon-bearing column name is a MAP KEY in params, never in the query text
    assert "party:party_name" not in cypher
    assert params["rows"] == [{"id": "b1", "attrs": {"party:party_name": "Acme"}}]


def test_null_cells_are_stripped_and_allnull_rows_dropped():
    stmts = build_promote_cypher(
        [
            {"NODE": "b1", "a": 1, "b": None},   # b dropped
            {"NODE": "b2", "a": None, "b": None},# all-null -> row dropped
            {"NODE": None, "a": 5, "b": 6},      # null key -> row dropped
        ],
        key="NODE", columns=["a", "b"])
    rows = stmts[0][1]["rows"]
    assert rows == [{"id": "b1", "attrs": {"a": 1}}]


def test_batches_respect_batch_size():
    rows = [{"NODE": f"b{i}", "a": i} for i in range(12)]
    stmts = build_promote_cypher(rows, key="NODE", columns=["a"], batch_size=5)
    assert [len(s[1]["rows"]) for s in stmts] == [5, 5, 2]


def test_empty_payload_yields_no_statements():
    stmts = build_promote_cypher(
        [{"NODE": None, "a": None}], key="NODE", columns=["a"])
    assert stmts == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_promote_cypher.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_promote_cypher'`.

- [ ] **Step 3: Write minimal implementation**

Add this **new module-level function** to `falkordb_adapter.py`, immediately after `build_ingest_cypher` (near line 253) so the two builders sit together. Do NOT modify `build_ingest_cypher`, `_upsert_statements`, `_valid_nodes`, `_valid_edges`, or `_cypher_ident`.

```python
def build_promote_cypher(rows, key="NODE", columns=None, batch_size=5000):
    """Build MATCH-only SET statements that promote whole wide-source columns
    onto EXISTING graph nodes, making them mid-traversal filterable.

    Additive sibling to `build_ingest_cypher` -- it shares no code with the
    extraction upsert path and never MERGEs, so it creates no nodes. Each
    statement is:

        UNWIND $rows AS r
        MATCH (n {<key>: r.id})       -- label-agnostic; NEVER :Entity
        SET n += r.attrs
        RETURN count(n) AS matched

    `key` is validated with safe_ident and interpolated into the MATCH pattern
    (identifier-only, injection-safe). Property keys are the VERBATIM source
    column names -- they travel inside the r.attrs parameter map, never in the
    query text, so a colon (e.g. 'party:party_name') is safe with no escaping.
    Null cells are dropped from attrs; a row whose key is null or whose every
    requested cell is null contributes no write. Returns a list of
    (cypher, params) tuples, one per <=batch_size batch (empty if nothing to
    write)."""
    columns = [c for c in (columns or []) if c]
    key = safe_ident(key)
    payload = []
    for r in rows:
        rid = r.get(key)
        if rid is None:
            continue
        attrs = {c: r[c] for c in columns if r.get(c) is not None}
        if not attrs:
            continue
        payload.append({"id": rid, "attrs": attrs})
    cypher = (
        "UNWIND $rows AS r\n"
        f"MATCH (n {{{key}: r.id}})\n"
        "SET n += r.attrs\n"
        "RETURN count(n) AS matched"
    )
    return [(cypher, {"rows": payload[i:i + batch_size]})
            for i in range(0, len(payload), batch_size)]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_promote_cypher.py -v`
Expected: PASS (all five tests).

- [ ] **Step 5: Commit**

```bash
git add backend/xgraph_gateway/adapters/falkordb_adapter.py backend/tests/test_promote_cypher.py
git commit -m "feat(promote): MATCH-only promote-columns Cypher builder (null-stripped, batched)"
```

---

### Task 3: `promote_columns` on the adapter contract + FalkorDB + Kinetica

**Files:**
- Modify: `backend/xgraph_gateway/adapters/base.py` (contract default)
- Modify: `backend/xgraph_gateway/adapters/falkordb_adapter.py` (implement — additive)
- Modify: `backend/xgraph_gateway/adapters/kinetica_adapter.py` (explicit unsupported)
- Test: `backend/tests/test_promote_columns_live.py` (create — live FalkorDB, skippable)

**Interfaces:**
- Consumes: `build_promote_cypher` (Task 2), `DuckDBComputeEngine.read_columns` (Task 1), `self._graph(graph)` (existing).
- Produces: `GraphEngineAdapter.promote_columns(graph, source, key="NODE", columns=None) -> dict`. Default (base) and Kinetica raise `ValueError` ("promotion not supported for <engine>"). FalkorDB returns `{"promoted": [...], "nodes_matched": int, "properties_set": int, "source": source, "key": key}`.

- [ ] **Step 1: Write the failing test (live FalkorDB, skippable)**

This is the **headline acceptance test** — a mid-traversal filter on a promoted column. It self-builds a tiny wide graph + Parquet so it runs without the full 622k banking graph, but exercises the exact mechanism the banking-graph browser acceptance uses. It SKIPs if FalkorDB is unreachable.

```python
# backend/tests/test_promote_columns_live.py
import duckdb
import pytest
from xgraph_gateway import config
from xgraph_gateway.adapters.falkordb_adapter import FalkorDBAdapter


def _adapter_or_skip():
    s = config.load_settings()
    conn = {"host": s.falkordb_host, "port": s.falkordb_port,
            "password": s.falkordb_password}
    a = FalkorDBAdapter(conn=conn)
    try:
        a.list_graphs()  # cheap round-trip; skip if unreachable
    except Exception as e:
        pytest.skip(f"FalkorDB unreachable: {e}")
    return a


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
    g = a._graph(graph)
    # Build a tiny skinny graph: nodes carry only NODE (no party_name yet),
    # labeled :party like a wide-source (falcor create) graph -- NOT :Entity.
    g.delete() if graph in a.list_graphs() else None
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
```

> Adjust `FalkorDBAdapter(conn=...)` construction and the `config.load_settings()` field names to match the existing live tests in the repo (e.g. `backend/tests/` FalkorDB integration tests) — copy their exact connection-building helper if one exists rather than re-deriving it.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_promote_columns_live.py -v`
Expected: FAIL (if FalkorDB is up) — `AttributeError: 'FalkorDBAdapter' object has no attribute 'promote_columns'`; or SKIP if FalkorDB is down (then verify the implementation via the Task-4 Fake test and run this live test manually once FalkorDB is available).

- [ ] **Step 3a: Add the base-contract default**

In `backend/xgraph_gateway/adapters/base.py`, add a concrete default alongside the other non-abstract defaults (`ingest_elements`, `list_columns`, etc.):

```python
    def promote_columns(self, graph, source, key="NODE", columns=None):
        """Promote whole wide-source columns onto existing nodes as properties
        (making them mid-traversal filterable). FalkorDB-only; the default is
        unsupported."""
        raise ValueError(
            f"promotion not supported for {getattr(self, 'engine', 'this engine')}")
```

> If `GraphEngineAdapter` subclasses don't carry an `engine` attribute, use a literal string in each override instead; the base message just needs to be a clear "unsupported" that `_status_for` maps to 400 (any message without "timeout"/"unreachable"/"connection" → 400).

- [ ] **Step 3b: Implement on FalkorDB (additive)**

Add this method to `FalkorDBAdapter` (place it near `ingest_elements`, ~line 514, with the other write methods). It reuses `build_promote_cypher` (Task 2) and `read_columns` (Task 1). Match `ingest_elements`'s exact `g.query(query, params, timeout=60000)` call convention.

```python
    def promote_columns(self, graph, source, key="NODE", columns=None):
        """Promote whole wide-source columns onto existing nodes so they
        become mid-traversal filterable. MATCH-only (never creates nodes);
        null cells skipped. ADDITIVE -- shares no code with the extraction
        upsert path (_upsert_statements/build_ingest_cypher/ingest_elements)."""
        from xgraph_gateway.compute.duckdb_engine import DuckDBComputeEngine
        columns = [c for c in (columns or []) if c]
        if not columns:
            raise ValueError("columns must be a non-empty list")
        key = safe_ident(key)
        rows = DuckDBComputeEngine().read_columns(source, key=key, columns=columns)
        stmts = build_promote_cypher(rows, key=key, columns=columns)
        g = self._graph(graph)
        nodes_matched = 0
        properties_set = 0
        for query, params in stmts:
            qr = g.query(query, params, timeout=60000)
            if qr.result_set:
                nodes_matched += qr.result_set[0][0] or 0
            properties_set += getattr(qr, "properties_set", 0) or 0
        return {"promoted": columns, "nodes_matched": nodes_matched,
                "properties_set": properties_set, "source": source, "key": key}
```

- [ ] **Step 3c: Explicit unsupported on Kinetica**

In `backend/xgraph_gateway/adapters/kinetica_adapter.py`, override `promote_columns` on the Kinetica adapter class with an explicit, informative error:

```python
    def promote_columns(self, graph, source, key="NODE", columns=None):
        """Kinetica materializes real typed columns at extract time
        (ALTER TABLE ADD COLUMN), so the skinny/wide promote gap is a FalkorDB
        concern only."""
        raise ValueError(
            "promotion not supported for kinetica: Kinetica materializes real "
            "typed columns at extract time (no schemaless property maps to "
            "promote into)")
```

- [ ] **Step 4: Run the live test (or verify via Task 4 if FalkorDB is down)**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_promote_columns_live.py -v`
Expected: PASS if FalkorDB is up; SKIP otherwise.

- [ ] **Step 5: Commit**

```bash
git add backend/xgraph_gateway/adapters/base.py backend/xgraph_gateway/adapters/falkordb_adapter.py backend/xgraph_gateway/adapters/kinetica_adapter.py backend/tests/test_promote_columns_live.py
git commit -m "feat(promote): FalkorDBAdapter.promote_columns (additive) + contract default + kinetica unsupported"
```

---

### Task 4: `POST /promote_columns` gateway route

**Files:**
- Modify: `backend/xgraph_gateway/app.py`
- Modify: `backend/xgraph_gateway/adapters/fake.py` (canned `promote_columns` for the happy-path test)
- Test: the existing gateway test module (`backend/tests/test_app.py` or equivalent — match where other endpoint tests live)

**Interfaces:**
- Consumes: `_resolve_adapter`, `_resolve_engine`, `_err` (existing in `app.py`); `FakeAdapter` injected via `create_app(adapter_factory=...)`.
- Produces: `POST /promote_columns` accepting `{session?, engine?, graph, source, key?, columns[]}` → the adapter's `promote_columns` result dict, or the uniform `{"error":{...}}` envelope. Empty `columns` → 400. Non-FalkorDB engine → 400 (adapter raises "not supported", `_status_for` → 400).

- [ ] **Step 1: Add a canned `promote_columns` to `FakeAdapter`**

In `backend/xgraph_gateway/adapters/fake.py`, add:

```python
    def promote_columns(self, graph, source, key="NODE", columns=None):
        columns = list(columns or [])
        return {"promoted": columns, "nodes_matched": len(columns),
                "properties_set": len(columns), "source": source, "key": key}
```

- [ ] **Step 2: Write the failing gateway tests**

Add to the gateway test module (use the same `create_app(adapter_factory=lambda ...: FakeAdapter())` + `TestClient` pattern the other endpoint tests use):

```python
def test_promote_columns_happy_path(client):
    # `client` is the existing fixture wiring a FakeAdapter via create_app.
    r = client.post("/promote_columns", json={
        "graph": "g1", "source": "vertexes.parquet",
        "key": "NODE", "columns": ["party:party_name", "amount"]})
    assert r.status_code == 200
    body = r.json()
    assert body["promoted"] == ["party:party_name", "amount"]
    assert body["source"] == "vertexes.parquet"
    assert body["key"] == "NODE"


def test_promote_columns_empty_columns_is_400(client):
    r = client.post("/promote_columns", json={
        "graph": "g1", "source": "vertexes.parquet", "columns": []})
    assert r.status_code == 400
    assert r.json()["error"]["code"]  # uniform error envelope
```

> If a non-FalkorDB engine is easy to exercise in the gateway harness, also assert a 400 for it. Otherwise the engine-gating 400 is covered by Task 3's Kinetica/base unit behavior (adapter raises "not supported" → `_status_for` → 400).

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_app.py -k promote -v`
Expected: FAIL — 404 (route not registered) / KeyError.

- [ ] **Step 4: Add the route to `app.py`**

Place it near `/hydrate` (line ~584) so it sits with the other compute/write POST routes. Mirror the existing try/except → `_err` pattern.

```python
@app.post("/promote_columns")
def promote_columns(payload: dict = Body(...)):
    session = payload.get("session")
    engine = payload.get("engine", "")
    try:
        graph = payload["graph"]
        source = payload["source"]
        key = payload.get("key", "NODE")
        columns = [c for c in (payload.get("columns") or []) if c]
        if not columns:
            raise ValueError("columns must be a non-empty list")
        adapter = _resolve_adapter(session, engine)
        return adapter.promote_columns(graph, source, key=key, columns=columns)
    except Exception as exc:
        return _err(_resolve_engine(session, engine) or engine, exc)
```

> Match the exact route-decorator / `Body`/payload style the neighboring POST routes use (some may take a Pydantic model or a plain dict). Use whichever the file already uses for `/hydrate` and `/create`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_app.py -k promote -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/xgraph_gateway/app.py backend/xgraph_gateway/adapters/fake.py backend/tests/test_app.py
git commit -m "feat(promote): POST /promote_columns gateway route (engine-gated, uniform errors)"
```

---

### Task 5: `gateway.js` client method

**Files:**
- Modify: `frontend/gateway.js`
- Test: `frontend/tests/test_client.mjs`

**Interfaces:**
- Consumes: existing `postJSONWithAuth` / `withSessionOrEngine` helpers in `gateway.js`.
- Produces: client method `promoteColumns(graph, source, key, columns)` that POSTs to `/promote_columns` with `{graph, source, key, columns}` (plus session/engine per the existing helper convention) and returns the parsed response.

- [ ] **Step 1: Write the failing test**

Add to `frontend/tests/test_client.mjs` (follow the existing injected-fake-`fetch` pattern used by the other client tests):

```javascript
// promoteColumns posts the right body and returns the response
{
  let captured = null;
  const fakeFetch = async (url, opts) => {
    captured = { url, body: JSON.parse(opts.body) };
    return { ok: true, status: 200, json: async () => ({
      promoted: ["party:party_name"], nodes_matched: 2, properties_set: 2,
      source: "vertexes.parquet", key: "NODE" }) };
  };
  const client = makeClient("http://gw", "falkordb", { fetch: fakeFetch });
  const res = await client.promoteColumns(
    "g1", "vertexes.parquet", "NODE", ["party:party_name"]);
  assert(captured.url.endsWith("/promote_columns"), "hits /promote_columns");
  assert(captured.body.graph === "g1", "sends graph");
  assert(captured.body.source === "vertexes.parquet", "sends source");
  assert(captured.body.key === "NODE", "sends key");
  assert(JSON.stringify(captured.body.columns) === '["party:party_name"]',
         "sends columns");
  assert(res.nodes_matched === 2, "returns response");
}
```

> Match the exact `makeClient(...)` signature and the fake-`fetch` injection mechanism used by the existing tests in this file (the third arg / options shape may differ — copy a neighboring test's setup verbatim).

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && node tests/test_client.mjs`
Expected: FAIL — `client.promoteColumns is not a function`.

- [ ] **Step 3: Add the client method**

In `gateway.js`, add alongside the other client methods (e.g. next to `hydrate`/`runQuery`), using the same helper the neighbors use:

```javascript
    promoteColumns: function (graph, source, key, columns) {
      return postJSONWithAuth("/promote_columns",
        withSessionOrEngine({ graph, source, key, columns }));
    },
```

> Use whatever body-wrapping helper the sibling methods use so session/engine are attached consistently. If siblings call `postJSONWithAuth("/x", { ... })` directly without `withSessionOrEngine`, match that instead.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && node tests/test_client.mjs`
Expected: PASS (all client tests, including the new one).

- [ ] **Step 5: Commit**

```bash
git add frontend/gateway.js frontend/tests/test_client.mjs
git commit -m "feat(promote): gateway.js promoteColumns() client method"
```

---

### Task 6: Query-panel "Promote columns" UI + version bump

**Files:**
- Modify: `frontend/XGraph.html`

**Interfaces:**
- Consumes: `gwClient.promoteColumns(...)` (Task 5), the existing `/columns` fetch (`gwClient.listColumns(source)` or the equivalent already used by the Create panel's column autocomplete), `HYDRATE_SOURCE` const, `engine` state, `activeGraph` state.
- Produces: a collapsible "Promote columns" control inside the Query panel, gated to `engine === 'falkordb'`.

- [ ] **Step 1: Add the control (anchored search-and-replace)**

Add a collapsible section in the Query panel (find the Query panel's JSX by an existing verbatim anchor — e.g. the Run button or the tab strip — and insert adjacent to it). The control has:
- A source text input defaulting to `HYDRATE_SOURCE`.
- A key text input defaulting to `"NODE"`.
- A column multi-select populated by fetching columns for the entered source (reuse the same `/columns` call the Create panel uses; if the source is blank/unreadable, show an empty list — don't crash).
- A "Promote" button that calls `gwClient.promoteColumns(activeGraph, source, key, selectedColumns)`.
- A result line showing `nodes_matched` / `properties_set` and, for each promoted column, a copy-paste hint with the exact backtick-quoted Cypher form, e.g.:
  `` Filter with: WHERE n.`party:party_name` = '…'  (snapshot of vertexes.parquet) ``
- The whole section wrapped so it only renders when `engine === 'falkordb'`.

Example JSX skeleton (adapt to the file's existing component style, state hooks, and CSS classes — the file uses `React.useState` via the global `React`):

```jsx
{engine === 'falkordb' && (
  <div className="promote-columns">
    <button onClick={() => setPromoteOpen(o => !o)}>
      {promoteOpen ? '▾' : '▸'} Promote columns into FalkorDB
    </button>
    {promoteOpen && (
      <div className="promote-body">
        <input value={promoteSource}
               onChange={e => setPromoteSource(e.target.value)}
               placeholder="source (e.g. vertexes.parquet)" />
        <input value={promoteKey}
               onChange={e => setPromoteKey(e.target.value)}
               placeholder="key (NODE)" />
        <select multiple value={promoteCols}
                onChange={e => setPromoteCols(
                  Array.from(e.target.selectedOptions, o => o.value))}>
          {promoteColOptions.map(c => <option key={c} value={c}>{c}</option>)}
        </select>
        <button disabled={!promoteCols.length || !activeGraph}
                onClick={runPromote}>Promote</button>
        {promoteResult && (
          <div className="promote-result">
            Matched {promoteResult.nodes_matched} nodes,
            set {promoteResult.properties_set} properties.
            {promoteResult.promoted.map(c => (
              <div key={c}><code>WHERE n.`{c}` = '…'</code></div>
            ))}
            <div className="hint">snapshot of {promoteResult.source}</div>
          </div>
        )}
      </div>
    )}
  </div>
)}
```

With the state + handler (place near the Query panel's other hooks):

```jsx
const [promoteOpen, setPromoteOpen] = React.useState(false);
const [promoteSource, setPromoteSource] = React.useState(HYDRATE_SOURCE);
const [promoteKey, setPromoteKey] = React.useState('NODE');
const [promoteCols, setPromoteCols] = React.useState([]);
const [promoteColOptions, setPromoteColOptions] = React.useState([]);
const [promoteResult, setPromoteResult] = React.useState(null);

// Load column options when the source changes (reuse existing /columns call).
React.useEffect(() => {
  if (engine !== 'falkordb' || !promoteSource) { setPromoteColOptions([]); return; }
  let live = true;
  gwClient.listColumns(promoteSource)
    .then(cols => { if (live) setPromoteColOptions(
      (cols || []).map(c => c.name || c)); })
    .catch(() => { if (live) setPromoteColOptions([]); });
  return () => { live = false; };
}, [engine, promoteSource]);

const runPromote = async () => {
  try {
    const res = await gwClient.promoteColumns(
      activeGraph, promoteSource, promoteKey, promoteCols);
    setPromoteResult(res);
  } catch (e) {
    setPromoteResult({ nodes_matched: 0, properties_set: 0, promoted: [],
      source: promoteSource, error: String(e) });
  }
};
```

> `gwClient.listColumns` is illustrative — use the **actual** method/name the Create panel already uses to hit `/columns`. Grep `XGraph.html` for the existing column-fetch call and reuse it verbatim.

- [ ] **Step 2: Bump the version**

Find `EXPLORER_VERSION` near the top of `XGraph.html` and bump it one patch level above the current value (grep for `EXPLORER_VERSION =`).

- [ ] **Step 3: Validate the transpile + server**

Run the Babel transpile check the repo uses (grep the frontend README/CLAUDE for the exact `@babel/standalone` Node one-liner or `ESBUILD_OK` check), then:

```bash
cd frontend && node tests/test_transforms.mjs && node tests/test_client.mjs
```
Expected: transpile OK (no syntax error), both Node test files PASS.

Then confirm the gateway serves the page (start it if needed per CLAUDE.md, `./xgraph restart`):

```bash
curl -s -o /dev/null -w '%{http_code}\n' localhost:8090/
```
Expected: `200`.

- [ ] **Step 4: Commit**

```bash
git add frontend/XGraph.html
git commit -m "feat(promote): Query-panel Promote-columns control (FalkorDB); vX.Y.Z"
```

---

### Task 7: Full-suite regression gate (NO regression to the extraction path)

**Files:** none modified — verification only.

- [ ] **Step 1: Confirm the extraction/upsert path is byte-for-byte unchanged**

Run: `git diff <feature-base>..HEAD -- backend/xgraph_gateway/adapters/falkordb_adapter.py`
Expected: the diff shows ONLY the two additive blocks (`build_promote_cypher`, `promote_columns`). `_upsert_statements`, `build_ingest_cypher`, `ingest_elements`, `_valid_nodes`, `_valid_edges`, `_cypher_ident` appear nowhere in the diff. If any of them appear, revert that change — the constraint is violated.

- [ ] **Step 2: Re-run the extraction/upsert tests**

Run: `cd backend && ./.venv/bin/python -m pytest tests/ -k "ingest or upsert or extract or create" -v`
Expected: all PASS (unchanged from before the feature). These prove the text→graph build path did not regress.

- [ ] **Step 3: Run the full backend suite**

Run: `cd backend && ./.venv/bin/python -m pytest tests/ -v`
Expected: all pass except pre-existing live-engine SKIPs (FalkorDB/Kinetica unreachable) — the promote live test SKIPs too if FalkorDB is down. No NEW failures versus the baseline.

- [ ] **Step 4: Run the frontend Node tests**

Run: `cd frontend && node tests/test_transforms.mjs && node tests/test_client.mjs`
Expected: all PASS.

- [ ] **Step 5: Commit (docs/notes only, if any)**

No code changes in this task. If a run log or note is worth keeping, add it under `docs/`; otherwise this is a verification checkpoint with nothing to commit.

---

## Manual Acceptance (headline, user-mandated)

Not an automated test — the real acceptance the user asked for, run in the browser against the **live banking graph on FalkorDB**:

1. Start the gateway (`./xgraph restart`), open `http://localhost:8090/`, Connect to FalkorDB, and select/load the banking graph (built from `vertexes.parquet` + edges via Create, or an existing FalkorDB banking graph).
2. In the Query panel, open **Promote columns**, source = `vertexes.parquet`, key = `NODE`, select a wide column that is NOT in the skinny graph (e.g. `party:party_name`), click **Promote**. Confirm the result line shows a non-zero `nodes_matched` / `properties_set`.
3. Run a mid-traversal Cypher filter on the promoted column, e.g.:
   ``MATCH (n) WHERE n.`party:party_name` = '<a real value>' RETURN n.NODE LIMIT 25``
   Confirm it returns the expected rows (before promotion the same query returned nothing because the property was absent/NULL). This is the definition of done.

---

## Self-Review

- **Spec coverage:** data flow (Tasks 1–4), verbatim naming (Task 2 `attrs` map + Task 6 backtick hint), MATCH-only/label-agnostic (Task 2/Task 3 live test), null-skip (Task 2 + live test), whole-column scope (Task 1), FalkorDB-only + Kinetica/base unsupported (Task 3), endpoint contract + engine-gating 400 + empty-columns 400 (Task 4), client method (Task 5), Query-panel UI + version bump (Task 6), snapshot/staleness hint (Task 6). Both user constraints: no-regression (Global Constraints + Task 7), banking-graph mid-traversal acceptance (Task 3 live test + Manual Acceptance).
- **Placeholder scan:** all code steps carry real code; illustrative names (`listColumns`, `config.load_settings()` fields, `makeClient` signature, route-decorator style) are flagged with an explicit "use the repo's actual X" note because the exact local symbol must be copied from neighboring code — these are integration seams, not placeholders.
- **Type consistency:** `read_columns(source, key, columns)` → list[dict]; `build_promote_cypher(rows, key, columns, batch_size)` → list[(cypher, {"rows":[{"id","attrs"}]})]; `promote_columns(graph, source, key, columns)` → `{promoted, nodes_matched, properties_set, source, key}` — consistent across Tasks 1→3→4→5→6.
