# Unified "Build" Panel — Slice B3 Implementation Plan (`POST /register_file` — files as first-class sources)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user turn a **file path** (Parquet/CSV, local or `s3://`/`http(s)://`) into a pickable source for the structured builder on **DuckDB→FalkorDB**, so "build a graph from files" works the way explorer did — the file shows up in the section table dropdowns and its columns autocomplete — without hand-typing paths blind.

**Architecture:** For DuckDB/FalkorDB the build already consumes paths directly (`FalkorDBAdapter.load_graph` runs `spec["tables"]` = `{name: path}` through `resolve_data_path` → `DuckDBSource.connect`), and `/columns` already describes any path (`FalkorDBAdapter.list_columns` → `describe_relation`). The **only** gap is discovery: `list_tables()` returns `[]` for FalkorDB because there's no server catalog to enumerate. So `register_file` records the validated path in the **session** (the one place per-connection state already lives), and `GET /tables` merges those registered files into its result. No persistent DuckDB catalog is introduced — paths are resolved at build time exactly as today. Kinetica file-import (external tables / KiFS / DATA SOURCE) is **out of scope** (Kinetica already lists its real tables via `/tables`; importing new files is a separate ingestion subsystem — see "Deferred").

**Tech Stack:** Backend — FastAPI + embedded DuckDB (`backend/.venv`, run pytest from `backend/`). Frontend — React 18 UMD + Babel-standalone (no build step); JSX validated by the local `esbuild` check; behavior is browser-driven. `gateway.js` client is Node-tested.

## Global Constraints

- **No `git commit` unless authorized** (CLAUDE.md). This work stream commits locally per convention; in a background job commits land on the worktree branch and are fast-forwarded onto `main`.
- **Self-contained backend** — no imports from `../falkor`/`../graphrag`; `graph_loader` is vendored (CLAUDE.md).
- **Untrusted paths:** every path reaching DuckDB goes through `config.resolve_data_path` and the existing single-quote guard in `describe_source` (raises `ValueError("unsafe source path: …")`). `register_file` must reuse that guard — never interpolate a raw path.
- **Degrade, don't block:** a session-less `register_file` call returns a clear error (files can only be remembered against a live session); `/tables` still returns the adapter list if the session has no files.
- **DuckDB tests are embedded (no skip); Kinetica/FalkorDB live tests SKIP when unreachable.** Backend suite runs from `backend/` with `./.venv/bin/python -m pytest tests/ -v` (currently 333 passed / 43 skipped).
- **Every frontend edit validated by the esbuild JSX check** (below) → `ESBUILD_OK`; `gateway.js` via `node tests/test_client.mjs`.
- **Version badge:** bump `EXPLORER_VERSION` `0.7.0` → `0.8.0` in the final frontend task.
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
  - `backend/xgraph_gateway/sessions.py` — add a `register_file(session_id, path)` method + `"files"` key on the session dict (Task 1).
  - `backend/xgraph_gateway/app.py` — `POST /register_file` endpoint + merge session files into `GET /tables` + short-circuit `GET /columns` for registered files (Task 2).
  - `backend/tests/test_register_file.py` — new test file (Tasks 1–2).
- **Frontend:**
  - `frontend/gateway.js` — `registerFile(path)` client method (Task 3).
  - `frontend/tests/test_client.mjs` — assert the new method (Task 3).
  - `frontend/XGraph.html` — a "＋ File" affordance next to the builder's Default-table input that prompts for a path, calls `registerFile`, refreshes `tables`; version bump (Task 4).

No files deleted.

---

## Task 1: Session file registry (`SessionStore.register_file`)

**Files:**
- Modify: `backend/xgraph_gateway/sessions.py`.
- Create: `backend/tests/test_register_file.py`.

**Interfaces:**
- Produces:
  - Session dict gains a `"files"` key: `list[str]` of registered (raw, unresolved) paths, initialised `[]` in `create()`.
  - `SessionStore.register_file(self, session_id: str, path: str) -> list[str]` — appends `path` (deduped) to that session's `files` and returns the current list. Raises `KeyError` for an unknown session (mirrors `get`).

**Context:** `SessionStore` (sessions.py) has only `create()` and `get()`; sessions are dicts holding `adapter`/`compute`/`graph_engine`/`compute_engine`/`extract_mode`. Registered paths are per-connection state, so they belong on the session. Store the **raw** path the user supplied — `resolve_data_path` is applied at describe/build time, exactly as `load_graph` and `describe_source` already do.

- [ ] **Step 1: Add `"files": []` to the session dict in `create()`**

In `sessions.py`, in `create(...)`, add the key to the stored dict:

