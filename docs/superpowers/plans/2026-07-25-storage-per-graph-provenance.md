# Per-graph Storage provenance — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Storage action show, per active graph, the real CSV/Parquet source files it was built from and how the graph was created — replacing the hardcoded global `HYDRATE_SOURCE` preview that shows the same banking files for every graph.

**Architecture:** Store the structured `/create` spec (not just the rendered recipe text) in the DuckDB meta-store ledger `xgraph_creations` via a new nullable `spec_json` column. A pure `graph_source_relations(recipe)` helper in `app.py` derives `[{name, path, role}]` from that spec (primary) or by parsing the legacy recipe text (fallback). The `/graph_ddl` response is widened with `spec` + `sources`, and StoragePanel is reshaped into **Route → Data in (per-source previews) → How it was created (recipe)**.

**Tech Stack:** FastAPI/Python gateway (`backend/`, own venv at `backend/.venv`), embedded DuckDB meta store, single-file React 18 UMD + Babel frontend (`frontend/XGraph.html`, no build step). Backend tests: pytest run from `backend/`. Frontend validation: esbuild JSX check + gateway `curl` 200.

## Global Constraints

- **Do NOT `git commit` anything under `xgraph/`** (CLAUDE.md). Write files freely; never stage/commit them. This plan and the spec are local-only. Where the writing-plans/executing-plans skill mentions a commit step, **skip the commit** — end each task at the passing-test/validation step instead.
- **Backend venv:** run Python/pytest via `backend/.venv/bin/python` from the `backend/` directory. Never use a global interpreter.
- **DuckDB TIMESTAMP is tz-naive** — the ledger already stores naive UTC (`datetime.now(timezone.utc).replace(tzinfo=None)`); keep that pattern.
- **`spec_json` is nullable + backward compatible** — `record_creation`'s `spec` param defaults to `None`; legacy rows and very old DBs (column absent) must read back as `spec=None` without error.
- **Frontend edits are anchored search-and-replace** against verbatim strings in the ~8,900-line `XGraph.html`; line numbers drift — **read each region immediately before editing**.
- **Every frontend edit** is validated by the esbuild JSX check (`ESBUILD_OK`) + a gateway `curl` 200; real behavior is browser-driven (React app is not headlessly verifiable).
- **Version bump:** `EXPLORER_VERSION` (frontend, ~L50) `"0.13.1"` → `"0.14.0"` in the final task.

### Backend test command

```bash
cd /home/kkaramete/xgraph/backend && ./.venv/bin/python -m pytest tests/test_metadata_store.py tests/test_creation_viewer.py tests/test_graph_ddl.py -v
```

### Frontend esbuild JSX check

```bash
cd /home/kkaramete/xgraph/frontend
end=$(grep -n '</script>' XGraph.html | tail -1 | cut -d: -f1)
sed -n "47,$((end-1))p" XGraph.html | ./node_modules/.bin/esbuild --loader=jsx > /dev/null && echo ESBUILD_OK || echo ESBUILD_FAIL
```

### Gateway curl smoke

```bash
cd /home/kkaramete/xgraph && (./xgraph status >/dev/null 2>&1 || ./xgraph start) && sleep 1 && curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8090/
```

---

## File Structure

- **Modify:** `backend/xgraph_gateway/compute/duckdb_engine.py`
  - `_meta_con()` — add `spec_json` to the `CREATE TABLE IF NOT EXISTS xgraph_creations` + a guarded `ALTER TABLE … ADD COLUMN spec_json` migration.
  - `record_creation(graph, engine, statement, source, spec=None)` — serialize `spec` to JSON; INSERT/UPDATE both carry it.
  - `get_creation(graph)` — select `spec_json`; return parsed `spec` (dict or `None`); tolerate the column being absent.
- **Modify:** `backend/xgraph_gateway/app.py`
  - New pure module-level helper `graph_source_relations(recipe) -> list[dict]`.
  - `/create` — pass `spec=spec` into `record_creation`.
  - `/graph_ddl` — widen the ledger branch to `{statement, source, spec, sources}`; other branches carry `spec=None, sources=[]`.
- **Modify:** `backend/tests/test_metadata_store.py` — `spec` round-trip, legacy NULL, migration.
- **Modify:** `backend/tests/test_graph_ddl.py` — `graph_source_relations` unit cases + `/graph_ddl` endpoint returns `sources`.
- **Modify:** `frontend/XGraph.html` — StoragePanel reshape (Route → Data in → recipe), version bump.

