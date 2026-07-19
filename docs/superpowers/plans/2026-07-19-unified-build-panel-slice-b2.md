# Unified "Build" Panel — Slice B2 Implementation Plan (generalize the structured builder beyond Kinetica)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the structured graph builder (`CreateHelperPanel`, shipped live for Kinetica in B1) work for **DuckDB→FalkorDB** too, by giving the gateway engine-neutral **table-list** (`GET /tables`) and **column-list** (`GET /columns`) sources, threading them into the frontend so the section dropdowns/autocomplete populate for every engine, and mounting the builder in `CreatePanel`'s non-Kinetica branch with a **per-engine build call** (Kinetica → DDL as today; DuckDB/FalkorDB → the existing `/create` `{tables,nodes,edges}` spec).

**Architecture:** Backend adds two read endpoints and one adapter method per engine (`list_tables()`, `list_columns(table)`), mirroring the existing `_resolve_adapter(session, engine)` + `_err(engine, e)` FastAPI style. Kinetica uses its existing `show_table` primitive; DuckDB uses `SHOW TABLES` / `DESCRIBE` on a per-call connection; FalkorDB (built from DuckDB Parquet sources) reuses the DuckDB compute path over the registered source files. The frontend (single-file `frontend/XGraph.html`) fetches `/tables` into the already-declared `tables` state, routes column autocomplete through the new `/columns` client method for non-Kinetica engines (Kinetica keeps its direct `/get/records` path), adds a static DuckDB/FalkorDB grammar, and mounts `CreateHelperPanel` for non-Kinetica with an `onGenerate` that assembles a `/create` spec instead of DDL.

**Tech Stack:** Backend — FastAPI + the vendored `graph_loader` + embedded DuckDB (`backend/.venv`, run pytest from `backend/`). Frontend — React 18 UMD + Babel-standalone (no build step); JSX validated by the local `esbuild` check; behavior is browser-driven (CLAUDE.md: the React app cannot be runtime-verified headlessly). `gateway.js` client + transforms are Node-tested.

## Global Constraints

- **No `git commit` unless the user authorizes it** (CLAUDE.md). This work stream has been committing locally per the Slice-A/B1 convention; keep commits local unless told otherwise. In a background job the commits land on the worktree branch.
- **Self-contained backend** — no `sys.path` hacks, no imports from `../falkor` / `../graphrag`; `graph_loader` is vendored at `backend/graph_loader/` (CLAUDE.md).
- **DuckDB returns DECIMAL as Python `Decimal`** — coerce to `float` before returning to a client (reuse `graph_loader.duckdb_source.coerce_row` / `coerce_value`, as `ComputeEngine` already does).
- **Never put `ORDER BY` in paged Kinetica SQL** (CLAUDE.md) — irrelevant here (we use `show_table`, not paged SQL), but do not introduce it.
- **Table/column endpoints degrade to an empty list on an unreachable engine** (autocomplete just has no suggestions); they must NEVER raise in a way that blocks manual `schema.table.column` entry. The uniform error envelope still applies to genuinely bad requests.
- **Backend tests:** run from `backend/` with `./.venv/bin/python -m pytest tests/ -v`. DuckDB tests are embedded (no skip). Kinetica/FalkorDB live tests **SKIP** when the engine is unreachable (existing `_adapter_or_skip()` / `_task8_adapter_or_skip()` pattern). FakeAdapter gateway tests use `create_app(adapter_factory=lambda e: FakeAdapter())`.
- **Every frontend edit is validated by the esbuild JSX check** (command below); it must print `ESBUILD_OK` before each frontend commit. `gateway.js` changes are validated by `node tests/test_client.mjs`.
- **Version badge:** bump `EXPLORER_VERSION` from `0.5.0` to `0.6.0` in the final frontend task.
- Commit messages: concise 1–2 lines, no `Co-Authored-By` footer.

### Frontend esbuild JSX check (the frontend "run the test" step)

```bash
cd /home/kkaramete/xgraph/frontend
end=$(grep -n '</script>' XGraph.html | tail -1 | cut -d: -f1)
sed -n "47,$((end-1))p" XGraph.html | ./node_modules/.bin/esbuild --loader=jsx > /dev/null && echo ESBUILD_OK || echo ESBUILD_FAIL
```