```python
        self._sessions[session_id] = {
            "adapter": self._adapter_factory(graph_engine, graph_conn),
            "compute": self._compute_factory(compute_engine, compute_conn),
            "graph_engine": graph_engine,
            "compute_engine": compute_engine,
            "extract_mode": extract_mode or "sequential",
            "files": [],
        }
```

- [ ] **Step 2: Add the `register_file` method**

After `get(...)` in `SessionStore`, add:

```python
    def register_file(self, session_id: str, path: str) -> list[str]:
        """Remember a data-file path against a session so it shows up as a
        pickable builder source. Raw path stored verbatim (resolved at
        describe/build time). Deduped, insertion-ordered."""
        s = self.get(session_id)  # raises KeyError for unknown session
        files = s.setdefault("files", [])
        if path and path not in files:
            files.append(path)
        return files
```

- [ ] **Step 3: Write the failing unit test**

Create `backend/tests/test_register_file.py`:

```python
import pytest
from xgraph_gateway.sessions import SessionStore
from xgraph_gateway.adapters.fake import FakeAdapter


def _store():
    return SessionStore(adapter_factory=lambda e, c=None: FakeAdapter(),
                        compute_factory=lambda e, c=None: object())


def test_register_file_records_path_on_session():
    st = _store()
    sid = st.create("fake", None, "duckdb", None)
    st.register_file(sid, "vertexes.parquet")
    st.register_file(sid, "vertexes.parquet")  # dedup
    st.register_file(sid, "edges.parquet")
    assert st.get(sid)["files"] == ["vertexes.parquet", "edges.parquet"]


def test_register_file_unknown_session_raises():
    st = _store()
    with pytest.raises(KeyError):
        st.register_file("s999", "x.parquet")


def test_new_session_has_empty_files():
    st = _store()
    sid = st.create("fake", None, "duckdb", None)
    assert st.get(sid)["files"] == []
```

- [ ] **Step 4: Run the tests**

```bash
cd /home/kkaramete/xgraph/backend
./.venv/bin/python -m pytest tests/test_register_file.py -v
```
Expected: all 3 PASS.

- [ ] **Step 5: Commit**

```bash
cd /home/kkaramete/xgraph
git add backend/xgraph_gateway/sessions.py backend/tests/test_register_file.py
git commit -m "feat(build): SessionStore.register_file remembers file paths per session"
```

---

## Task 2: `POST /register_file` endpoint + merge files into `/tables` and `/columns`

**Files:**
- Modify: `backend/xgraph_gateway/app.py` (add `POST /register_file`; extend `GET /tables` and `GET /columns`).
- Modify: `backend/tests/test_register_file.py` (gateway tests).

