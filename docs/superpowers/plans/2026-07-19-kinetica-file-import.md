# Kinetica file-import Implementation Plan (`LOAD DATA INTO` for `register_file`)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend `POST /register_file` so that on **Kinetica** it imports a file into a real Kinetica table via `LOAD DATA INTO` (remote via a reused named `DATA SOURCE`; local via a Kinetica-readable/KiFS path). The imported table then appears in `/tables` and feeds the structured builder's `INPUT_TABLES` — parity with the DuckDB/FalkorDB file route from Slice B3.

**Architecture:** Backend adds a pure `load_data_sql(...)` builder and a `KineticaAdapter.register_file(...)` that runs it via the existing `_execute_ddl`, then reports the new table's columns. The `/register_file` endpoint branches by resolved engine: Kinetica → `adapter.register_file`; everything else → the existing B3 session path-registry. No secret handling (the DATA SOURCE is admin-pre-created and referenced by name). Frontend makes the existing ＋ File affordance engine-aware (extra prompts for Kinetica) and generalizes `gwClient.registerFile` to accept an options object.

**Tech Stack:** Backend — FastAPI + `gpudb` (Kinetica), run pytest from `backend/`. Frontend — React 18 UMD + Babel-standalone; esbuild JSX check; `gateway.js` Node-tested.

## Global Constraints

- **No `git commit` unless authorized** (CLAUDE.md); in a background job commits land on the worktree branch and are fast-forwarded onto `main`.
- **Self-contained backend** — no imports from `../falkor`/`../graphrag` (CLAUDE.md).
- **All Kinetica SQL is string-built** (`KineticaSource` has no parameterized path) — every identifier goes through `_validate_table_ident` (safe_ident per dotted part) and every string literal through `_escape_sql_literal` (doubles `'`). Never raw-interpolate a path/data-source.
- **Gateway stays secret-free:** reuse an existing named `DATA SOURCE`; do NOT emit `CREATE CREDENTIAL` / `CREATE DATA SOURCE`.
- **Re-import appends** (LOAD DATA semantics) — documented, no dedup/replace in v1.
- **Tests:** run from `backend/` with `./.venv/bin/python -m pytest tests/ -v`. Pure builder tests need no DB; Kinetica live tests SKIP when unreachable (or when no test DATA SOURCE/path is configured). Current baseline: 339 passed / 43 skipped.
- **Frontend edits validated by the esbuild JSX check** (below) → `ESBUILD_OK`; `gateway.js` via `node tests/test_client.mjs`.
- **Version badge:** bump `EXPLORER_VERSION` `0.8.0` → `0.9.0` in the final frontend task.
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
  - `backend/xgraph_gateway/adapters/kinetica_adapter.py` — module-level `load_data_sql`, `_detect_format`, `_derive_table_name`; `KineticaAdapter.register_file` (Tasks 1–2).
  - `backend/xgraph_gateway/adapters/base.py` — default `register_file` raising `NotImplementedError` (Task 1).
  - `backend/xgraph_gateway/adapters/fake.py` — canned `register_file` for routing tests (Task 3).
  - `backend/xgraph_gateway/app.py` — `POST /register_file` engine branch (Task 3).
  - `backend/tests/test_kinetica_file_import.py` — new (Tasks 1–3).
- **Frontend:**
  - `frontend/gateway.js` — `registerFile` accepts a string OR an options object (Task 4).
  - `frontend/XGraph.html` — engine-aware ＋ File prompts; `graphEngine` passed to `CreateHelperPanel`; `registerFileGW` passes the arg through; version bump (Task 4).
  - `frontend/tests/test_client.mjs` — assert the options-object form (Task 4).

---

## Task 1: Pure `load_data_sql` builder + helpers + base default

**Files:**
- Modify: `backend/xgraph_gateway/adapters/kinetica_adapter.py` (module scope, near the other pure builders ≈after `_escape_sql_literal`, L160).
- Modify: `backend/xgraph_gateway/adapters/base.py`.
- Create: `backend/tests/test_kinetica_file_import.py`.

**Interfaces:**
- Produces:
  - `load_data_sql(table: str, path: str, fmt: str, data_source: str | None = None) -> str` — the `LOAD DATA INTO` statement.
  - `_detect_format(path: str) -> str` — format from extension (default `"parquet"`).
  - `_derive_table_name(path: str) -> str` — sanitized table name from the filename stem.
  - `GraphEngineAdapter.register_file(self, path, table=None, fmt=None, data_source=None) -> dict` (base) — `raise NotImplementedError`.
