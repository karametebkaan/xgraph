# Graph creation-SQL viewer — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show (and copy) the SQL/recipe that built a graph — Kinetica `CREATE GRAPH` DDL from `show_graph`, FalkorDB's recorded `/create` recipe — per-graph in **List** and for the active graph in **Build**. Persist creation recipes so they survive reloads and need no session state.

**Architecture:** A new `xgraph_creations` table in the meta DuckDB records each `/create` build; `GET /graph_ddl` returns the adapter's live statement (Kinetica `show_graph`) and, when null, falls back to the recorded recipe (FalkorDB). The List panel gains a per-graph DDL expander (wired to the existing `gwClient.graphDdl`); the Build panel already renders `activeDdl.statement`, so it benefits automatically.

**Tech Stack:** Backend — FastAPI + embedded DuckDB (meta store), run pytest from `backend/`. Frontend — React 18 UMD + Babel-standalone; esbuild JSX check.

## Global Constraints

- **No `git commit` unless authorized** (CLAUDE.md); in a background job commits land on the worktree branch and are fast-forwarded onto `main`.
- **Recording is best-effort:** a `record_creation` failure inside `/create` must NEVER fail the build (wrap in try/except).
- **Kinetica behavior unchanged:** `show_graph` DDL (non-null) still wins in `/graph_ddl`; the ledger is only a fallback for null statements.
- **Meta-store timestamps:** naive UTC via `datetime.now(timezone.utc).replace(tzinfo=None)`, ISO-coerced on read with the existing `_iso` helper (match `record_document`).
- **Tests:** run from `backend/` with `./.venv/bin/python -m pytest tests/ -v`; embedded DuckDB tests use an isolated `meta_path` under `tmp_path` (never the shared `data/xgraph_meta.duckdb`). Baseline: 348 passed / 44 skipped.
- **Frontend edits validated by the esbuild JSX check** (below) → `ESBUILD_OK`.
- **Version badge:** bump `EXPLORER_VERSION` `0.9.0` → `0.10.0` in the frontend task.
- Commit messages: concise 1–2 lines, no `Co-Authored-By` footer.

### Frontend esbuild JSX check

```bash
cd /home/kkaramete/xgraph/frontend
end=$(grep -n '</script>' XGraph.html | tail -1 | cut -d: -f1)
sed -n "47,$((end-1))p" XGraph.html | ./node_modules/.bin/esbuild --loader=jsx > /dev/null && echo ESBUILD_OK || echo ESBUILD_FAIL
```

---

## File Structure

- **Backend:**
  - `backend/xgraph_gateway/compute/duckdb_engine.py` — `xgraph_creations` table in `_meta_con()`; `record_creation`/`get_creation`; extend `clear_graph_metadata` (Task 1).
  - `backend/xgraph_gateway/app.py` — module-level `render_create_recipe(spec)`; `/create` records; `/graph_ddl` ledger fallback (Task 2).
  - `backend/tests/test_creation_viewer.py` — new (Tasks 1–2).
- **Frontend:**
  - `frontend/XGraph.html` — `ListPanel` gwClient + per-row "⌄ SQL" expander; thread `gwClient` at the mount; Build caption tweak; version bump (Task 3).

---

## Task 1: `xgraph_creations` meta table + `record_creation`/`get_creation`

**Files:**
- Modify: `backend/xgraph_gateway/compute/duckdb_engine.py`.
- Create: `backend/tests/test_creation_viewer.py`.

**Interfaces:**
- Produces:
  - `DuckDBComputeEngine.record_creation(self, graph: str, engine: str, statement: str, source: str) -> dict` — UPSERT on `(graph, engine)`; returns `{"graph","engine","source","ts"}` (ISO ts).
  - `DuckDBComputeEngine.get_creation(self, graph: str) -> dict | None` — most-recent row for `graph`, `{"graph","engine","statement","source","ts"}` or `None`.
- Consumes: existing `_meta_con()`, `_iso`, `datetime`/`timezone` (already imported).

**Context:** Mirror `record_document`/`get_document` exactly (same connection/close pattern, naive-UTC ts). Table keyed `(graph, engine)`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_creation_viewer.py`:

```python
from xgraph_gateway.compute.duckdb_engine import DuckDBComputeEngine


def _engine(tmp_path):
    return DuckDBComputeEngine(meta_path=str(tmp_path / "meta.duckdb"))