---

## Task 1: Ledger `spec_json` column + migration + record/get changes

**Files:**
- Modify: `backend/xgraph_gateway/compute/duckdb_engine.py:36-42` (CREATE TABLE), `:104-123` (`record_creation`), `:125-137` (`get_creation`)
- Test: `backend/tests/test_metadata_store.py`

**Interfaces:**
- Produces: `record_creation(graph, engine, statement, source, spec=None) -> dict` — `spec` is an optional dict, stored as JSON in `spec_json` (NULL when `None`). `get_creation(graph) -> dict | None` — the returned dict gains a `spec` key holding the parsed dict (or `None`).

**Context:** `xgraph_creations` currently has columns `(graph, engine, statement, source, ts)` with PK `(graph, engine)`. The store is DuckDB-only (no Kinetica mirror). `_meta_con()` runs one-time DDL guarded by `self._meta_ready`. `json` is not yet imported in this file.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_metadata_store.py`:

```python
def test_record_creation_roundtrips_spec(tmp_path):
    eng = _engine(tmp_path)
    spec = {"graph": "g1", "nodes": [{"sql": "SELECT * FROM 'a.parquet'"}],
            "tables": {"a": "/data/a.parquet"}}
    eng.record_creation("g1", "falkordb", "-- recipe", "create", spec=spec)
    row = eng.get_creation("g1")
    assert row["spec"] == spec


def test_record_creation_without_spec_reads_back_none(tmp_path):
    eng = _engine(tmp_path)
    eng.record_creation("g1", "falkordb", "-- recipe", "create")
    row = eng.get_creation("g1")
    assert row["spec"] is None


def test_creation_spec_survives_upsert(tmp_path):
    eng = _engine(tmp_path)
    eng.record_creation("g1", "falkordb", "-- v1", "create", spec={"graph": "g1", "v": 1})
    eng.record_creation("g1", "falkordb", "-- v2", "create", spec={"graph": "g1", "v": 2})
    assert eng.get_creation("g1")["spec"] == {"graph": "g1", "v": 2}