Expected on success: `ESBUILD_OK`. (In a worktree without `node_modules`, use the primary checkout's binary: `/home/kkaramete/xgraph/frontend/node_modules/.bin/esbuild`.)

---

## File Structure

- **Backend:**
  - `backend/xgraph_gateway/adapters/base.py` — add two optional (non-abstract) methods `list_tables()` and `list_columns(table)` with safe default `return []` so existing adapters keep working (Task 1).
  - `backend/xgraph_gateway/adapters/fake.py` — canned `list_tables`/`list_columns` for gateway tests (Task 1).
  - `backend/xgraph_gateway/adapters/kinetica_adapter.py` — `list_tables()` (via `show_table("")`) + `list_columns(table)` (reuse `_current_columns`) (Task 2).
  - `backend/xgraph_gateway/adapters/falkordb_adapter.py` — `list_tables()` + `list_columns(table)` delegating to a DuckDB compute helper over the source Parquet (Task 3).
  - `backend/xgraph_gateway/compute/duckdb_engine.py` — `list_tables()` (`SHOW TABLES` on a persistent-per-call connection over registered views — here, list nothing by default; used by FalkorDB path as `describe_source`-style helper) + `describe_relation(name)` (`DESCRIBE`) (Task 3).
  - `backend/xgraph_gateway/app.py` — `GET /tables` + `GET /columns` endpoints (Task 4).
  - `backend/tests/test_tables_columns.py` — new test file (Tasks 1–4).
- **Frontend:**
  - `frontend/gateway.js` — client methods `tables()` and `columns(table)` (Task 5).
  - `frontend/tests/test_client.mjs` — extend with the two new methods (Task 5).
  - `frontend/XGraph.html`:
    - App: populate `tables` state from `/tables` (Task 6); a `fetchTableColumnsGW(table)` that routes through `gwClient.columns` for non-Kinetica (Task 6); a static `DUCKDB_GRAMMAR` + per-engine grammar selection (Task 7); mount `<CreateHelperPanel>` in `CreatePanel`'s non-Kinetica branch with a spec-emitting `onGenerate` (Task 7); version bump (Task 7).

No backend file is deleted. `/register_file` and Kinetica live `/show/graph/grammar` are **explicitly deferred** (see "Deferred / follow-up plan").

---

## Task 1: Adapter base defaults + FakeAdapter `list_tables`/`list_columns` + `/tables`·`/columns` gateway tests (red)

**Files:**
- Modify: `backend/xgraph_gateway/adapters/base.py` (add two concrete default methods).
- Modify: `backend/xgraph_gateway/adapters/fake.py` (canned implementations).
- Create: `backend/tests/test_tables_columns.py`.

**Interfaces:**
- Produces:
  - `GraphEngineAdapter.list_tables(self) -> list[dict]` — each item `{"name": str, "type": str}` (`type` is engine-specific, e.g. `"table"`, `"view"`, `"external"`, `"collection"`); default `return []`.
  - `GraphEngineAdapter.list_columns(self, table: str) -> list[str]` — column names for `table`; default `return []`.
  - `FakeAdapter.list_tables()` → `[{"name": "expero.vertexes", "type": "table"}, {"name": "expero.edges", "type": "table"}]`.
  - `FakeAdapter.list_columns(table)` → `["NODE", "NODE_LABEL", "AMOUNT"]` for any table, `[]` for a table named `"missing"`.
- Consumes: nothing new.

**Context:** The base defaults make the two methods safe to call on every adapter (Kinetica/FalkorDB get real impls in Tasks 2–3; any other adapter degrades to `[]`). FakeAdapter's canned data lets the gateway endpoints (Task 4) be tested without a live engine, mirroring `test_app.py`'s FakeAdapter pattern.

- [ ] **Step 1: Add default methods to `GraphEngineAdapter`**

In `backend/xgraph_gateway/adapters/base.py`, after the existing `creation_statement` method (≈L67-79), add (these are **concrete**, not `@abstractmethod`, so subclasses need not override):

```python
    def list_tables(self) -> list[dict]:
        """List tables/relations usable as builder section sources.

        Each item is {"name": str, "type": str}. Default: no introspection
        (empty list) so the builder degrades to manual table entry.
        """
        return []

    def list_columns(self, table: str) -> list[str]:
        """Column names for a table/relation (for builder autocomplete).

        Default: no introspection (empty list). Never raises for an unknown
        table — returns [] so manual column entry still works.
        """
        return []
```

- [ ] **Step 2: Implement canned versions on `FakeAdapter`**

In `backend/xgraph_gateway/adapters/fake.py`, add to `class FakeAdapter` (after `delete_graph`):

```python
    def list_tables(self):
        return [
            {"name": "expero.vertexes", "type": "table"},
            {"name": "expero.edges", "type": "table"},
        ]

    def list_columns(self, table):
        if table == "missing":
            return []
        return ["NODE", "NODE_LABEL", "AMOUNT"]
```

- [ ] **Step 3: Write the failing gateway tests**

Create `backend/tests/test_tables_columns.py`:

```python
from fastapi.testclient import TestClient
from xgraph_gateway.app import create_app
from xgraph_gateway.adapters.fake import FakeAdapter


def _client():
    return TestClient(create_app(adapter_factory=lambda e: FakeAdapter()))


def test_tables_lists_relations():
    r = _client().get("/tables", params={"engine": "fake"})
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    names = [t["name"] for t in body]
    assert "expero.vertexes" in names
    assert all("type" in t for t in body)


def test_columns_lists_column_names():
    r = _client().get("/columns", params={"engine": "fake", "table": "expero.vertexes"})
    assert r.status_code == 200
    assert r.json() == ["NODE", "NODE_LABEL", "AMOUNT"]


def test_columns_unknown_table_returns_empty_list():
    r = _client().get("/columns", params={"engine": "fake", "table": "missing"})
    assert r.status_code == 200
    assert r.json() == []


def test_columns_requires_table_param():
    # FastAPI returns 422 when a required query param is absent.
    r = _client().get("/columns", params={"engine": "fake"})
    assert r.status_code == 422
```

- [ ] **Step 4: Run the tests to verify they FAIL**

```bash
cd /home/kkaramete/xgraph/backend
./.venv/bin/python -m pytest tests/test_tables_columns.py -v
```
Expected: FAIL — the `/tables` and `/columns` routes don't exist yet (404), so `test_tables_lists_relations` / `test_columns_*` fail. (The endpoints arrive in Task 4; this red state is expected now.)

- [ ] **Step 5: Commit**

```bash
cd /home/kkaramete/xgraph
git add backend/xgraph_gateway/adapters/base.py backend/xgraph_gateway/adapters/fake.py backend/tests/test_tables_columns.py
git commit -m "test(build): base list_tables/list_columns defaults + FakeAdapter + failing /tables·/columns tests"
```

---

## Task 2: Kinetica `list_tables` / `list_columns`

**Files:**
- Modify: `backend/xgraph_gateway/adapters/kinetica_adapter.py` (add two methods to `KineticaAdapter`, ≈after `_current_columns` at L890).
- Modify: `backend/tests/test_tables_columns.py` (add live-skip Kinetica tests).

**Interfaces:**
- Consumes: existing `self._db` (GPUdb) and `self._current_columns(table)` (L890, returns `list[str]` via `show_table` `get_column_info`).
- Produces: `KineticaAdapter.list_tables()` and `KineticaAdapter.list_columns(table)` conforming to the Task-1 shapes.

**Context:** Kinetica already introspects a single table's columns via `_current_columns` (parses `show_table` `type_schemas[0]` JSON). For the table list, `show_table(table_name="", options={"show_children":"true"})` returns `table_names` (all tables/collections). We map each to `{"name", "type"}` using the `additional_info`/`table_descriptions` when present, else `"table"`.

- [ ] **Step 1: Add the two methods**

In `backend/xgraph_gateway/adapters/kinetica_adapter.py`, inside `class KineticaAdapter`, add right after `_current_columns` (≈L906):

```python
    def list_tables(self):
        """All Kinetica tables/relations (name + coarse type)."""
        try:
            resp = self._db.show_table(
                table_name="",
                options={"show_children": "true", "no_error_if_not_exists": "true"},
            )
        except Exception:
            return []
        names = resp.get("table_names", []) or []
        descs = resp.get("table_descriptions", []) or []
        out = []
        for i, name in enumerate(names):
            if not name:
                continue
            d = descs[i] if i < len(descs) else []
            t = "collection" if "COLLECTION" in d else ("view" if "VIEW" in d or "MATERIALIZED_VIEW" in d else "table")
            out.append({"name": name, "type": t})
        return out

    def list_columns(self, table):
        """Column names for a Kinetica table (empty list if it doesn't exist)."""
        try:
            return self._current_columns(table)
        except Exception:
            return []
```

- [ ] **Step 2: Add live-skip tests**

Append to `backend/tests/test_tables_columns.py` (reuse the module's Kinetica skip idiom from `test_kinetica_adapter.py`):

```python
import pytest
from xgraph_gateway import config


def _kinetica_or_skip():
    try:
        from xgraph_gateway.adapters.kinetica_adapter import KineticaAdapter
        a = KineticaAdapter(config.load_settings())
        a.list_graphs()
        return a
    except Exception as e:
        pytest.skip(f"Kinetica unreachable: {e}")


def test_kinetica_list_tables_shape():
    a = _kinetica_or_skip()
    tables = a.list_tables()
    assert isinstance(tables, list)
    for t in tables:
        assert set(t.keys()) >= {"name", "type"}


def test_kinetica_list_columns_of_first_table():
    a = _kinetica_or_skip()
    tables = [t for t in a.list_tables() if t["type"] == "table"]
    if not tables:
        pytest.skip("no base tables present")
    cols = a.list_columns(tables[0]["name"])
    assert isinstance(cols, list)
```

- [ ] **Step 3: Run tests (unit pass; Kinetica tests pass or skip)**

```bash
cd /home/kkaramete/xgraph/backend
./.venv/bin/python -m pytest tests/test_tables_columns.py -v
```
Expected: the FakeAdapter tests still FAIL until Task 4 (routes missing); the two Kinetica tests PASS if Kinetica is up, else SKIP. (This task adds no route, so the Fake HTTP tests remain red — that's fine; they go green in Task 4.)

- [ ] **Step 4: Commit**

```bash
cd /home/kkaramete/xgraph
git add backend/xgraph_gateway/adapters/kinetica_adapter.py backend/tests/test_tables_columns.py
git commit -m "feat(build): Kinetica list_tables/list_columns (show_table introspection)"
```

---

## Task 3: FalkorDB / DuckDB `list_tables` / `list_columns`

**Files:**
- Modify: `backend/xgraph_gateway/compute/duckdb_engine.py` (add `describe_relation(source)`; note `describe_source` already exists for files).
- Modify: `backend/xgraph_gateway/adapters/falkordb_adapter.py` (add `list_tables`/`list_columns`).
- Modify: `backend/tests/test_tables_columns.py` (embedded DuckDB tests — no skip).

**Interfaces:**
- Consumes: `ComputeEngine.describe_source(source)` (existing, L174-183 — `DESCRIBE SELECT * FROM '<file>'`, returns column names, guards single-quote injection) and `graph_loader.config.resolve_data_path`.
- Produces:
  - `DuckDBComputeEngine.describe_relation(self, source: str) -> list[str]` — alias/thin wrapper around the file-describe so the FalkorDB adapter has one call for "columns of this Parquet source".
  - `FalkorDBAdapter.list_tables()` — FalkorDB graphs are built from DuckDB Parquet sources, which the gateway does not enumerate server-side; return `[]` (the builder falls back to manual `path.parquet` entry). Documented, not a gap.
  - `FalkorDBAdapter.list_columns(table)` — treat `table` as a Parquet source path (bare name resolved via `resolve_data_path`), return its columns via the compute engine; `[]` if unreadable.

**Context:** For FalkorDB the "tables" are Parquet/CSV **files** turned into DuckDB relations at build time (`falkordb_adapter.load_graph` → `DuckDBSource.connect(tables)`). There is no persistent catalog to list, so `list_tables()` is `[]` (B2 keeps manual file entry; `/register_file` in the follow-up plan will make files first-class). But `list_columns(path)` is fully answerable by DESCRIBE-ing the file, which is exactly what powers autocomplete once the user types a source path.

- [ ] **Step 1: Add `describe_relation` to the compute engine**

In `backend/xgraph_gateway/compute/duckdb_engine.py`, add to `class DuckDBComputeEngine` right after `describe_source` (≈L183):

```python
    def describe_relation(self, source):
        """Columns of a relation identified by a file path/source name.

        Thin wrapper over describe_source so callers (adapters) have a
        single 'columns of this source' entry point. Returns [] on error.
        """
        try:
            return self.describe_source(source)
        except Exception:
            return []
```

- [ ] **Step 2: Add `list_tables`/`list_columns` to `FalkorDBAdapter`**

In `backend/xgraph_gateway/adapters/falkordb_adapter.py`, add to `class FalkorDBAdapter` (after `load_graph`, ≈L484). Import the compute engine + `resolve_data_path` at module top if not already present (they are used elsewhere; add `from xgraph_gateway.compute.duckdb_engine import DuckDBComputeEngine` and `from graph_loader.config import resolve_data_path` if missing):

```python
    def list_tables(self):
        """FalkorDB graphs are built from DuckDB Parquet sources; the gateway
        does not enumerate them server-side (no catalog). Return [] — the
        builder falls back to manual source-path entry. /register_file
        (follow-up) will make file relations first-class."""
        return []

    def list_columns(self, table):
        """Columns of a Parquet/CSV source (bare names resolve via
        resolve_data_path). Powers autocomplete once the user types a path."""
        try:
            src = resolve_data_path(table)
        except Exception:
            src = table
        return DuckDBComputeEngine().describe_relation(src)
```

- [ ] **Step 3: Add embedded DuckDB tests (no skip)**

Append to `backend/tests/test_tables_columns.py`:

```python
import duckdb
from xgraph_gateway.compute.duckdb_engine import DuckDBComputeEngine


def test_describe_relation_returns_columns(tmp_path):
    p = tmp_path / "v.parquet"
    con = duckdb.connect()
    con.execute(
        "COPY (SELECT 1 AS node, 'bank' AS node_label, 12.5 AS amount) "
        f"TO '{p}' (FORMAT PARQUET)"
    )
    con.close()
    cols = DuckDBComputeEngine().describe_relation(str(p))
    assert cols == ["node", "node_label", "amount"]


def test_describe_relation_missing_file_returns_empty():
    assert DuckDBComputeEngine().describe_relation("/no/such/file.parquet") == []


def test_falkordb_list_columns_reads_parquet(tmp_path):
    from xgraph_gateway.adapters.falkordb_adapter import FalkorDBAdapter
    p = tmp_path / "e.parquet"
    con = duckdb.connect()
    con.execute(f"COPY (SELECT 1 AS src, 2 AS dst) TO '{p}' (FORMAT PARQUET)")
    con.close()
    # Construct without connecting (list_columns doesn't touch FalkorDB).
    a = FalkorDBAdapter.__new__(FalkorDBAdapter)
    cols = a.list_columns(str(p))
    assert cols == ["src", "dst"]


def test_falkordb_list_tables_is_empty():
    from xgraph_gateway.adapters.falkordb_adapter import FalkorDBAdapter
    a = FalkorDBAdapter.__new__(FalkorDBAdapter)
    assert a.list_tables() == []
```

- [ ] **Step 4: Run the DuckDB/FalkorDB tests**

```bash
cd /home/kkaramete/xgraph/backend
./.venv/bin/python -m pytest tests/test_tables_columns.py -k "describe_relation or falkordb" -v
```
Expected: all four PASS (embedded, no skip). (The Fake HTTP tests are still red until Task 4.)

- [ ] **Step 5: Commit**

```bash
cd /home/kkaramete/xgraph
git add backend/xgraph_gateway/compute/duckdb_engine.py backend/xgraph_gateway/adapters/falkordb_adapter.py backend/tests/test_tables_columns.py
git commit -m "feat(build): FalkorDB/DuckDB list_columns via DESCRIBE (list_tables=[] for now)"
```

---

## Task 4: `GET /tables` + `GET /columns` gateway endpoints (green)

**Files:**
- Modify: `backend/xgraph_gateway/app.py` (add two GET endpoints next to `/entities`/`/record`, ≈L144-157).

**Interfaces:**
- Consumes: `_resolve_adapter(session, engine)` (L64), `_err(engine, e)` (L23), the adapter `list_tables()`/`list_columns(table)` from Tasks 1–3.
- Produces: `GET /tables?engine=&session=` → `list[{"name","type"}]`; `GET /columns?engine=&session=&table=` → `list[str]`.

**Context:** Mirror the existing GET style exactly (`/schema`, `/entities`, `/record`). `table` is a required query param on `/columns` (FastAPI → 422 when absent, matching Task 1's `test_columns_requires_table_param`). Both wrap in try/except → `_err`, but adapters already swallow introspection failures to `[]`, so the happy path returns a list even on a partially-unreachable engine.

- [ ] **Step 1: Add the endpoints**

In `backend/xgraph_gateway/app.py`, after the `/record` endpoint (≈L157), add:

```python
    @app.get("/tables")
    def tables(engine: str = "", session: str | None = None):
        try:
            return _resolve_adapter(session, engine).list_tables()
        except Exception as e:
            return _err(engine, e)

    @app.get("/columns")
    def columns(table: str, engine: str = "", session: str | None = None):
        try:
            return _resolve_adapter(session, engine).list_columns(table)
        except Exception as e:
            return _err(engine, e)
```

(Indentation matches the other `@app.get` functions defined inside `create_app`.)

- [ ] **Step 2: Run the full new test file — everything green (or Kinetica-skip)**

```bash
cd /home/kkaramete/xgraph/backend
./.venv/bin/python -m pytest tests/test_tables_columns.py -v
```
Expected: all FakeAdapter HTTP tests PASS, the DuckDB/FalkorDB tests PASS, the two Kinetica tests PASS-or-SKIP.

- [ ] **Step 3: Run the whole backend suite (no regressions)**

```bash
cd /home/kkaramete/xgraph/backend
./.venv/bin/python -m pytest tests/ -q
```
Expected: the prior green count + the new tests, no failures (live tests may SKIP).

- [ ] **Step 4: Commit**

```bash
cd /home/kkaramete/xgraph
git add backend/xgraph_gateway/app.py
git commit -m "feat(build): GET /tables and GET /columns gateway endpoints"
```

---

## Task 5: `gateway.js` client methods `tables()` + `columns(table)` + Node tests

**Files:**
- Modify: `frontend/gateway.js` (add two methods in the `makeClient` return object, ≈L138-149).
- Modify: `frontend/tests/test_client.mjs` (assert the two new methods hit the right URLs).

**Interfaces:**
- Consumes: the client's existing `q(path)` helper (L77 — appends `session`+`engine` to a GET query string) and `getJSON(url)` (L94).
- Produces: `client.tables()` → `GET /tables?...`; `client.columns(table)` → `GET /columns?table=<enc>&...`.

**Context:** `q("/columns?table=" + encodeURIComponent(table))` — `q` appends `&session=&engine=` after the existing query string, so pre-adding `?table=` is correct. Mirror `fetchEntities`/`getRecord` which already pass extra query params.

- [ ] **Step 1: Add the two client methods**

In `frontend/gateway.js`, inside the object returned by `makeClient` (after `sourcePreview`, ≈L144), add:

```javascript
        tables: function () { return getJSON(q('/tables')); },
        columns: function (table) { return getJSON(q('/columns?table=' + encodeURIComponent(table))); },
```

- [ ] **Step 2: Write the failing Node test**

In `frontend/tests/test_client.mjs`, add (using the file's existing injected-fetch harness — follow the pattern already used to test `getSchema`/`fetchEntities`; capture the URL passed to the fake `fetch`):

```javascript
// --- /tables and /columns -------------------------------------------------
{
    const calls = [];
    const client = makeClient('http://gw', 'duckdb', {
        fetch: async function (url) { calls.push(url); return { ok: true, json: async () => [] }; }
    });
    await client.tables();
    await client.columns('expero.vertexes');
    assert(calls[0].includes('/tables'), '/tables URL');
    assert(calls[0].includes('engine=duckdb'), '/tables carries engine');
    assert(calls[1].includes('/columns?table=expero.vertexes'), '/columns table param');
    assert(calls[1].includes('engine=duckdb'), '/columns carries engine');
    console.log('ok: tables/columns client methods');
}
```

(If `makeClient`'s fetch-injection signature differs, match the exact form already used earlier in this test file — the point is: call both methods, assert the recorded URLs.)

- [ ] **Step 3: Run the client tests**

```bash
cd /home/kkaramete/xgraph/frontend
node tests/test_client.mjs
```
Expected: all existing assertions plus `ok: tables/columns client methods`, exit 0.

- [ ] **Step 4: Commit**

```bash
cd /home/kkaramete/xgraph
git add frontend/gateway.js frontend/tests/test_client.mjs
git commit -m "feat(build): gateway.js tables()/columns() client methods + Node tests"
```

---

## Task 6: Frontend — populate `tables` state from `/tables` + gateway-routed column autocomplete

**Files:**
- Modify: `frontend/XGraph.html` — App component: `tables` state population (the `setTables` call site, currently only reset at ≈L8543), and a new `fetchTableColumnsGW(table)` that routes through `gwClient.columns` for non-Kinetica while keeping Kinetica's existing direct `/get/records` path (`fetchTableColumns`, ≈L7977-7993).

**Interfaces:**
- Consumes: `gwClient.tables()` / `gwClient.columns(table)` (Task 5); existing App state `tables`/`setTables`, `tableColumnsCache`/`setTableColumnsCache`, `graphEngine`, `credentials`.
- Produces: a populated `tables` state after connect/graph-select; a unified `fetchTableColumns` that works for every engine.

**Context:** Today `setTables` is only ever called with `[]` and `fetchTableColumns` hits Kinetica directly. B2 (a) loads the table list when a connection/graph is active, and (b) makes column autocomplete engine-neutral: Kinetica keeps the fast direct probe (it needs no gateway round-trip and works today), DuckDB/FalkorDB use the new `/columns` endpoint.

- [ ] **Step 1: Load `/tables` when connected**

Find the effect/handler that runs after a successful connect or `setActiveGraph` (search for `setActiveGraph(` in the connect flow / `loadGraphDetails`). Add a best-effort table fetch. Insert an effect near the other App effects:

```javascript
    // Populate the structured-builder table list from the gateway (best effort;
    // empty list is fine — the builder falls back to manual entry). Re-runs when
    // the engine or connection changes.
    React.useEffect(function () {
        if (!gwClient || !connected) { return; }
        var cancelled = false;
        gwClient.tables()
            .then(function (list) { if (!cancelled && Array.isArray(list)) setTables(list); })
            .catch(function () { /* leave tables as-is */ });
        return function () { cancelled = true; };
    }, [gwClient, connected, graphEngine]);
```

(Use the existing `connected` boolean; if the App uses a different name, match it. Do NOT remove the `setTables([])` reset at disconnect.)

- [ ] **Step 2: Add an engine-neutral column fetcher**

Immediately after the existing `fetchTableColumns` (≈L7993), add:

```javascript
    // Engine-neutral column autocomplete: Kinetica keeps the direct /get/records
    // probe (fetchTableColumns); DuckDB/FalkorDB go through the gateway /columns.
    function fetchTableColumnsGW(tableName) {
        if (!tableName) return;
        if (graphEngine === 'kinetica') { return fetchTableColumns(tableName); }
        if (Object.prototype.hasOwnProperty.call(tableColumnsCache, tableName)) return;
        gwClient.columns(tableName)
            .then(function (cols) {
                if (Array.isArray(cols)) {
                    setTableColumnsCache(function (m) { var n = Object.assign({}, m); n[tableName] = cols; return n; });
                }
            })
            .catch(function () { /* leave uncached — manual entry still works */ });
    }
```

- [ ] **Step 3: Route the builder's column fetch through the neutral fetcher**

In the two places that pass `onFetchTableColumns={fetchTableColumns}` to a panel that hosts `CreateHelperPanel` — the `createProps` object (≈L9480, `onFetchTableColumns: fetchTableColumns`) — change it to `onFetchTableColumns: fetchTableColumnsGW`. **Do NOT** change the `<QueryPanel …>` prop at the embedded-query render (that path is Solve/Match, Kinetica-only, and keeps `fetchTableColumns`).

- [ ] **Step 4: esbuild JSX check**

Run the Global-Constraints esbuild command. Expected: `ESBUILD_OK`.

- [ ] **Step 5: Commit**

```bash
cd /home/kkaramete/xgraph
git add frontend/XGraph.html
git commit -m "feat(build): populate tables from /tables; gateway-routed column autocomplete for non-Kinetica"
```

---

## Task 7: Frontend — static DuckDB grammar + mount `CreateHelperPanel` in the non-Kinetica branch with a spec-emitting build + version bump

**Files:**
- Modify: `frontend/XGraph.html` — add `DUCKDB_GRAMMAR` near `DEFAULT_GRAMMAR` (≈L7930); select grammar by engine where `createProps.graphGrammar` is set (≈L9480); render `<CreateHelperPanel>` in `CreatePanel`'s non-Kinetica branch (≈L7058) with an `onGenerate` that assembles a `/create` spec; `EXPLORER_VERSION` (L50) `0.5.0` → `0.6.0`.

**Interfaces:**
- Consumes: `CreateHelperPanel` (B1), `gwClient.create(spec)`, the non-Kinetica create spec shape (from `CreatePanel.handleCreate`, ≈L6946-6967): `{ graph, tables:{<tableName>:<filePath>}, nodes:[{sql,id,label_column,properties}], edges:[{sql,id,type_column,source_key,target_key,properties}], node_key_property:'NODE' }`.
- Produces: a live structured builder for DuckDB/FalkorDB that emits a `/create` spec (not DDL) and runs via the existing Create button.

**Context:** `CreateHelperPanel.chGenerate` is hard-wired to Kinetica DDL. Rather than fork the component, B2 keeps the component's **section model** (NODES/EDGES rows with a default table + `table.column` refs) and translates it to the DuckDB spec **in the host's `onGenerate`** — the component still calls `props.onGenerate(payload)`, but for non-Kinetica the payload is an object (a spec), and the host runs `gwClient.create` directly instead of stuffing DDL into a textarea. To keep the component engine-agnostic, add an optional `props.buildTarget` (`'ddl'` default | `'spec'`); when `'spec'`, `chGenerate` calls `props.onGenerate` with the **row model** (`{ graphName, directed, rows, sectionTable, options }`) and lets the host translate. This isolates the Kinetica-vs-DuckDB divergence to one prop + the host callback.

- [ ] **Step 1: Add `buildTarget` to `CreateHelperPanel.chGenerate`**

In `CreateHelperPanel` (`frontend/XGraph.html`), in `chGenerate` (≈L6686-6706), wrap the existing DDL assembly so that when `props.buildTarget === 'spec'` it emits the row model instead:

```javascript
    function chGenerate() {
        if (props.buildTarget === 'spec') {
            props.onGenerate({
                graphName: (props.graphName || 'new_graph').trim(),
                directed: chDirected,
                rows: chRows,
                sectionTable: chSectionTable,
                options: chOptions,
            });
            return;
        }
        // ── existing Kinetica DDL path (unchanged) ──
        var name = (props.graphName || 'new_graph').trim();
        // … (leave the current DDL-building body exactly as-is) …
        props.onGenerate(sql);
    }
```

(Only the early `if (props.buildTarget === 'spec')` block is new; the Kinetica DDL body below it is untouched.)

- [ ] **Step 2: Add a static DuckDB grammar**

Near `DEFAULT_GRAMMAR` (≈L7966), add a DuckDB/FalkorDB grammar (id + optional label; no Kinetica-only WKT/weights configs):

```javascript
const DUCKDB_GRAMMAR = {
    NODES: { configurations: [{ label: 'Node id', required: ['NODE'] }], optional: ['NODE_LABEL'] },
    EDGES: { configurations: [{ label: 'Edge source+target', required: ['EDGE_NODE1', 'EDGE_NODE2'] }], optional: ['EDGE_LABEL'] },
    WEIGHTS: { configurations: [], optional: [] },
    RESTRICTIONS: { configurations: [], optional: [] },
};
```

- [ ] **Step 3: Select grammar by engine in `createProps`**

Where `createProps` sets `graphGrammar: graphGrammar` (≈L9480), change it to pick per engine:

```javascript
                            graphGrammar: (graphEngine === 'kinetica' ? graphGrammar : DUCKDB_GRAMMAR),
```

- [ ] **Step 4: Mount `CreateHelperPanel` in the non-Kinetica branch**

In `CreatePanel`, the non-Kinetica branch (the `: (` else of `graphEngine === 'kinetica' ? (…)`, ≈L7058, above the Node-table form), insert:

```javascript
                    <CreateHelperPanel
                        graphGrammar={graphGrammar} tables={tables}
                        tableColumnsCache={tableColumnsCache} onFetchTableColumns={onFetchTableColumns}
                        graphName={createGraphName} setGraphName={setCreateGraphName}
                        initialMode="recreate" buildTarget="spec"
                        onGenerate={function(model){ handleBuildFromModel(model); }}
                    />
```

Then add `handleBuildFromModel` inside `CreatePanel` (near `handleCreate`, ≈L6939) — it translates the row model to the existing `/create` spec and runs it:

```javascript
    // Translate the structured builder's row model into the DuckDB→FalkorDB
    // /create spec (same shape handleCreate builds from the manual form), then
    // build. Each section row is `table.column` or a bare column joined to the
    // section's default table.
    async function handleBuildFromModel(model) {
        function ref(component, aliasWanted) {
            var rows = (model.rows[component] || []).filter(function (r) { return r.value && r.value.trim(); });
            var dfl = (model.sectionTable && model.sectionTable[component] || '').trim();
            var picked = null;
            rows.forEach(function (r) {
                var v = r.value.trim();
                var dot = v.lastIndexOf('.');
                var tbl = dot > 0 ? v.slice(0, dot) : dfl;
                var col = dot > 0 ? v.slice(dot + 1) : v;
                if (!picked && (r.id === aliasWanted || !aliasWanted)) picked = { table: tbl, column: col };
            });
            return picked;
        }
        var nodeId = ref('NODES', 'NODE');
        var e1 = ref('EDGES', 'EDGE_NODE1'), e2 = ref('EDGES', 'EDGE_NODE2');
        if (!nodeId || !e1 || !e2) { setStatus({ loading:false, error:'Need a NODES id and EDGES source+target (table.column).', text:null }); return; }
        var spec = {
            graph: model.graphName,
            tables: {},
            nodes: [{ sql: 'SELECT ' + nodeId.column + ' AS NODE FROM ' + nodeId.table, id: 'NODE', label_column: null, properties: [] }],
            edges: [{ sql: 'SELECT ' + e1.column + ' AS SRC, ' + e2.column + ' AS DST FROM ' + e1.table,
                      id: null, type_column: null, source_key: 'SRC', target_key: 'DST', properties: [] }],
            node_key_property: 'NODE',
        };
        setStatus({ loading:true, error:null, text:null });
        try {
            var res = await gwClient.create(spec);
            setStatus({ loading:false, error:null, text:'Built ' + model.graphName });
            if (onCreated) onCreated(model.graphName, spec);
            var gl = await gwClient.listGraphs(); setGraphs(gl.graphs || gl || []);
        } catch (e) { setStatus({ loading:false, error:String(e.message || e), text:null }); }
    }
```

(This is intentionally the minimal single-table-per-section translation matching the DuckDB grammar in Step 2; multi-table combos remain a Slice-C polish. The manual Node/Edge form below stays as an alternative.)

- [ ] **Step 5: Bump the version badge**

Change `EXPLORER_VERSION` (L50) from `"0.5.0"` to `"0.6.0"`.

- [ ] **Step 6: esbuild JSX check + gateway 200**

```bash
cd /home/kkaramete/xgraph/frontend
end=$(grep -n '</script>' XGraph.html | tail -1 | cut -d: -f1)
sed -n "47,$((end-1))p" XGraph.html | ./node_modules/.bin/esbuild --loader=jsx > /dev/null && echo ESBUILD_OK || echo ESBUILD_FAIL
cd /home/kkaramete/xgraph && (./xgraph status >/dev/null 2>&1 || ./xgraph start) && sleep 1 && curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8090/
```
Expected: `ESBUILD_OK` then `200`.

- [ ] **Step 7: Commit**

```bash
cd /home/kkaramete/xgraph
git add frontend/XGraph.html
git commit -m "feat(build): mount structured builder for DuckDB/FalkorDB (spec build) + DuckDB grammar; v0.6.0"
```

---

## Manual (browser) acceptance — run after Task 7

Hard-reload `http://localhost:8090/`, confirm `v0.6.0`:

1. **Kinetica unaffected:** connect Kinetica, Build → Tables/files → the NODES/EDGES builder still emits `CREATE OR REPLACE … GRAPH … INPUT_TABLES(…)` DDL into the textarea; Create/Replace still works (B1 regression check).
2. **DuckDB/FalkorDB:** connect FalkorDB, Build → Tables/files → the NODES/EDGES builder appears (not just the manual node/edge form). Type a NODES id (`vertexes.parquet.id` or default-table + `id`), EDGES source+target, click **Generate/Build** → the graph builds via `/create` and appears in **List**.
3. Column autocomplete: after typing a Parquet source path, the column dropdown populates from `/columns` (best-effort; manual entry always works).
4. Table dropdowns: Kinetica shows real tables from `/tables`; FalkorDB shows none (manual path entry) — expected until `/register_file` (follow-up).

---

## Deferred / follow-up plan (NOT in B2)

- **`POST /register_file`** — turn a file into a first-class relation: DuckDB `CREATE VIEW v AS SELECT * FROM '<path>'`; Kinetica `CREATE EXTERNAL TABLE … FILE PATHS … WITH OPTIONS (DATA SOURCE=…)` / KiFS upload for local files, remote via named `DATA SOURCE` + `CREDENTIAL`. This needs a persistent DuckDB catalog (the compute engine currently uses throwaway per-call connections) and Kinetica DATA SOURCE management — a separate subsystem, its own plan.
- **Kinetica live `/show/graph/grammar`** — fetch the real grammar with the static `DEFAULT_GRAMMAR` fallback (frontend `setGraphGrammar`/`setEndpointGrammar` are currently never called). B2 keeps the static grammars.
- **Slice C polish** — WKT/geo grammar entries, multi-table combos in the builder→spec translation, live-DDL preview parity, unifying the Solve/Match helpers onto the same grammar-driven section component.

---

## Self-Review

- **Spec coverage (B2):** engine-neutral `/tables` (Tasks 1–4) + `/columns` (Tasks 1–4); per-engine grammar via static `DUCKDB_GRAMMAR` (Task 7) with Kinetica live-grammar explicitly deferred; per-engine build call — Kinetica DDL (B1) vs DuckDB `/create` spec (Task 7, `buildTarget='spec'`); table-list/column-list sources per engine (Kinetica `show_table`, DuckDB/FalkorDB `DESCRIBE`). File-backed relations / `/register_file` are stated as deferred (matches the design's "resolved in B2" caveat being partially deferred — called out, not hidden).
- **Placeholder scan:** none — every backend step gives exact code + exact pytest command + expected pass/fail; frontend steps give exact code and cite the anchor to insert against. The one judgment call (`buildTarget='spec'` translation) is fully specified with concrete code.
- **Type/name consistency:** `list_tables() -> list[{"name","type"}]` and `list_columns(table) -> list[str]` are defined in Task 1 and consumed identically in Tasks 2–4; `gwClient.tables()`/`columns(table)` (Task 5) match the endpoints (Task 4); `fetchTableColumnsGW` (Task 6) is threaded via `createProps.onFetchTableColumns` (Task 6 Step 3) and consumed by `CreateHelperPanel` (B1); `buildTarget`/the row-model shape `{graphName,directed,rows,sectionTable,options}` produced by `chGenerate` (Task 7 Step 1) is consumed by `handleBuildFromModel` (Task 7 Step 4); the `/create` spec shape matches `falkordb_adapter.load_graph` / the existing `handleCreate`.
- **Scope:** B2 = generalize read-endpoints + non-Kinetica builder mount. `/register_file`, Kinetica live grammar, and Slice-C polish are deferred to their own plans, each independently shippable.
- **Risk sequencing:** backend is pure TDD with FakeAdapter + embedded DuckDB (Tasks 1–4 fully verifiable headlessly); Kinetica/FalkorDB live paths degrade to `[]` and are live-skip tested; the frontend (Tasks 6–7, not headlessly verifiable) lands last and cannot regress the backend. Kinetica's B1 builder path is untouched (guarded by `graphEngine === 'kinetica'`).