def test_record_and_get_creation(tmp_path):
    eng = _engine(tmp_path)
    assert eng.get_creation("g1") is None
    eng.record_creation("g1", "falkordb", "-- recipe text", "create")
    row = eng.get_creation("g1")
    assert row["graph"] == "g1"
    assert row["engine"] == "falkordb"
    assert row["statement"] == "-- recipe text"
    assert row["source"] == "create"
    assert row["ts"]  # ISO string present


def test_record_creation_upserts(tmp_path):
    eng = _engine(tmp_path)
    eng.record_creation("g1", "kinetica", "CREATE GRAPH g1 (...);", "create")
    eng.record_creation("g1", "kinetica", "CREATE OR REPLACE GRAPH g1 (...);", "create")
    row = eng.get_creation("g1")
    assert row["statement"] == "CREATE OR REPLACE GRAPH g1 (...);"  # latest wins


def test_clear_graph_metadata_drops_creation(tmp_path):
    eng = _engine(tmp_path)
    eng.record_creation("g1", "falkordb", "x", "create")
    eng.clear_graph_metadata("g1")
    assert eng.get_creation("g1") is None
```

- [ ] **Step 2: Run — verify FAIL**

```bash
cd /home/kkaramete/xgraph/backend
./.venv/bin/python -m pytest tests/test_creation_viewer.py -v
```
Expected: FAIL (`record_creation`/`get_creation` don't exist).

- [ ] **Step 3: Add the table to `_meta_con()`**

In `_meta_con()`, inside the `if not self._meta_ready:` block (after the `xgraph_ontology` CREATE), add:

```python
            con.execute(
                "CREATE TABLE IF NOT EXISTS xgraph_creations ("
                " graph VARCHAR, engine VARCHAR, statement VARCHAR,"
                " source VARCHAR, ts TIMESTAMP,"
                " PRIMARY KEY (graph, engine))")