- Consumes: existing `_validate_table_ident`, `_escape_sql_literal` (kinetica_adapter.py).

**Context:** These are pure functions (no DB), unit-testable like the existing `_dot_from_show_graph`/`graph_from_gql_result` builders. `_escape_sql_literal` returns the escaped *inner* string (no surrounding quotes), so wrap in `'…'`.

- [ ] **Step 1: Write the failing unit tests**

Create `backend/tests/test_kinetica_file_import.py`:

```python
import pytest
from xgraph_gateway.adapters import kinetica_adapter as ka


def test_load_data_sql_remote_with_data_source():
    sql = ka.load_data_sql("myschema.airports", "s3://bkt/a.parquet", "parquet", "my_s3")
    assert "LOAD DATA INTO myschema.airports" in sql
    assert "FROM FILE PATHS 's3://bkt/a.parquet'" in sql
    assert "FORMAT PARQUET" in sql
    assert "WITH OPTIONS (DATA SOURCE = 'my_s3')" in sql
    assert sql.rstrip().endswith(";")


def test_load_data_sql_local_no_data_source():
    sql = ka.load_data_sql("t", "kifs://u/a.csv", "csv", None)
    assert "FROM FILE PATHS 'kifs://u/a.csv'" in sql
    assert "FORMAT CSV" in sql
    assert "DATA SOURCE" not in sql


def test_load_data_sql_rejects_bad_format():
    with pytest.raises(ValueError):
        ka.load_data_sql("t", "a.parquet", "exe", None)


def test_load_data_sql_rejects_bad_table_ident():
    with pytest.raises(Exception):  # MappingError from safe_ident
        ka.load_data_sql("bad name!", "a.parquet", "parquet", None)


def test_load_data_sql_escapes_quotes_in_path():
    sql = ka.load_data_sql("t", "s3://b/o'x.parquet", "parquet", None)
    assert "'s3://b/o''x.parquet'" in sql  # doubled quote


def test_detect_format():
    assert ka._detect_format("/x/a.CSV") == "csv"
    assert ka._detect_format("s3://b/a.parquet?x=1") == "parquet"
    assert ka._detect_format("a.jsonl") == "json"
    assert ka._detect_format("a.unknown") == "parquet"


def test_derive_table_name():
    assert ka._derive_table_name("s3://b/My File.parquet") == "My_File"
    assert ka._derive_table_name("/p/2020data.csv") == "t_2020data"


def test_base_register_file_not_implemented():
    from xgraph_gateway.adapters.base import GraphEngineAdapter
    class A(GraphEngineAdapter):
        def list_graphs(self): return []
        def get_schema(self, g, options=None): return {}
        def run_query(self, g, c, timeout=60000): return {}
        def fetch_entities(self, g, limit, offset=0): return {}
        def get_record(self, g, i): return {}
        def load_graph(self, spec): return {}
        def graph_sizes(self): return {}
    with pytest.raises(NotImplementedError):
        A().register_file("a.parquet")
```

- [ ] **Step 2: Run — verify FAIL**