**Interfaces:**
- Consumes: `_sess(session)`, `_resolve_compute(session)` (returns the session's `DuckDBComputeEngine`), `store.register_file(session, path)` (Task 1), `ComputeEngine.describe_source(path)` (validates + returns columns; raises on unreadable/unsafe path).
- Produces:
  - `POST /register_file {path, session, engine}` → `{"name": path, "type": "file", "columns": [str, ...]}` on success; requires a live session (else 400).
  - `GET /tables` now appends `{"name": <path>, "type": "file"}` for each registered file in the session.
  - `GET /columns?table=<path>` returns the file's columns via the compute engine when `<path>` is a registered file (otherwise the existing adapter path).

**Context:** `describe_source` doubles as validation — if the path can't be read (missing file, bad format, quote-injection), it raises and the endpoint returns the uniform error envelope, so the UI shows *why* a file was rejected instead of silently adding a dud. Registration requires a session because there's nowhere else to persist per-connection state (the module-level default compute has no session scope).

- [ ] **Step 1: Write the failing gateway tests**

Append to `backend/tests/test_register_file.py`:

```python
import duckdb
from fastapi.testclient import TestClient
from xgraph_gateway.app import create_app
from xgraph_gateway.compute.duckdb_engine import DuckDBComputeEngine


def _app(tmp_path):
    # Real session store + FalkorDB-style routing via FakeAdapter; isolated meta db.
    store = SessionStore(adapter_factory=lambda e, c=None: FakeAdapter(),
                        compute_factory=lambda e, c=None: DuckDBComputeEngine(meta_path=str(tmp_path / "m.duckdb")))
    return TestClient(create_app(adapter_factory=lambda e: FakeAdapter(),
                                compute=DuckDBComputeEngine(meta_path=str(tmp_path / "m2.duckdb")),
                                store=store))


def _parquet(tmp_path):
    p = tmp_path / "v.parquet"
    con = duckdb.connect()
    con.execute(f"COPY (SELECT 1 AS id, 'bank' AS label) TO '{p}' (FORMAT PARQUET)")
    con.close()
    return str(p)


def test_register_file_validates_and_lists(tmp_path):
    client = _app(tmp_path)
    sid = client.post("/connect", json={"graph": {"engine": "fake"}, "compute": {"engine": "duckdb"}}).json()["session"]
    p = _parquet(tmp_path)
    r = client.post("/register_file", json={"session": sid, "path": p})
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == p and body["type"] == "file"
    assert body["columns"] == ["id", "label"]
    # now /tables includes it
    tbls = client.get("/tables", params={"session": sid}).json()
    assert {"name": p, "type": "file"} in tbls
    # and /columns describes it
    cols = client.get("/columns", params={"session": sid, "table": p}).json()
    assert cols == ["id", "label"]


def test_register_file_bad_path_errors(tmp_path):
    client = _app(tmp_path)
    sid = client.post("/connect", json={"graph": {"engine": "fake"}, "compute": {"engine": "duckdb"}}).json()["session"]
    r = client.post("/register_file", json={"session": sid, "path": "/no/such/file.parquet"})
    assert r.status_code >= 400
    assert "error" in r.json()


def test_register_file_requires_session(tmp_path):
    client = _app(tmp_path)
    r = client.post("/register_file", json={"path": "x.parquet"})
    assert r.status_code >= 400
    assert "error" in r.json()
```

- [ ] **Step 2: Run tests to confirm they FAIL**

```bash
cd /home/kkaramete/xgraph/backend
./.venv/bin/python -m pytest tests/test_register_file.py -v
```
Expected: the 3 gateway tests FAIL (route missing → 404/405); the 3 Task-1 unit tests still PASS.

- [ ] **Step 3: Add the `/register_file` endpoint**

In `app.py`, after the `/columns` endpoint (≈L165-171), add:

```python
    @app.post("/register_file")
    def register_file(payload: dict = Body(...)):
        engine = payload.get("engine", "")
        session = payload.get("session")
        path = payload.get("path")
        try:
            if not _sess(session):
                raise ValueError("register_file requires a live session (connect first)")
            if not path:
                raise ValueError("path is required")
            # describe_source validates readability + guards quote injection.
            columns = _resolve_compute(session).describe_source(path)
            store.register_file(session, path)
            return {"name": path, "type": "file", "columns": columns}
        except Exception as e:
            return _err(engine, e)
```

- [ ] **Step 4: Merge registered files into `GET /tables`**

Replace the `/tables` endpoint body with:

```python
    @app.get("/tables")
    def tables(engine: str = "", session: str | None = None):
        try:
            out = list(_resolve_adapter(session, engine).list_tables())
            s = _sess(session)
            if s:
                for p in (s.get("files") or []):
                    out.append({"name": p, "type": "file"})
            return out
        except Exception as e:
            return _err(engine, e)
```

- [ ] **Step 5: Short-circuit `GET /columns` for registered files**

Replace the `/columns` endpoint body with:

```python
    @app.get("/columns")
    def columns(table: str, engine: str = "", session: str | None = None):
        try:
            s = _sess(session)
            if s and table in (s.get("files") or []):
                return _resolve_compute(session).describe_relation(table)
            return _resolve_adapter(session, engine).list_columns(table)
        except Exception as e:
            return _err(engine, e)
```

- [ ] **Step 6: Run tests — green + no regressions**

```bash
cd /home/kkaramete/xgraph/backend
./.venv/bin/python -m pytest tests/test_register_file.py -v
./.venv/bin/python -m pytest tests/ -q
```
Expected: `test_register_file.py` all PASS; full suite green (prior count + new, live tests may SKIP).

- [ ] **Step 7: Commit**

```bash
cd /home/kkaramete/xgraph
git add backend/xgraph_gateway/app.py backend/tests/test_register_file.py
git commit -m "feat(build): POST /register_file validates a path + lists it in /tables·/columns (session-scoped)"
```

---

## Task 3: `gateway.js` `registerFile(path)` client + Node test

**Files:**
- Modify: `frontend/gateway.js` (add method next to `create`/`tables`/`columns`, ≈L145-146).
- Modify: `frontend/tests/test_client.mjs`.

**Interfaces:**
- Consumes: `postJSONWithAuth(path, payload)` (merges session/engine into the JSON body).
- Produces: `client.registerFile(path)` → `POST /register_file {path, session, engine}`.

**Context:** JSON POST (not multipart) — we register a server-resolvable path, not an upload. `postJSONWithAuth` already injects `session`/`engine`, matching how `/register_file` reads them.

- [ ] **Step 1: Add the client method**

In `frontend/gateway.js`, after `columns:` (≈L146):

```javascript
      registerFile: function (path) { return postJSONWithAuth("/register_file", { path: path }); },
```

- [ ] **Step 2: Add the Node test**

In `frontend/tests/test_client.mjs`, before `console.log("client OK");`, add:

```javascript
  // registerFile(): JSON POST carrying the path + session
  let regUrl, regBody;
  const regClient = g.makeClient("http://gw", async (url, opts) => {
    if (url === "http://gw/connect") return { ok: true, json: async () => ({ session: "s1", graphs: [] }) };
    regUrl = url; regBody = JSON.parse(opts.body);
    return { ok: true, json: async () => ({ name: "v.parquet", type: "file", columns: ["id"] }) };
  });
  await regClient.connect({ engine: "falkordb", conn: {} }, { engine: "duckdb", conn: {} });
  const reg = await regClient.registerFile("v.parquet");
  assert.equal(regUrl, "http://gw/register_file");
  assert.equal(regBody.path, "v.parquet");
  assert.equal(regBody.session, "s1");
  assert.deepEqual(reg.columns, ["id"]);
  console.log("ok: registerFile client method");
```

- [ ] **Step 3: Run the client tests**

```bash
cd /home/kkaramete/xgraph/frontend
node tests/test_client.mjs
```
Expected: existing assertions plus `ok: registerFile client method`, then `client OK`.

- [ ] **Step 4: Commit**

```bash
cd /home/kkaramete/xgraph
git add frontend/gateway.js frontend/tests/test_client.mjs
git commit -m "feat(build): gateway.js registerFile(path) client method + Node test"
```

---

## Task 4: Frontend — "＋ File" affordance in the builder + refresh + version bump

**Files:**
- Modify: `frontend/XGraph.html` — add a register-file button beside `CreateHelperPanel`'s Default-table input (≈L6782-6794); add an App-level `registerFileGW(path)` that calls `gwClient.registerFile` and refreshes `tables`; thread it into `createProps`; bump `EXPLORER_VERSION` (L50).

**Interfaces:**
- Consumes: `gwClient.registerFile(path)` (Task 3); App `setTables`/`tables`, `gwClient`.
- Produces: a registered file appears in every section dropdown (the datalist filter only excludes `type === 'collection'`, so `type:'file'` shows), and its columns autocomplete via the existing `/columns` path.

**Context:** `CreateHelperPanel` reads `props.tables` for the Default-table datalist (`availableTables.filter(t => t.type !== 'collection')`). The App populates `tables` from `/tables` on connect. After a successful register we re-fetch `/tables` so the new file shows. The register affordance is a small button next to the Default-table input that prompts for a path (`window.prompt`) — minimal UI, no upload widget (paths are server-resolvable: bare names under `XGRAPH_DATA_DIR`, absolute paths, or `s3://`/`http(s)://`).

- [ ] **Step 1: Add `registerFileGW` in the App and thread it through `createProps`**

Near `fetchTableColumnsGW` (App scope), add:

```javascript
    // Register a data-file path as a builder source (DuckDB/FalkorDB). Validates
    // server-side (describe), then refreshes the table list so it shows in the
    // section dropdowns. Returns the columns (or throws with the gateway message).
    function registerFileGW(path) {
        if (!path || !gwClient) return Promise.resolve(null);
        return gwClient.registerFile(path).then(function(res) {
            return gwClient.tables().then(function(list) {
                if (Array.isArray(list)) setTables(list);
                return res;
            });
        });
    }
```

In the `createProps` object, add `onRegisterFile: registerFileGW,` alongside `onFetchTableColumns`.

- [ ] **Step 2: Destructure + pass the prop through `CreatePanel` to `CreateHelperPanel`**

In `CreatePanel`, where it reads `tables`/`onFetchTableColumns` from props (≈L6913-6914), also read `var onRegisterFile = props.onRegisterFile;`. In both `<CreateHelperPanel …>` mounts (Kinetica ≈L7117 and non-Kinetica ≈L7130), add `onRegisterFile={onRegisterFile}`.

- [ ] **Step 3: Add the "＋ File" button next to the Default-table input**

In `CreateHelperPanel`, in the Default-table row (the grid at ≈L6782, currently `grid-template-columns: auto 1fr auto`), change the template to `auto 1fr auto auto` and add a button after the clear-`✕` span:

```javascript
                                <span title="Register a data file (Parquet/CSV, local path or s3://…) as a pickable source"
                                      onClick={function(){
                                          var p = window.prompt('File path to register (Parquet/CSV; bare name under the data dir, absolute path, or s3://…):', '');
                                          if (!p || !props.onRegisterFile) return;
                                          props.onRegisterFile(p.trim())
                                              .then(function(res){ if (res && res.name) { setChSectionTable(function(prev){ var n = Object.assign({}, prev); n[comp] = res.name; return n; }); } })
                                              .catch(function(e){ window.alert('Could not register file: ' + (e && e.message ? e.message : e)); });
                                      }}
                                      style={{ cursor:'pointer', color:'#0984e3', fontSize:11, fontWeight:700, userSelect:'none', whiteSpace:'nowrap' }}>＋ File</span>
```

(On success it sets this section's Default table to the registered path, so the user can immediately pick columns.)

- [ ] **Step 4: Bump the version badge**

Change `EXPLORER_VERSION` (L50) `"0.7.0"` → `"0.8.0"`.

- [ ] **Step 5: esbuild JSX check + gateway 200**

```bash
cd /home/kkaramete/xgraph/frontend
end=$(grep -n '</script>' XGraph.html | tail -1 | cut -d: -f1)
sed -n "47,$((end-1))p" XGraph.html | ./node_modules/.bin/esbuild --loader=jsx > /dev/null && echo ESBUILD_OK || echo ESBUILD_FAIL
cd /home/kkaramete/xgraph && (./xgraph status >/dev/null 2>&1 || ./xgraph start) && sleep 1 && curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8090/
```
Expected: `ESBUILD_OK` then `200`.

- [ ] **Step 6: Commit**

```bash
cd /home/kkaramete/xgraph
git add frontend/XGraph.html
git commit -m "feat(build): '＋ File' registers a path as a builder source (DuckDB/FalkorDB); v0.8.0"
```

---

## Manual (browser) acceptance — run after Task 4

Hard-reload `http://localhost:8090/`, confirm `v0.8.0`, connect **FalkorDB**, **Build → Tables / files**:

1. The section builder shows an empty Default-table dropdown (FalkorDB has no catalog) plus a **＋ File** link.
2. Click **＋ File**, enter `vertexes.parquet` (a name under the data dir) → it validates, the Default table fills with that path, and the dropdown now lists it.
3. Row inputs autocomplete columns for that file (from `/columns`).
4. Fill NODES id + EDGES source/target, **Generate/Build** → the graph builds via `/create` and appears in **List**.
5. A bad path (`/nope.parquet`) shows an alert with the gateway error and does not get added.
6. **Kinetica regression:** Kinetica still lists its real tables and the ＋ File link is present but not required (its tables come from `/tables`).

---

## Deferred / follow-up

- **Kinetica file-import** (`CREATE EXTERNAL TABLE … FILE PATHS … WITH OPTIONS (DATA SOURCE=…)`, KiFS upload for local files, remote via named `DATA SOURCE` + `CREDENTIAL`) — greenfield (no such code exists), needs DATA SOURCE/CREDENTIAL management, and Kinetica already lists its existing tables, so this is *import*, not *discovery*. Its own plan.
- **Friendly aliases** — register under a short name mapped to the path (needs the builder to resolve alias→path at build time). v1 uses the path as the identifier.
- **Upload widget** — multipart upload of a local browser file (vs a server-resolvable path). Would reuse `postFormWithAuth` like `/extract`.
- **Persistent DuckDB catalog** — only needed if xGraph grows non-file DuckDB relations; not required for file-backed FalkorDB builds.

---

## Self-Review

- **Spec coverage:** `POST /register_file` (Task 2) turning a path into a pickable relation for DuckDB/FalkorDB (design's file-route), surfaced in `/tables` (Task 2) + `/columns` (Task 2, already worked for paths) + the builder UI (Task 4). Kinetica external-table registration explicitly deferred with rationale (not hidden).
- **Placeholder scan:** none — every backend step has exact code + pytest command + expected result; frontend steps cite exact anchors and give complete code.
- **Type/name consistency:** `SessionStore.register_file(session_id, path) -> list[str]` (Task 1) is called by the endpoint (Task 2); the endpoint returns `{name, type, columns}` consumed by `gwClient.registerFile` (Task 3) and `registerFileGW` (Task 4); `/tables` file entries use `{"name","type":"file"}` matching the builder's `availableTables.filter(t => t.type !== 'collection')`.
- **Scope:** DuckDB/FalkorDB file registration only. Kinetica file-import, aliases, uploads, and a persistent catalog are deferred, each independently shippable.
- **Risk sequencing:** backend is pure TDD (session unit + Fake/embedded-DuckDB gateway tests, all headless); the frontend lands last and can't regress the backend; Kinetica's existing `/tables` path is untouched.