```

- [ ] **Step 4: Add `record_creation` / `get_creation`**

After `get_document` (≈L97), add:

```python
    def record_creation(self, graph, engine, statement, source):
        """UPSERT the 'how this graph was created' recipe, keyed on
        (graph, engine). Latest write wins."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        con = self._meta_con()
        try:
            existing = con.execute(
                "SELECT 1 FROM xgraph_creations WHERE graph = ? AND engine = ?",
                [graph, engine]).fetchone()
            if existing is None:
                con.execute("INSERT INTO xgraph_creations VALUES (?, ?, ?, ?, ?)",
                            [graph, engine, statement, source, now])
            else:
                con.execute(
                    "UPDATE xgraph_creations SET statement = ?, source = ?, ts = ?"
                    " WHERE graph = ? AND engine = ?",
                    [statement, source, now, graph, engine])
            return {"graph": graph, "engine": engine, "source": source, "ts": _iso(now)}
        finally:
            con.close()

    def get_creation(self, graph):
        """Most-recent recorded creation recipe for `graph` (any engine)."""
        con = self._meta_con()
        try:
            row = con.execute(
                "SELECT graph, engine, statement, source, ts FROM xgraph_creations"
                " WHERE graph = ? ORDER BY ts DESC LIMIT 1", [graph]).fetchone()
            if not row:
                return None
            return {"graph": row[0], "engine": row[1], "statement": row[2],
                    "source": row[3], "ts": _iso(row[4])}
        finally:
            con.close()
```

- [ ] **Step 5: Extend `clear_graph_metadata`**

In `clear_graph_metadata`, add alongside the existing DELETEs:

```python
            con.execute("DELETE FROM xgraph_creations WHERE graph = ?", [graph])
```

- [ ] **Step 6: Run — verify PASS**

```bash
cd /home/kkaramete/xgraph/backend
./.venv/bin/python -m pytest tests/test_creation_viewer.py -v
```
Expected: all 3 PASS.

- [ ] **Step 7: Commit**

```bash
cd /home/kkaramete/xgraph
git add backend/xgraph_gateway/compute/duckdb_engine.py backend/tests/test_creation_viewer.py
git commit -m "feat(build): xgraph_creations meta ledger (record/get creation recipe)"
```

---

## Task 2: `render_create_recipe` + `/create` recording + `/graph_ddl` fallback

**Files:**
- Modify: `backend/xgraph_gateway/app.py`.
- Modify: `backend/tests/test_creation_viewer.py`.

**Interfaces:**
- Produces: `render_create_recipe(spec: dict) -> str` (module scope). `/create` records via `record_creation`; `/graph_ddl` falls back to `get_creation` when the adapter statement is null.
- Consumes: `record_creation`/`get_creation` (Task 1), `_resolve_compute`, `_resolve_adapter`, `_resolve_engine`.

**Context:** For Kinetica-via-gateway, `spec["ddl"]` is the statement to record. For FalkorDB, render the `{tables, nodes, edges}` spec into a readable pseudo-recipe. `/graph_ddl` keeps Kinetica's live `show_graph` DDL as the primary (non-null) source; the ledger only fills the FalkorDB null case.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_creation_viewer.py`:

```python
from fastapi.testclient import TestClient
from xgraph_gateway.app import create_app, render_create_recipe
from xgraph_gateway.sessions import SessionStore
from xgraph_gateway.adapters.fake import FakeAdapter


def test_render_create_recipe_ddl_passthrough():
    assert render_create_recipe({"graph": "g", "ddl": "CREATE GRAPH g (...);"}) == "CREATE GRAPH g (...);"


def test_render_create_recipe_falkordb_spec():
    spec = {"graph": "banking", "tables": {"b2_nodes": "vertexes.parquet", "b2_edges": "edges.parquet"},
            "nodes": [{"sql": "SELECT id AS NODE FROM b2_nodes", "id": "NODE"}],
            "edges": [{"sql": "SELECT src AS SRC, dst AS DST FROM b2_edges", "source_key": "SRC", "target_key": "DST"}]}
    out = render_create_recipe(spec)
    assert "banking" in out
    assert "SELECT id AS NODE FROM b2_nodes" in out
    assert "SELECT src AS SRC, dst AS DST FROM b2_edges" in out


def _app(tmp_path):
    from xgraph_gateway.compute.duckdb_engine import DuckDBComputeEngine
    store = SessionStore(adapter_factory=lambda e, c=None: FakeAdapter(),
                        compute_factory=lambda e, c=None: DuckDBComputeEngine(meta_path=str(tmp_path / "m.duckdb")))
    return TestClient(create_app(adapter_factory=lambda e: FakeAdapter(),
                                compute=DuckDBComputeEngine(meta_path=str(tmp_path / "m2.duckdb")),
                                store=store))


def test_create_records_recipe_and_graph_ddl_returns_it(tmp_path):
    client = _app(tmp_path)
    sid = client.post("/connect", json={"graph": {"engine": "falkordb"}, "compute": {"engine": "duckdb"}}).json()["session"]
    spec = {"graph": "g1", "tables": {"t": "v.parquet"},
            "nodes": [{"sql": "SELECT id AS NODE FROM t", "id": "NODE"}], "edges": []}
    r = client.post("/create", json={"session": sid, "spec": spec})
    assert r.status_code == 200
    ddl = client.get("/graph_ddl", params={"session": sid, "graph": "g1"}).json()
    assert ddl["statement"] and "SELECT id AS NODE FROM t" in ddl["statement"]
    assert ddl["source"] == "xgraph:create-ledger"


def test_graph_ddl_null_for_unrecorded(tmp_path):
    client = _app(tmp_path)
    sid = client.post("/connect", json={"graph": {"engine": "falkordb"}, "compute": {"engine": "duckdb"}}).json()["session"]
    ddl = client.get("/graph_ddl", params={"session": sid, "graph": "never_built"}).json()
    assert ddl["statement"] is None
```

(`FakeAdapter.load_graph` returns a canned success and `creation_statement` inherits the base null, so the FalkorDB fallback path is exercised.)

- [ ] **Step 2: Run — verify FAIL**

```bash
cd /home/kkaramete/xgraph/backend
./.venv/bin/python -m pytest tests/test_creation_viewer.py -v
```
Expected: the new tests FAIL (`render_create_recipe` missing / no recording / no fallback).

- [ ] **Step 3: Add `render_create_recipe` (module scope, above `create_app`)**

```python
def render_create_recipe(spec: dict) -> str:
    """Render a /create spec as a readable 'how this graph was built' recipe.
    Kinetica-via-gateway carries raw DDL (returned verbatim); FalkorDB carries a
    {tables, nodes, edges} spec rendered as commented SELECT lines."""
    if not isinstance(spec, dict):
        return ""
    if spec.get("ddl"):
        return str(spec["ddl"])
    graph = spec.get("graph", "graph")
    lines = ['-- FalkorDB graph "' + str(graph) + '" (built via xGraph /create)']
    for n in spec.get("nodes", []) or []:
        if n.get("sql"):
            lines.append("-- NODES: " + n["sql"])
    for e in spec.get("edges", []) or []:
        if e.get("sql"):
            lines.append("-- EDGES: " + e["sql"])
    tables = spec.get("tables") or {}
    if tables:
        lines.append("-- tables: " + ", ".join(str(k) + " = " + str(v) for k, v in tables.items()))
    return "\n".join(lines)
```

- [ ] **Step 4: Record in `/create` (best-effort)**

Replace the `/create` body with:

```python
    @app.post("/create")
    def create(payload: dict = Body(...)):
        engine = payload.get("engine", "")
        session = payload.get("session")
        try:
            spec = payload["spec"]
            result = _resolve_adapter(session, engine).load_graph(spec)
            # Best-effort: record the recipe so List/Build can show it later.
            try:
                if isinstance(spec, dict) and spec.get("graph"):
                    _resolve_compute(session).record_creation(
                        spec["graph"], _resolve_engine(session, engine),
                        render_create_recipe(spec), "create")
            except Exception:
                pass
            return result
        except Exception as e:
            return _err(engine, e)
```

- [ ] **Step 5: Add the `/graph_ddl` ledger fallback**

Replace the `/graph_ddl` body with:

```python
    @app.get("/graph_ddl")
    def graph_ddl(graph: str, engine: str = "", session: str | None = None):
        try:
            stmt = _resolve_adapter(session, engine).creation_statement(graph)
            if stmt and stmt.get("statement"):
                return stmt
            recorded = _resolve_compute(session).get_creation(graph)
            if recorded and recorded.get("statement"):
                return {"statement": recorded["statement"], "source": "xgraph:create-ledger"}
            return stmt if stmt else {"statement": None, "source": None}
        except Exception as e:
            return _err(engine, e)
```

- [ ] **Step 6: Run new file + full suite**

```bash
cd /home/kkaramete/xgraph/backend
./.venv/bin/python -m pytest tests/test_creation_viewer.py -v
./.venv/bin/python -m pytest tests/ -q
```
Expected: new tests PASS; full suite green, no regressions (existing `/graph_ddl` Kinetica behavior unchanged — its non-null statement still wins).

- [ ] **Step 7: Commit**

```bash
cd /home/kkaramete/xgraph
git add backend/xgraph_gateway/app.py backend/tests/test_creation_viewer.py
git commit -m "feat(build): /create records creation recipe; /graph_ddl falls back to it (FalkorDB)"
```

---

## Task 3: List-panel DDL expander + gwClient thread + Build caption + version bump

**Files:**
- Modify: `frontend/XGraph.html` — `ListPanel` (≈L7398-7430), its mount (≈L9631-9637), the Build recipe-viewer caption (≈L7100-7113), `EXPLORER_VERSION` (L50).

**Interfaces:**
- Consumes: `gwClient.graphDdl(graph)` (existing). No new client method.
- Produces: a per-graph, expandable, copyable DDL view in List; Build unchanged behaviorally (caption clarified).

**Context:** `ListPanel` currently gets `graphs`/`activeGraph`/`onSelectGraph`/`graphSizes`/`onDeleteGraph` but NOT `gwClient`. Thread `gwClient` in, add a per-row toggle that fetches on first expand into a local `ddlByGraph` map. The row `<div>` has an `onClick` that selects the graph, so the toggle must `stopPropagation`.

- [ ] **Step 1: Thread `gwClient` into the `<ListPanel>` mount**

At the mount (≈L9632), add `gwClient={gwClient}`:

```jsx
                {activeAction === 'list' && (
                    <ListPanel
                        graphs={graphs} activeGraph={activeGraph} graphSizes={graphSizes}
                        gwClient={gwClient}
                        onSelectGraph={function(name){ setActiveGraph(name); setActiveAction('ontology'); }}
                        onDeleteGraph={handleDeleteGraph}
                    />
                )}
```

- [ ] **Step 2: Add state + a fetch/toggle in `ListPanel`**

In `function ListPanel(props)`, destructure `gwClient` and add state + a toggle handler (near the top of the function, after the existing prop reads):

```javascript
    var gwClient = props.gwClient;
    const [ddlOpen, setDdlOpen] = useState({});      // { [graph]: bool }
    const [ddlByGraph, setDdlByGraph] = useState({}); // { [graph]: string|null|'…loading' }
    function toggleDdl(name, e) {
        if (e) e.stopPropagation();
        setDdlOpen(function(prev){ var n = Object.assign({}, prev); n[name] = !prev[name]; return n; });
        if (gwClient && !Object.prototype.hasOwnProperty.call(ddlByGraph, name)) {
            setDdlByGraph(function(prev){ var n = Object.assign({}, prev); n[name] = '…loading'; return n; });
            gwClient.graphDdl(name)
                .then(function(r){ setDdlByGraph(function(prev){ var n = Object.assign({}, prev); n[name] = (r && r.statement) || null; return n; }); })
                .catch(function(){ setDdlByGraph(function(prev){ var n = Object.assign({}, prev); n[name] = null; return n; }); });
        }
    }
```

- [ ] **Step 3: Add the toggle span + expanded block to each row**

In the per-row map, add a "⌄ SQL" toggle next to the delete `🗑` span (inside the row `<div>`), and render the expanded DDL as a sibling block below the row (still inside the `key={name}` group). Wrap the existing row `<div>` and the new block in a fragment/container per graph. Concretely, add the toggle span after the delete span:

```jsx
                            <span onClick={function(e){ toggleDdl(name, e); }} title="Show the creation SQL / recipe"
                                  style={{ cursor:'pointer', color:'#0984e3', fontSize:11, fontWeight:700, marginLeft:8, userSelect:'none' }}>
                                {ddlOpen[name] ? '⌃ SQL' : '⌄ SQL'}
                            </span>
```

and, immediately after the row `<div>` (before the map callback's closing), the expanded panel:

```jsx
                        {ddlOpen[name] && (
                            <pre style={{ margin:'2px 0 8px', padding:10, background:'#f8fafc', border:'1px solid #eef1f4', borderRadius:6, fontSize:11, whiteSpace:'pre-wrap', overflowX:'auto', color:'#2d3436' }}>
                                {ddlByGraph[name] === '…loading' ? 'Loading…' : (ddlByGraph[name] || 'No recorded creation recipe.')}
                            </pre>
                        )}
```

(Ensure the map callback returns a single parent element — wrap the row `<div>` and the `<pre>` in an outer `<div key={name}>`; move the existing `key={name}` to that wrapper.)

- [ ] **Step 4: Build recipe-viewer caption tweak**

In `CreatePanel`'s recipe viewer, where the `activeDdl.statement` branch renders its caption (≈L7100-7101, "from Kinetica show_graph"), make the caption reflect the source generically, e.g. base it on `activeDdl.source` (show "from Kinetica show_graph" for `kinetica:show_graph`, "recorded at build time" for `xgraph:create-ledger`). Keep the `<pre>{activeDdl.statement}</pre>` as-is.

- [ ] **Step 5: Bump the version badge**

`EXPLORER_VERSION` (L50) `"0.9.0"` → `"0.10.0"`.

- [ ] **Step 6: esbuild + gateway 200**

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
git commit -m "feat(build): List per-graph '⌄ SQL' creation viewer; Build caption; v0.10.0"
```

---

## Manual (browser) acceptance — run after Task 3

Hard-reload `http://localhost:8090/`, confirm `v0.10.0`:

1. **Kinetica:** connect, **List** → click **⌄ SQL** on a graph → the `CREATE GRAPH` DDL from `show_graph` shows (copyable). Same DDL appears in **Build** for the active graph.
2. **FalkorDB:** build a graph via **Build → Tables/files** (structured or manual) → **List → ⌄ SQL** → the recorded recipe (NODES/EDGES SELECT lines) shows; it survives a reload (persisted).
3. A pre-existing / never-built-through-xGraph graph → **⌄ SQL** shows "No recorded creation recipe."

---

## Self-Review

- **Spec coverage:** persistent `xgraph_creations` ledger (Task 1); `/create` records + `/graph_ddl` fallback with Kinetica `show_graph` still primary (Task 2); List per-graph expander + Build (Task 3); FalkorDB = recorded recipe only, null when unrecorded (Tasks 2–3). Back-fill and DDL-editing deferred (spec).
- **Placeholder scan:** none — every backend step has exact code + pytest command + expected output; frontend steps cite exact anchors and give complete JSX.
- **Type/name consistency:** `record_creation(graph, engine, statement, source)` / `get_creation(graph)` (Task 1) called by `/create` and `/graph_ddl` (Task 2); `render_create_recipe(spec)` produced in Task 2 and imported in tests; `/graph_ddl` returns `{statement, source}` consumed by `gwClient.graphDdl` → List `ddlByGraph` (Task 3) and the existing Build viewer.
- **Scope:** creation viewer only. No back-fill, no editing, no schema-synthesis.
- **Risk sequencing:** meta store (Task 1, embedded) → endpoint recording+fallback (Task 2, Fake+embedded, best-effort so a build never breaks) → frontend (Task 3, esbuild + browser). Kinetica `/graph_ddl` unchanged (non-null wins); `/create` behavior preserved on the happy path.
```