```bash
cd /home/kkaramete/xgraph/backend
./.venv/bin/python -m pytest tests/test_kinetica_file_import.py -v
```
Expected: FAIL (`load_data_sql` / `_detect_format` / `_derive_table_name` / base `register_file` don't exist).

- [ ] **Step 3: Add the helpers + builder to `kinetica_adapter.py`**

After `_escape_sql_literal` (≈L160), add:

```python
_LOAD_FORMATS = {"parquet", "csv", "json", "shapefile", "avro"}


def _detect_format(path: str) -> str:
    p = str(path).lower().split("?")[0]
    if p.endswith((".csv", ".tsv")):
        return "csv"
    if p.endswith((".json", ".jsonl", ".ndjson")):
        return "json"
    if p.endswith(".avro"):
        return "avro"
    if p.endswith(".shp"):
        return "shapefile"
    return "parquet"


def _derive_table_name(path: str) -> str:
    import os
    import re
    base = os.path.basename(str(path).split("?")[0])
    stem = base.rsplit(".", 1)[0] if "." in base else base
    stem = re.sub(r"[^A-Za-z0-9_]", "_", stem) or "imported"
    if stem[0].isdigit():
        stem = "t_" + stem
    return stem


def load_data_sql(table: str, path: str, fmt: str, data_source: str | None = None) -> str:
    """LOAD DATA INTO statement. `table` validated via safe_ident; `path` and
    `data_source` escaped as SQL string literals. DATA SOURCE clause only when
    a name is given (remote); omitted for a Kinetica-readable path (local/KiFS)."""
    tbl = _validate_table_ident(table)
    f = (fmt or "parquet").lower()
    if f not in _LOAD_FORMATS:
        raise ValueError(f"unsupported LOAD format: {fmt!r}")
    sql = ("LOAD DATA INTO " + tbl +
           "\nFROM FILE PATHS '" + _escape_sql_literal(path) + "'" +
           "\nFORMAT " + f.upper())
    if data_source:
        sql += "\nWITH OPTIONS (DATA SOURCE = '" + _escape_sql_literal(data_source) + "')"
    return sql + ";"
```

- [ ] **Step 4: Add the base default `register_file`**

In `backend/xgraph_gateway/adapters/base.py`, after `graph_grammar` (the method added in the grammar slice), add:

```python
    def register_file(self, path, table=None, fmt=None, data_source=None) -> dict:
        """Import a file as a table/relation for the builder. Engine-specific;
        only Kinetica implements server-side ingestion here (DuckDB/FalkorDB are
        handled by the /register_file session path-registry, not the adapter)."""
        raise NotImplementedError("register_file not supported for this engine")
```

- [ ] **Step 5: Run — verify PASS**

```bash
cd /home/kkaramete/xgraph/backend
./.venv/bin/python -m pytest tests/test_kinetica_file_import.py -v
```
Expected: all Step-1 tests PASS.

- [ ] **Step 6: Commit**

```bash
cd /home/kkaramete/xgraph
git add backend/xgraph_gateway/adapters/kinetica_adapter.py backend/xgraph_gateway/adapters/base.py backend/tests/test_kinetica_file_import.py
git commit -m "feat(build): load_data_sql builder + base register_file default (Kinetica file-import)"
```

---

## Task 2: `KineticaAdapter.register_file`

**Files:**
- Modify: `backend/xgraph_gateway/adapters/kinetica_adapter.py` (add method to `KineticaAdapter`, near `list_columns`/`graph_grammar`).
- Modify: `backend/tests/test_kinetica_file_import.py` (live-skip integration test).

**Interfaces:**
- Consumes: `load_data_sql`, `_detect_format`, `_derive_table_name` (Task 1); existing `self._execute_ddl` and `self._current_columns`.
- Produces: `KineticaAdapter.register_file(self, path, table=None, fmt=None, data_source=None) -> dict` → `{"name": <table>, "type": "table", "columns": [...]}`.

**Context:** `_execute_ddl` already runs `execute_sql` and raises `RuntimeError(<kinetica message>)` on failure, so LOAD errors surface cleanly. Format defaults from the extension; table name derives from the filename when not supplied.

- [ ] **Step 1: Add the method**

In `class KineticaAdapter`, after `list_columns` (≈L962), add:

```python
    def register_file(self, path, table=None, fmt=None, data_source=None):
        """Import a file into a real Kinetica table via LOAD DATA INTO, then
        report its columns. Remote sources use a reused named DATA SOURCE;
        local/KiFS/server-readable paths omit it. Re-import appends."""
        if not path:
            raise ValueError("path is required")
        fmt = fmt or _detect_format(path)
        table = table or _derive_table_name(path)
        self._execute_ddl(load_data_sql(table, path, fmt, data_source or None))
        return {"name": table, "type": "table", "columns": self._current_columns(table)}
```

- [ ] **Step 2: Add a live-skip integration test**

Append to `backend/tests/test_kinetica_file_import.py` (env-gated on a real DATA SOURCE + path so it only runs where configured):

```python
import os
from xgraph_gateway import config


def _kinetica_or_skip():
    try:
        from xgraph_gateway.adapters.kinetica_adapter import KineticaAdapter
        a = KineticaAdapter(config.load_settings())
        a.list_graphs()
        return a
    except Exception as e:
        pytest.skip(f"Kinetica unreachable: {e}")


def test_kinetica_register_file_live():
    # Requires a reachable file Kinetica can read. Configure via env:
    #   XG_TEST_LOAD_PATH   (e.g. kifs://… or s3://…)  [required]
    #   XG_TEST_DATA_SOURCE (name)                     [optional, for remote]
    path = os.environ.get("XG_TEST_LOAD_PATH")
    if not path:
        pytest.skip("set XG_TEST_LOAD_PATH to run the live LOAD DATA test")
    a = _kinetica_or_skip()
    tbl = "xg_test_import_tmp"
    try:
        res = a.register_file(path, table=tbl,
                              data_source=os.environ.get("XG_TEST_DATA_SOURCE"))
        assert res["name"] == tbl and res["type"] == "table"
        assert isinstance(res["columns"], list) and res["columns"]
        assert any(t["name"].endswith(tbl) or t["name"] == tbl for t in a.list_tables())
    finally:
        try:
            a._execute_ddl(f"DROP TABLE IF EXISTS {tbl};")
        except Exception:
            pass
```

- [ ] **Step 3: Run — unit pass, live test skips unless configured**

```bash
cd /home/kkaramete/xgraph/backend
./.venv/bin/python -m pytest tests/test_kinetica_file_import.py -v
```
Expected: pure tests PASS; `test_kinetica_register_file_live` SKIPS (no `XG_TEST_LOAD_PATH`).

- [ ] **Step 4: Commit**

```bash
cd /home/kkaramete/xgraph
git add backend/xgraph_gateway/adapters/kinetica_adapter.py backend/tests/test_kinetica_file_import.py
git commit -m "feat(build): KineticaAdapter.register_file (LOAD DATA INTO -> real table)"
```

---

## Task 3: `POST /register_file` engine branch + FakeAdapter routing test

**Files:**
- Modify: `backend/xgraph_gateway/app.py` (`/register_file`, ≈L181-195).
- Modify: `backend/xgraph_gateway/adapters/fake.py` (canned `register_file`).
- Modify: `backend/tests/test_kinetica_file_import.py` (routing test).

**Interfaces:**
- Consumes: `_resolve_engine(session, engine)` (L72), `_resolve_adapter`, `_resolve_compute`, `_sess`, `store.register_file` (existing).
- Produces: `POST /register_file` routes Kinetica payloads (`{path, table?, format?, data_source?}`) to `adapter.register_file`; all other engines keep the B3 session-registry path.

**Context:** The current endpoint always does the DuckDB/FalkorDB session-registry path. Branch on the resolved engine so Kinetica materializes a table instead.

- [ ] **Step 1: Add `register_file` to `FakeAdapter`**

In `backend/xgraph_gateway/adapters/fake.py`, add to `class FakeAdapter`:

```python
    def register_file(self, path, table=None, fmt=None, data_source=None):
        return {"name": table or "imported", "type": "table",
                "columns": ["NODE", "AMOUNT"]}
```

- [ ] **Step 2: Branch the endpoint by engine**

Replace the `/register_file` body in `app.py` with:

```python
    @app.post("/register_file")
    def register_file(payload: dict = Body(...)):
        engine = payload.get("engine", "")
        session = payload.get("session")
        path = payload.get("path")
        try:
            if _resolve_engine(session, engine) == "kinetica":
                return _resolve_adapter(session, engine).register_file(
                    path, table=payload.get("table"),
                    fmt=payload.get("format"), data_source=payload.get("data_source"))
            # Non-Kinetica: remember the path in the session (files ARE relations).
            if not _sess(session):
                raise ValueError("register_file requires a live session (connect first)")
            if not path:
                raise ValueError("path is required")
            columns = _resolve_compute(session).describe_source(path)
            store.register_file(session, path)
            return {"name": path, "type": "file", "columns": columns}
        except Exception as e:
            return _err(engine, e)
```

- [ ] **Step 3: Add the routing test**

Append to `backend/tests/test_kinetica_file_import.py`:

```python
from fastapi.testclient import TestClient
from xgraph_gateway.app import create_app
from xgraph_gateway.sessions import SessionStore
from xgraph_gateway.adapters.fake import FakeAdapter


def test_register_file_routes_kinetica_to_adapter(tmp_path):
    from xgraph_gateway.compute.duckdb_engine import DuckDBComputeEngine
    store = SessionStore(adapter_factory=lambda e, c=None: FakeAdapter(),
                        compute_factory=lambda e, c=None: DuckDBComputeEngine(meta_path=str(tmp_path / "m.duckdb")))
    client = TestClient(create_app(adapter_factory=lambda e: FakeAdapter(),
                                  compute=DuckDBComputeEngine(meta_path=str(tmp_path / "m2.duckdb")),
                                  store=store))
    sid = client.post("/connect", json={"graph": {"engine": "kinetica"},
                                        "compute": {"engine": "duckdb"}}).json()["session"]
    r = client.post("/register_file", json={"session": sid, "path": "s3://b/a.parquet",
                                            "table": "airports", "data_source": "my_s3"})
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "airports" and body["type"] == "table"
    assert body["columns"] == ["NODE", "AMOUNT"]
```

(`_resolve_engine` returns the session's `graph_engine`, `"kinetica"`, so the endpoint takes the adapter branch even though `FakeAdapter` is the stand-in.)

- [ ] **Step 4: Run new file + full suite**

```bash
cd /home/kkaramete/xgraph/backend
./.venv/bin/python -m pytest tests/test_kinetica_file_import.py -v
./.venv/bin/python -m pytest tests/ -q
```
Expected: new tests PASS (live one SKIPS); full suite green, no regressions (B3 DuckDB/FalkorDB register_file tests still pass).

- [ ] **Step 5: Commit**

```bash
cd /home/kkaramete/xgraph
git add backend/xgraph_gateway/app.py backend/xgraph_gateway/adapters/fake.py backend/tests/test_kinetica_file_import.py
git commit -m "feat(build): /register_file routes Kinetica to LOAD DATA (adapter), others to session registry"
```

---

## Task 4: Frontend — engine-aware ＋ File + `registerFile` options object + version bump

**Files:**
- Modify: `frontend/gateway.js` (`registerFile` accepts string or object).
- Modify: `frontend/tests/test_client.mjs`.
- Modify: `frontend/XGraph.html` — `registerFileGW` passes arg through; `graphEngine` prop to `CreateHelperPanel`; engine-aware ＋ File prompts; `EXPLORER_VERSION` (L50).

**Interfaces:**
- Consumes: `gwClient.registerFile(argOrPath)`.
- Produces: for Kinetica, ＋ File collects `path` + optional `DATA SOURCE` + optional `table` and imports via LOAD DATA; the new table then appears in `/tables`.

**Context:** `registerFile` currently sends `{path}`. Generalize it: a string stays `{path}` (DuckDB/FalkorDB back-compat); an object is sent verbatim (`{path, data_source, table, format}`). The ＋ File `onClick` already lives in `CreateHelperPanel` — give it `props.graphEngine` to branch the prompts.

- [ ] **Step 1: Generalize `gwClient.registerFile`**

In `frontend/gateway.js`, replace the `registerFile` line with:

```javascript
      registerFile: function (arg) {
        var payload = (typeof arg === "string") ? { path: arg } : (arg || {});
        return postJSONWithAuth("/register_file", payload);
      },
```

- [ ] **Step 2: Add a Node test for the options form**

In `frontend/tests/test_client.mjs`, extend the existing `registerFile` block (before `console.log("client OK")`) with an object call:

```javascript
  // registerFile(object): Kinetica form carries data_source/table/format
  let regUrl2, regBody2;
  const regClient2 = g.makeClient("http://gw", async (url, opts) => {
    if (url === "http://gw/connect") return { ok: true, json: async () => ({ session: "s1", graphs: [] }) };
    regUrl2 = url; regBody2 = JSON.parse(opts.body);
    return { ok: true, json: async () => ({ name: "airports", type: "table", columns: ["id"] }) };
  });
  await regClient2.connect({ engine: "kinetica", conn: {} }, { engine: "duckdb", conn: {} });
  await regClient2.registerFile({ path: "s3://b/a.parquet", data_source: "my_s3", table: "airports" });
  assert.equal(regUrl2, "http://gw/register_file");
  assert.equal(regBody2.path, "s3://b/a.parquet");
  assert.equal(regBody2.data_source, "my_s3");
  assert.equal(regBody2.table, "airports");
  console.log("ok: registerFile options form");
```

- [ ] **Step 3: `registerFileGW` passes the arg through**

In `frontend/XGraph.html`, change `registerFileGW` to accept and forward the arg (string or object):

```javascript
    function registerFileGW(arg) {
        if (!arg || !gwClient) return Promise.resolve(null);
        return gwClient.registerFile(arg).then(function(res) {
            return gwClient.tables().then(function(list) {
                if (Array.isArray(list)) setTables(list);
                return res;
            });
        });
    }
```

- [ ] **Step 4: Pass `graphEngine` to both `CreateHelperPanel` mounts**

In `CreatePanel`, add `graphEngine={graphEngine}` to each `<CreateHelperPanel …>` (both the Kinetica and non-Kinetica mounts). `graphEngine` is already in scope in `CreatePanel`.

- [ ] **Step 5: Engine-aware ＋ File prompts**

In `CreateHelperPanel`'s ＋ File `onClick`, replace the body with:

```javascript
                                          if (!props.onRegisterFile) { window.alert('File registration needs a live connection.'); return; }
                                          var p = window.prompt('File path/URL to import (s3://…, kifs://…, or a Kinetica-readable path):', '');
                                          if (!p) return;
                                          var arg = p.trim();
                                          if (props.graphEngine === 'kinetica') {
                                              var ds = (window.prompt('DATA SOURCE name for a remote path (leave blank for a KiFS/server-readable path):', '') || '').trim();
                                              var tbl = (window.prompt('Target table name (leave blank to derive from the filename):', '') || '').trim();
                                              arg = { path: p.trim(), data_source: ds || undefined, table: tbl || undefined };
                                          }
                                          props.onRegisterFile(arg)
                                              .then(function(res){ if (res && res.name) { setChSectionTable(function(prev){ var n = Object.assign({}, prev); n[comp] = res.name; return n; }); } })
                                              .catch(function(e){ window.alert('Could not register file: ' + (e && e.message ? e.message : e)); });
```

- [ ] **Step 6: Bump the version badge**

`EXPLORER_VERSION` (L50) `"0.8.0"` → `"0.9.0"`.

- [ ] **Step 7: esbuild + node tests + gateway 200**

```bash
cd /home/kkaramete/xgraph/frontend
end=$(grep -n '</script>' XGraph.html | tail -1 | cut -d: -f1)
sed -n "47,$((end-1))p" XGraph.html | ./node_modules/.bin/esbuild --loader=jsx > /dev/null && echo ESBUILD_OK || echo ESBUILD_FAIL
node tests/test_client.mjs
cd /home/kkaramete/xgraph && (./xgraph status >/dev/null 2>&1 || ./xgraph start) && sleep 1 && curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8090/
```
Expected: `ESBUILD_OK`; `client OK` (+ the two registerFile lines); `200`.

- [ ] **Step 8: Commit**

```bash
cd /home/kkaramete/xgraph
git add frontend/gateway.js frontend/tests/test_client.mjs frontend/XGraph.html
git commit -m "feat(build): engine-aware ＋ File (Kinetica LOAD DATA prompts); registerFile options object; v0.9.0"
```

---

## Manual (browser) acceptance — run after Task 4

Hard-reload `http://localhost:8090/`, confirm `v0.9.0`, connect **Kinetica**, **Build → Tables / files**:

1. Click **＋ File** on a section → prompts for path, then DATA SOURCE name, then target table.
2. Enter a remote path + an existing DATA SOURCE name (or a `kifs://` path + blank DATA SOURCE) → the file imports (`LOAD DATA INTO`), the new table appears in the section's Default table and in the dropdowns, and its columns autocomplete.
3. A bad path / unknown DATA SOURCE shows an alert with Kinetica's error; nothing is added.
4. Fill NODES/EDGES from the imported table, **Generate** → the DDL references it; **Create / Replace graph** builds.
5. **Regression:** FalkorDB ＋ File still uses the single path prompt and the B3 session-registry path.

---

## Self-Review

- **Spec coverage:** `LOAD DATA INTO` materialize (Tasks 1–2); reuse named DATA SOURCE, no secret handling (builder emits `DATA SOURCE=` only when supplied — Task 1); local path = no DATA SOURCE clause (Task 1); real table surfaced via existing `list_tables` (no session registry for Kinetica — Task 3); engine-aware frontend (Task 4). Append-on-reimport and KiFS byte-upload deferred (stated in spec).
- **Placeholder scan:** none — every step has exact code + pytest/esbuild commands + expected output.
- **Type/name consistency:** `load_data_sql(table, path, fmt, data_source)` (Task 1) called by `KineticaAdapter.register_file` (Task 2) and asserted in tests; endpoint payload `{path, table, format, data_source}` (Task 3) matches `gwClient.registerFile` object form (Task 4) and the adapter kwargs (`fmt=payload.get("format")`); return shape `{name, type, columns}` consumed by `registerFileGW`/＋ File (Task 4).
- **Scope:** Kinetica file-import via reused DATA SOURCE + LOAD DATA only. CREATE CREDENTIAL/DATA SOURCE provisioning, KiFS byte-upload, replace/dedup, and external-table mode are deferred.
- **Risk sequencing:** pure builder + base default (Task 1, fully headless) → adapter method (Task 2, live-skip) → endpoint routing (Task 3, Fake headless) → frontend (Task 4, esbuild + browser). DuckDB/FalkorDB register_file path is untouched (the endpoint's else-branch), so B3 can't regress.
```