def test_get_creation_migrates_legacy_table_without_spec_json(tmp_path):
    import duckdb
    meta = str(tmp_path / "meta.duckdb")
    # Simulate a pre-spec_json DB: create the ledger with the OLD column set.
    con = duckdb.connect(meta)
    con.execute("CREATE TABLE xgraph_creations ("
                " graph VARCHAR, engine VARCHAR, statement VARCHAR,"
                " source VARCHAR, ts TIMESTAMP, PRIMARY KEY (graph, engine))")
    con.execute("INSERT INTO xgraph_creations VALUES "
                "('old', 'falkordb', '-- legacy', 'create', now())")
    con.close()
    eng = DuckDBComputeEngine(meta_path=meta)
    row = eng.get_creation("old")           # must not raise; migration adds the column
    assert row["statement"] == "-- legacy"
    assert row["spec"] is None
    # New writes on the migrated table carry spec.
    eng.record_creation("new", "falkordb", "-- new", "create", spec={"graph": "new"})
    assert eng.get_creation("new")["spec"] == {"graph": "new"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/kkaramete/xgraph/backend && ./.venv/bin/python -m pytest tests/test_metadata_store.py -k "spec or migrat" -v`
Expected: FAIL (`record_creation()` got an unexpected keyword argument `'spec'`, and `get_creation` has no `spec` key).

- [ ] **Step 3: Add the `json` import**

At the top of `backend/xgraph_gateway/compute/duckdb_engine.py`, after `import duckdb` (line 2), add:

```python
import json
```

- [ ] **Step 4: Add the `spec_json` column to the CREATE TABLE + a guarded migration**

In `_meta_con()`, replace the `xgraph_creations` CREATE block (currently lines 36-40):

```python
            con.execute(
                "CREATE TABLE IF NOT EXISTS xgraph_creations ("
                " graph VARCHAR, engine VARCHAR, statement VARCHAR,"
                " source VARCHAR, ts TIMESTAMP,"
                " PRIMARY KEY (graph, engine))")
```

with (add `spec_json` to the fresh-DB shape, then a guarded ALTER for existing DBs):

```python
            con.execute(
                "CREATE TABLE IF NOT EXISTS xgraph_creations ("
                " graph VARCHAR, engine VARCHAR, statement VARCHAR,"
                " source VARCHAR, ts TIMESTAMP, spec_json VARCHAR,"
                " PRIMARY KEY (graph, engine))")
            # Migration for DBs created before spec_json existed. Idempotent:
            # ADD COLUMN raises if it already exists, so swallow that one case.
            try:
                con.execute("ALTER TABLE xgraph_creations ADD COLUMN spec_json VARCHAR")
            except Exception:
                pass
```

- [ ] **Step 5: Carry `spec` through `record_creation`**

Replace `record_creation` (lines 104-123) with:

```python
    def record_creation(self, graph, engine, statement, source, spec=None):
        """UPSERT the 'how this graph was created' recipe, keyed on
        (graph, engine). Latest write wins. `spec` (the structured /create
        spec) is stored as JSON in spec_json; None -> NULL (legacy shape)."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        spec_json = json.dumps(spec) if spec is not None else None
        con = self._meta_con()
        try:
            existing = con.execute(
                "SELECT 1 FROM xgraph_creations WHERE graph = ? AND engine = ?",
                [graph, engine]).fetchone()
            if existing is None:
                con.execute(
                    "INSERT INTO xgraph_creations VALUES (?, ?, ?, ?, ?, ?)",
                    [graph, engine, statement, source, now, spec_json])
            else:
                con.execute(
                    "UPDATE xgraph_creations SET statement = ?, source = ?,"
                    " ts = ?, spec_json = ? WHERE graph = ? AND engine = ?",
                    [statement, source, now, spec_json, graph, engine])
            return {"graph": graph, "engine": engine, "source": source, "ts": _iso(now)}
        finally:
            con.close()
```

- [ ] **Step 6: Return the parsed `spec` from `get_creation`**

Replace `get_creation` (lines 125-137) with:

```python
    def get_creation(self, graph):
        """Most-recent recorded creation recipe for `graph` (any engine),
        including the parsed structured spec (None when not recorded)."""
        con = self._meta_con()
        try:
            row = con.execute(
                "SELECT graph, engine, statement, source, ts, spec_json"
                " FROM xgraph_creations WHERE graph = ?"
                " ORDER BY ts DESC LIMIT 1", [graph]).fetchone()
            if not row:
                return None
            spec = None
            if row[5]:
                try:
                    spec = json.loads(row[5])
                except Exception:
                    spec = None
            return {"graph": row[0], "engine": row[1], "statement": row[2],
                    "source": row[3], "ts": _iso(row[4]), "spec": spec}
        finally:
            con.close()
```

- [ ] **Step 7: Run the full metadata-store suite**

Run: `cd /home/kkaramete/xgraph/backend && ./.venv/bin/python -m pytest tests/test_metadata_store.py tests/test_creation_viewer.py -v`
Expected: PASS (new spec/migration tests pass; the existing `test_creation_viewer` tests still pass — they don't assert on the absence of a `spec` key).

---

## Task 2: `graph_source_relations` helper + `/create` spec passing + `/graph_ddl` widening

**Files:**
- Modify: `backend/xgraph_gateway/app.py` — add `graph_source_relations` (module level, near `render_create_recipe` ~line 44); `/create` (~286-303); `/graph_ddl` (~312-332)
- Test: `backend/tests/test_graph_ddl.py`

**Interfaces:**
- Consumes: `get_creation(graph)` now returns a `spec` key (Task 1).
- Produces: `graph_source_relations(recipe: dict) -> list[dict]` where each entry is `{"name": str, "path": str, "role": str | None}` (`role` in `{"nodes", "edges", None}`). `/graph_ddl` ledger branch returns `{statement, source, spec, sources}`; other branches return `sources=[]` (and `spec=None` where applicable).

**Context:** `render_create_recipe(spec)` renders a `-- source tables: name = path, name2 = path2` line from `spec["tables"]` (a `name → path` map). A node/edge source SELECT lives in `spec["nodes"][i]["sql"]` / `spec["edges"][i]["sql"]` and references a file path (the table's value) inside quotes, e.g. `SELECT ... FROM '/data/a.parquet'`. Role is inferred by checking whether a table's path appears in any node SQL vs any edge SQL.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_graph_ddl.py`:

```python
from xgraph_gateway.app import graph_source_relations
from xgraph_gateway.compute.duckdb_engine import DuckDBComputeEngine


# ---------------------------------------------------------------------------
# graph_source_relations: derive per-graph source files from a recipe.
# ---------------------------------------------------------------------------

def test_source_relations_from_spec_infers_roles():
    recipe = {"spec": {
        "tables": {"acct": "/data/accounts.parquet", "wires": "/data/wires.csv"},
        "nodes": [{"sql": "SELECT id AS NODE FROM '/data/accounts.parquet'"}],
        "edges": [{"sql": "SELECT s AS n1, t AS n2 FROM '/data/wires.csv'"}],
    }}
    out = graph_source_relations(recipe)
    by_name = {r["name"]: r for r in out}
    assert by_name["acct"]["path"] == "/data/accounts.parquet"
    assert by_name["acct"]["role"] == "nodes"
    assert by_name["wires"]["role"] == "edges"


def test_source_relations_spec_role_none_when_unreferenced():
    recipe = {"spec": {"tables": {"x": "/data/x.parquet"}, "nodes": [], "edges": []}}
    assert graph_source_relations(recipe) == [
        {"name": "x", "path": "/data/x.parquet", "role": None}]


def test_source_relations_falls_back_to_recipe_text():
    recipe = {"spec": None, "statement":
              "-- FalkorDB graph\n"
              "-- source tables: acct = /data/accounts.parquet, wires = /data/wires.csv\n"
              "-- Step 2 ..."}
    out = graph_source_relations(recipe)
    assert {"name": "acct", "path": "/data/accounts.parquet", "role": None} in out
    assert {"name": "wires", "path": "/data/wires.csv", "role": None} in out


def test_source_relations_empty_when_nothing_recorded():
    assert graph_source_relations({"spec": None, "statement": "-- no sources"}) == []
    assert graph_source_relations({}) == []


# ---------------------------------------------------------------------------
# /graph_ddl: ledger branch carries spec + resolved sources.
# ---------------------------------------------------------------------------

def test_graph_ddl_endpoint_returns_sources_from_ledger(tmp_path):
    compute = DuckDBComputeEngine(meta_path=str(tmp_path / "meta.duckdb"))
    spec = {"graph": "g", "tables": {"acct": "/data/accounts.parquet"},
            "nodes": [{"sql": "SELECT id AS NODE FROM '/data/accounts.parquet'"}],
            "edges": []}
    compute.record_creation("g", "fake", "-- recipe", "create", spec=spec)
    # FakeAdapter.creation_statement returns {statement: None} so the endpoint
    # falls through to the ledger branch.
    c = TestClient(create_app(adapter_factory=lambda e: FakeAdapter(), compute=compute))
    r = c.get("/graph_ddl", params={"engine": "fake", "graph": "g"})
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "xgraph:create-ledger"
    assert body["spec"]["graph"] == "g"
    assert {"name": "acct", "path": "/data/accounts.parquet",
            "role": "nodes"} in body["sources"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/kkaramete/xgraph/backend && ./.venv/bin/python -m pytest tests/test_graph_ddl.py -k "source_relations or returns_sources" -v`
Expected: FAIL (`cannot import name 'graph_source_relations'`).

- [ ] **Step 3: Add the `graph_source_relations` helper**

In `backend/xgraph_gateway/app.py`, immediately after `render_create_recipe` (i.e. after its `return "\n".join(lines)` at line 69), add:

```python
def graph_source_relations(recipe: dict) -> list[dict]:
    """Derive the real source relations a graph was built from, as
    [{"name", "path", "role"}]. Primary: the structured spec's `tables`
    map, with role inferred from which node/edge SELECT references each path.
    Fallback: parse the recipe statement's `-- source tables: n = p, ...`
    line (role unknown). Neither present -> []."""
    if not isinstance(recipe, dict):
        return []
    spec = recipe.get("spec")
    if isinstance(spec, dict) and isinstance(spec.get("tables"), dict) and spec["tables"]:
        node_sql = " ".join(str(n.get("sql", "")) for n in (spec.get("nodes") or []))
        edge_sql = " ".join(str(e.get("sql", "")) for e in (spec.get("edges") or []))
        out = []
        for name, path in spec["tables"].items():
            p = str(path)
            in_nodes, in_edges = p in node_sql, p in edge_sql
            role = "nodes" if in_nodes and not in_edges else (
                "edges" if in_edges and not in_nodes else None)
            out.append({"name": str(name), "path": p, "role": role})
        return out
    # Legacy fallback: parse the rendered recipe text.
    stmt = recipe.get("statement") or ""
    for line in stmt.splitlines():
        marker = "-- source tables:"
        if marker in line:
            body = line.split(marker, 1)[1].strip()
            out = []
            for pair in body.split(","):
                if "=" in pair:
                    name, _, path = pair.partition("=")
                    out.append({"name": name.strip(), "path": path.strip(), "role": None})
            return out
    return []
```

- [ ] **Step 4: Pass `spec` into `record_creation` in `/create`**

In the `/create` endpoint (line 296-298), replace:

```python
                    _resolve_compute(session).record_creation(
                        spec["graph"], _resolve_engine(session, engine),
                        render_create_recipe(spec), "create")
```

with:

```python
                    _resolve_compute(session).record_creation(
                        spec["graph"], _resolve_engine(session, engine),
                        render_create_recipe(spec), "create", spec=spec)
```

- [ ] **Step 5: Widen the `/graph_ddl` response**

In the `graph_ddl` endpoint, replace the ledger branch (lines 319-321):

```python
            recorded = _resolve_compute(session).get_creation(graph)
            if recorded and recorded.get("statement"):
                return {"statement": recorded["statement"], "source": "xgraph:create-ledger"}
```

with (attach `spec` + derived `sources`):

```python
            recorded = _resolve_compute(session).get_creation(graph)
            if recorded and recorded.get("statement"):
                return {"statement": recorded["statement"],
                        "source": "xgraph:create-ledger",
                        "spec": recorded.get("spec"),
                        "sources": graph_source_relations(recorded)}
```

Then make the other two return paths carry the same keys so the shape is uniform. Replace the synthesized branch (lines 326-327):

```python
                if syn:
                    return {"statement": syn, "source": "xgraph:schema-synthesized"}
```

with:

```python
                if syn:
                    return {"statement": syn, "source": "xgraph:schema-synthesized",
                            "spec": None, "sources": []}
```

And the final fallthrough (line 330):

```python
            return stmt if stmt else {"statement": None, "source": None}
```

with:

```python
            if stmt:
                stmt.setdefault("spec", None)
                stmt.setdefault("sources", [])
                return stmt
            return {"statement": None, "source": None, "spec": None, "sources": []}
```

Note: the live-adapter DDL branch (`if stmt and stmt.get("statement"): return stmt` at lines 317-318) is left as-is — Kinetica/live callers get `spec`/`sources` absent, which the frontend treats as empty. (If you prefer strict uniformity, add `stmt.setdefault(...)` there too; not required.)

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd /home/kkaramete/xgraph/backend && ./.venv/bin/python -m pytest tests/test_graph_ddl.py -v`
Expected: PASS (new `source_relations`/`returns_sources` tests + all existing `/graph_ddl` tests).

- [ ] **Step 7: Run the broader backend suite for regressions**

Run: `cd /home/kkaramete/xgraph/backend && ./.venv/bin/python -m pytest tests/ -q`
Expected: PASS or SKIP only (live FalkorDB/Kinetica tests skip when engines are down; the 6 known Kinetica-env tests may still skip/fail per environment — no *new* failures introduced by this change).

---

## Task 3: StoragePanel reshape (Route → Data in → recipe) + version bump

**Files:**
- Modify: `frontend/XGraph.html` — `StoragePanel` (~7627-7751), `EXPLORER_VERSION` (~L50)

**Interfaces:**
- Consumes: `gwClient.graphDdl(graph)` → `{statement, source, spec, sources:[{name,path,role}]}`; `gwClient.sourcePreview(path)` → `{columns, rows}`; `gwClient.storage(graph)`; `gwClient.documents(graph)`; the existing `StorageTable` component.
- Produces: a per-graph Storage view — a **Route** banner, a **Data in** section rendering one `StorageTable` per `sources` entry (labeled `name · role · path`), and the existing **How it was built** recipe `<pre>`. Removes the hardcoded `HYDRATE_SOURCE` / `HYDRATE_EDGE_SOURCE` preview.

**Context:** StoragePanel today fetches `graphDdl`, `storage`, and `documents` for the active graph, renders the recipe in a `<pre>`, keeps the extracted-graph document list and the Kinetica backing-table `storage()` preview, and — the bug — hardcodes two `StorageTable`s over the module-level `HYDRATE_SOURCE` / `HYDRATE_EDGE_SOURCE` constants regardless of the active graph. `StorageTable` renders `{columns, rows}` (max 25 rows). React 18 UMD + Babel; no build step. `useState`/`useEffect` are in scope via the top-level `const { useState, useEffect } = React;`.

- [ ] **Step 1: Read the current StoragePanel + StorageTable region for verbatim anchors**

```bash
cd /home/kkaramete/xgraph/frontend
grep -n "function StoragePanel\|function StorageTable\|HYDRATE_SOURCE\|HYDRATE_EDGE_SOURCE\|sourcePreview\|graphDdl\|How it was built\|EXPLORER_VERSION" XGraph.html
```
Read the full `StoragePanel` body and the `StorageTable` signature (~15 lines) so the edits below anchor on the exact current strings (line numbers will have drifted).

- [ ] **Step 2: Add sources state + per-source preview fetch**

In `StoragePanel`, alongside the existing state (the `graphDdl`/recipe/documents state found in Step 1), add a `sources` list and a map of per-source previews. After the existing state declarations, add:

```javascript
        const [srcRelations, setSrcRelations] = useState([]);   // [{name,path,role}]
        const [srcPreviews, setSrcPreviews] = useState({});     // path -> {columns,rows} | {error}
```

In the `useEffect` that already fetches `graphDdl` for the active graph, capture `sources` from the response and fetch a preview per source. Inside that effect's `graphDdl` `.then` (or after `await gwClient.graphDdl(activeGraph)`), add:

```javascript
                var rels = (ddl && ddl.sources) || [];
                setSrcRelations(rels);
                setSrcPreviews({});
                rels.forEach(function (rel) {
                    gwClient.sourcePreview(rel.path).then(function (p) {
                        setSrcPreviews(function (prev) {
                            var next = Object.assign({}, prev);
                            next[rel.path] = p;
                            return next;
                        });
                    }).catch(function (err) {
                        setSrcPreviews(function (prev) {
                            var next = Object.assign({}, prev);
                            next[rel.path] = { error: err.message };
                            return next;
                        });
                    });
                });
```

(Match the effect's actual style found in Step 1 — if it uses `.then` chains, use the `.then` form above; if `async/await`, assign `var ddl = await gwClient.graphDdl(activeGraph);` then run the same block. Also reset `setSrcRelations([])` / `setSrcPreviews({})` at the top of the effect when `activeGraph` changes, mirroring how the recipe state is reset.)

- [ ] **Step 3: Add a Route banner derivation**

Near the top of the `StoragePanel` return (before the recipe `<pre>`), add a computed route label. Just before the `return (`, add:

```javascript
        var hasDocs = documents && documents.length > 0;   // match the doc-list state name from Step 1
        var route = hasDocs ? 'Built from documents (Extract)'
            : (srcRelations.length ? 'Built from files (DuckDB → FalkorDB)'
            : ((ddlSource === 'kinetica:show_graph') ? 'Built via Kinetica DDL'
            : 'Built externally / route not recorded'));
```

(Use the actual variable names found in Step 1 for the documents list and the `graphDdl` response's `source` field — shown here as `documents` and `ddlSource`. If the recipe response object is held whole, use `recipe.source` instead of a separate `ddlSource`.)

- [ ] **Step 4: Render the Route banner + Data-in section; remove the hardcoded HYDRATE preview**

Replace the two hardcoded `HYDRATE_SOURCE` / `HYDRATE_EDGE_SOURCE` `StorageTable` blocks (found in Step 1) with the route banner + a per-source loop. Insert the banner above the Data-in section and render:

```jsx
                <div style={{ fontSize:12, fontWeight:700, color:'#0984e3', margin:'4px 0 10px' }}>
                    Route: {route}
                </div>
                <div style={{ marginBottom:12 }}>
                    <label style={{ fontSize:12, fontWeight:700, color:'#636e72' }}>Data in — source files</label>
                    {srcRelations.length === 0 && (
                        <p style={{ fontSize:12, color:'#b2bec3', margin:'4px 0' }}>
                            Source files not recorded for this graph — rebuild to capture them.
                        </p>
                    )}
                    {srcRelations.map(function (rel) {
                        var p = srcPreviews[rel.path];
                        return (
                            <div key={rel.path} style={{ marginBottom:10 }}>
                                <div style={{ fontSize:11, color:'#636e72', margin:'2px 0' }}>
                                    <strong>{rel.name}</strong>{rel.role ? ' · ' + rel.role : ''} · <span style={{ color:'#b2bec3' }}>{rel.path}</span>
                                </div>
                                {!p && <span style={{ fontSize:11, color:'#b2bec3' }}>Loading…</span>}
                                {p && p.error && <span style={{ fontSize:11, color:'#d63031' }}>{p.error}</span>}
                                {p && !p.error && <StorageTable columns={p.columns} rows={p.rows} />}
                            </div>
                        );
                    })}
                </div>
```

(Match `StorageTable`'s actual prop names from Step 1 — if it takes a single `data`/`preview` object rather than `columns`/`rows`, pass `p` accordingly. Keep the existing extracted-graph document list and the Kinetica `storage()` backing-table preview blocks untouched — only the two `HYDRATE_*` tables are removed.)

- [ ] **Step 5: Bump the version badge**

`EXPLORER_VERSION` (~L50): `"0.13.1"` → `"0.14.0"`.

- [ ] **Step 6: esbuild JSX check**

Run the esbuild JSX check (Global Constraints).
Expected: `ESBUILD_OK`.

- [ ] **Step 7: Confirm no stale HYDRATE preview references remain in StoragePanel**

```bash
cd /home/kkaramete/xgraph/frontend
awk '/function StoragePanel/,/^        }$/' XGraph.html | grep -n "HYDRATE_SOURCE\|HYDRATE_EDGE_SOURCE" || echo "NO_HARDCODED_HYDRATE_IN_STORAGE"
```
Expected: `NO_HARDCODED_HYDRATE_IN_STORAGE`. (The module-level `HYDRATE_SOURCE` const may still exist for QueryPanel's Explain — only its use *inside StoragePanel* is removed.)

- [ ] **Step 8: Gateway curl 200**

Run the gateway curl smoke (Global Constraints).
Expected: `200`.

---

## Manual (browser) acceptance — run after Task 3

Hard-reload, confirm `v0.14.0`, connect + select a graph, open **Storage**:

1. **Route banner** shows the correct route for the active graph (files graph → "Built from files (DuckDB → FalkorDB)"; extracted graph → "Built from documents (Extract)").
2. **Data in — source files:** for a graph built via the Tables/files route, the previews are the graph's **own** source files (their real columns/rows), not the banking `HYDRATE_SOURCE` files. Switching the active graph swaps the previews.
3. A graph with no recorded sources shows the note "Source files not recorded for this graph — rebuild to capture them."
4. **How it was built** recipe `<pre>` is unchanged; extracted graphs still show the document-provenance list; Kinetica graphs still show their backing-table `storage()` preview.
5. A newly built graph (POST /create) immediately shows its real source files in Storage (spec now recorded).

---

## Self-Review

- **Spec coverage:** `spec_json` column + guarded migration + `record_creation(..., spec=None)` + `get_creation` returns `spec` (Task 1); `graph_source_relations` spec-primary/text-fallback/empty + `/create` spec passing + `/graph_ddl` widened with `spec`+`sources` (Task 2); StoragePanel Route → Data in → recipe replacing the hardcoded `HYDRATE_SOURCE` preview + version bump (Task 3). All spec "Architecture" and "Testing" items map to a task.
- **Placeholder scan:** every code step contains real, runnable content; the "match the actual name from Step 1" notes are explicit instructions to reuse verbatim sibling code discovered in a read-first step, not TODOs — required because `XGraph.html` line numbers drift and the exact local state-variable names must be confirmed before editing.
- **Type consistency:** `record_creation(graph, engine, statement, source, spec=None)` and `get_creation → {..., "spec"}` (Task 1) are exactly what Task 2's `graph_source_relations(recipe)` and the `/graph_ddl` ledger branch consume; `graph_source_relations` returns `[{name, path, role}]`, which the endpoint returns as `sources` and Task 3's frontend iterates. `sourcePreview(path) → {columns, rows}` matches `StorageTable`'s render (confirm exact prop names in Task 3 Step 1).
- **Scope:** per-graph Storage provenance only. Build-UI files-route (part A) and edge-folding parity remain deferred (spec Out-of-scope).
- **Risk sequencing:** backend ledger change (Task 1, isolated, fully unit-tested) → endpoint wiring (Task 2, unit + endpoint tests, regression sweep) → frontend reshape (Task 3, additive display change gated by esbuild + curl). Each task ends at a passing test/validation; **no commits** per the no-commit constraint.
```