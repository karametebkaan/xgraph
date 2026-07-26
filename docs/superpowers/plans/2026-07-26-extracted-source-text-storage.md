# Extracted Source-Text Storage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** For extraction-built graphs, persist the full source text (file-read AND pasted/appended) at extract time and show it, within a display cap, in the Storage → Provenance panel.

**Architecture:** A new DuckDB meta-store table `xgraph_document_texts` keyed on `(graph, doc_uri)` holds the full text out-of-line from the light `xgraph_documents` ledger. `/extract` writes the text after a successful ingest (and backfills on the reuse short-circuit when missing). A new `GET /document_text` serves a length-capped slice on demand, so the StoragePanel document list stays light and only fetches text when a row is expanded.

**Tech Stack:** Python 3 / FastAPI + DuckDB (backend, own venv at `backend/.venv`); single-file React 18 UMD + Babel via CDN (`frontend/XGraph.html`); UMD `gateway.js` client; pytest (backend) + node:assert `.mjs` (frontend client).

## Global Constraints

- **Do NOT `git commit` anything under `xgraph/`.** Write files freely; never `git add`/`git commit`. The "Checkpoint" steps below are validation gates, NOT commits (CLAUDE.md rule).
- **Backend tests run from `backend/`** via `./.venv/bin/python -m pytest`. Never invoke bare `pytest` or `python` from the repo root (module resolution fails without `cd backend`).
- **Frontend has no build step.** Validate `XGraph.html` edits with an esbuild JSX transpile check (expect `ESBUILD_OK`) plus a `curl` 200; real behavior is browser-verified by the user, not headlessly.
- **Meta table creation is idempotent** — `CREATE TABLE IF NOT EXISTS` inside `_meta_con()`; no separate migration step. Old meta DBs gain the table on next connect.
- **Text capture is best-effort provenance** — it must never fail the `/extract` result the user cares about. Extraction/ingest and the `xgraph_documents` ledger row are the contract; the text write is wrapped so a failure is swallowed.
- **Display cap = 20000 chars** (the `/document_text` `limit` default and the UI note copy). Use this exact value everywhere.
- **`gateway.js` is ES5-style UMD** (`function () {}`, `var`) and also `require`-able in Node — match the surrounding style, no arrow functions / template literals in the client method.

---

## File Structure

- `backend/xgraph_gateway/compute/duckdb_engine.py` — **Modify.** Add the `xgraph_document_texts` table to `_meta_con()`; add `record_document_text`, `has_document_text`, `get_document_text`; extend `clear_graph_metadata` to delete text rows. Owns all meta-store persistence for the feature.
- `backend/xgraph_gateway/app.py` — **Modify.** `/extract` writes text on the full path and backfills on the reuse short-circuit; new `GET /document_text` endpoint. Owns the HTTP surface.
- `frontend/gateway.js` — **Modify.** Add `documentText(graph, docUri, limit)` client method next to `documents`/`sourcePreview`.
- `frontend/XGraph.html` — **Modify.** Make StoragePanel "Provenance — documents" rows expandable with on-demand text fetch + scrollable `<pre>` + truncation note; bump `EXPLORER_VERSION`.
- `backend/tests/test_metadata_store.py` — **Modify.** Unit coverage for the new store methods.
- `backend/tests/test_extract_endpoint.py` — **Modify.** Endpoint coverage: `/extract` persists text; `/document_text` serves it; reuse path backfills.
- `frontend/tests/test_client.mjs` — **Modify.** `documentText` client assertion.

---

### Task 1: Meta-store text table + methods

**Files:**
- Modify: `backend/xgraph_gateway/compute/duckdb_engine.py` (`_meta_con` ~lines 22-49; `clear_graph_metadata` ~line 157)
- Test: `backend/tests/test_metadata_store.py`

**Interfaces:**
- Consumes: existing `DuckDBComputeEngine(meta_path=...)`, `self._meta_con()`, module-level `_iso` (already imported).
- Produces:
  - `record_document_text(graph: str, doc_uri: str, text: str) -> None` — upsert (DELETE then INSERT on `(graph, doc_uri)`); stores `text` and `char_len = len(text)`. Idempotent.
  - `has_document_text(graph: str, doc_uri: str) -> bool` — existence check.
  - `get_document_text(graph: str, doc_uri: str, limit: int | None = None) -> dict | None` — returns `{"doc_uri", "text", "char_len", "truncated"}` (text sliced to `limit`; `truncated = char_len > len(returned)`), or `None` when no row.
  - `clear_graph_metadata(graph)` now also deletes from `xgraph_document_texts`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_metadata_store.py`:

```python
def test_document_text_round_trip(tmp_path):
    eng = _engine(tmp_path)
    eng.record_document_text("g1", "doc:a", "hello world")
    got = eng.get_document_text("g1", "doc:a")
    assert got == {"doc_uri": "doc:a", "text": "hello world",
                   "char_len": 11, "truncated": False}


def test_document_text_limit_slices_and_flags_truncated(tmp_path):
    eng = _engine(tmp_path)
    eng.record_document_text("g1", "doc:a", "abcdefghij")  # 10 chars
    got = eng.get_document_text("g1", "doc:a", limit=4)
    assert got["text"] == "abcd"
    assert got["char_len"] == 10
    assert got["truncated"] is True
    # limit >= length -> not truncated, full text
    full = eng.get_document_text("g1", "doc:a", limit=10)
    assert full["text"] == "abcdefghij"
    assert full["truncated"] is False


def test_document_text_upsert_replaces(tmp_path):
    eng = _engine(tmp_path)
    eng.record_document_text("g1", "doc:a", "first")
    eng.record_document_text("g1", "doc:a", "second version")
    got = eng.get_document_text("g1", "doc:a")
    assert got["text"] == "second version"
    assert got["char_len"] == 14


def test_has_document_text(tmp_path):
    eng = _engine(tmp_path)
    assert eng.has_document_text("g1", "doc:a") is False
    eng.record_document_text("g1", "doc:a", "x")
    assert eng.has_document_text("g1", "doc:a") is True


def test_get_document_text_missing_returns_none(tmp_path):
    eng = _engine(tmp_path)
    assert eng.get_document_text("g1", "nope") is None


def test_clear_graph_metadata_removes_text(tmp_path):
    eng = _engine(tmp_path)
    eng.record_document_text("g1", "doc:a", "hello")
    eng.clear_graph_metadata("g1")
    assert eng.get_document_text("g1", "doc:a") is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_metadata_store.py -v -k document_text`
Expected: FAIL with `AttributeError: 'DuckDBComputeEngine' object has no attribute 'record_document_text'`

- [ ] **Step 3: Add the table to `_meta_con()`**

In `_meta_con()`, after the `xgraph_creations` `CREATE TABLE IF NOT EXISTS` block (and before the `spec_json` ALTER migration `try` is fine too — order among `CREATE`s doesn't matter), add:

```python
            con.execute(
                "CREATE TABLE IF NOT EXISTS xgraph_document_texts ("
                " graph VARCHAR, doc_uri VARCHAR, text VARCHAR,"
                " char_len INTEGER, PRIMARY KEY (graph, doc_uri))")
```

- [ ] **Step 4: Add the three methods**

Add these methods to `DuckDBComputeEngine` (e.g. right after `get_document`):

```python
    def record_document_text(self, graph, doc_uri, text):
        """Upsert the FULL source text for a document, keyed on
        (graph, doc_uri). Idempotent (DELETE then INSERT). Best-effort
        provenance -- callers wrap this so a failure never breaks /extract."""
        text = text or ""
        con = self._meta_con()
        try:
            con.execute(
                "DELETE FROM xgraph_document_texts WHERE graph = ? AND doc_uri = ?",
                [graph, doc_uri])
            con.execute(
                "INSERT INTO xgraph_document_texts VALUES (?, ?, ?, ?)",
                [graph, doc_uri, text, len(text)])
        finally:
            con.close()

    def has_document_text(self, graph, doc_uri):
        """Cheap existence check for the /extract reuse-path backfill."""
        con = self._meta_con()
        try:
            row = con.execute(
                "SELECT 1 FROM xgraph_document_texts"
                " WHERE graph = ? AND doc_uri = ?", [graph, doc_uri]).fetchone()
            return row is not None
        finally:
            con.close()

    def get_document_text(self, graph, doc_uri, limit=None):
        """Return {doc_uri, text, char_len, truncated} with text sliced to
        `limit` (full text when limit is None or >= length); None if no row."""
        con = self._meta_con()
        try:
            row = con.execute(
                "SELECT text, char_len FROM xgraph_document_texts"
                " WHERE graph = ? AND doc_uri = ?", [graph, doc_uri]).fetchone()
            if row is None:
                return None
            text, char_len = row[0] or "", row[1] or 0
            sliced = text if limit is None else text[:limit]
            return {"doc_uri": doc_uri, "text": sliced, "char_len": char_len,
                    "truncated": char_len > len(sliced)}
        finally:
            con.close()
```

- [ ] **Step 5: Extend `clear_graph_metadata`**

In `clear_graph_metadata`, add a fourth DELETE inside the `try`:

```python
            con.execute("DELETE FROM xgraph_document_texts WHERE graph = ?", [graph])
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_metadata_store.py -v`
Expected: PASS (new tests + all pre-existing metadata-store tests still green)

- [ ] **Step 7: Checkpoint (validate, do NOT commit — repo rule)**

Run the full backend suite to confirm no regression: `cd backend && ./.venv/bin/python -m pytest tests/ -q`
Expected: all pass / live tests skip. Do NOT `git add`/`git commit`.

---

### Task 2: `/extract` records text + `GET /document_text` endpoint

**Files:**
- Modify: `backend/xgraph_gateway/app.py` (`/extract` ~lines 427-483; add `/document_text` after `/documents` ~line 490)
- Test: `backend/tests/test_extract_endpoint.py`

**Interfaces:**
- Consumes: Task 1's `store.record_document_text`, `store.has_document_text`, `store.get_document_text`; existing `_resolve_compute(session)`, `_err(engine, e)`, the `doc`/`doc_uri` locals already computed in `/extract`.
- Produces: `GET /document_text?graph=&doc_uri=&session=&limit=` → `{doc_uri, text, char_len, truncated}` (missing row → `{doc_uri, text:"", char_len:0, truncated:false}`).

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_extract_endpoint.py` (reuses `_client`, `_patch_extract_document`, and the module-level `extract` import already present):

```python
def test_extract_persists_document_text(monkeypatch, tmp_path):
    _patch_extract_document(monkeypatch)
    client = _client(tmp_path)
    r = client.post("/extract", data={"graph": "g1", "text": "hello source text", "engine": "fake"})
    assert r.status_code == 200
    doc_uri = r.json()["document"]["doc_uri"]
    got = client.get("/document_text", params={"graph": "g1", "doc_uri": doc_uri})
    assert got.status_code == 200
    body = got.json()
    assert body["text"] == "hello source text"
    assert body["char_len"] == len("hello source text")
    assert body["truncated"] is False


def test_document_text_limit_truncates(monkeypatch, tmp_path):
    _patch_extract_document(monkeypatch)
    client = _client(tmp_path)
    client.post("/extract", data={"graph": "g1", "text": "abcdefghij", "engine": "fake"})
    doc_uri = "text:" + __import__("hashlib").sha256("abcdefghij".encode()).hexdigest()[:12]
    got = client.get("/document_text", params={"graph": "g1", "doc_uri": doc_uri, "limit": 4})
    body = got.json()
    assert body["text"] == "abcd"
    assert body["char_len"] == 10
    assert body["truncated"] is True


def test_document_text_missing_returns_empty(tmp_path):
    got = _client(tmp_path).get("/document_text", params={"graph": "g1", "doc_uri": "text:nope"})
    assert got.status_code == 200
    assert got.json() == {"doc_uri": "text:nope", "text": "", "char_len": 0, "truncated": False}


def test_extract_reuse_backfills_missing_text(monkeypatch, tmp_path):
    # First extraction stores text normally. Simulate a pre-feature graph by
    # deleting the text row, then re-submit identical bytes: the reuse
    # short-circuit (entities == 0) must backfill the text.
    _patch_extract_document(monkeypatch)
    compute = DuckDBComputeEngine(meta_path=str(tmp_path / "meta.duckdb"))
    client = TestClient(create_app(adapter_factory=lambda e: FakeAdapter(), compute=compute))
    r1 = client.post("/extract", data={"graph": "g1", "text": "reuse me", "engine": "fake"})
    doc_uri = r1.json()["document"]["doc_uri"]
    compute.clear_graph_metadata("g1")  # wipes text row (and ledger)
    # re-record ONLY the ledger row so the reuse short-circuit path is taken
    import hashlib
    sha = hashlib.sha256("reuse me".encode()).hexdigest()
    compute.record_document("g1", doc_uri, sha, "text")
    assert compute.has_document_text("g1", doc_uri) is False
    r2 = client.post("/extract", data={"graph": "g1", "text": "reuse me", "engine": "fake"})
    assert r2.json()["document"]["reused"] is True
    assert compute.has_document_text("g1", doc_uri) is True
    assert compute.get_document_text("g1", doc_uri)["text"] == "reuse me"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_extract_endpoint.py -v -k "document_text or persists or backfills"`
Expected: FAIL — `/document_text` returns 404/405 (route missing) and text assertions error.

- [ ] **Step 3: Record text on the full extraction path**

In `/extract`, in the full path, right after the ledger commit line `record = store.record_document(graph, doc_uri, sha, source_type)` (the one AFTER `ingest_elements`), add a best-effort text write:

```python
            record = store.record_document(graph, doc_uri, sha, source_type)
            # Best-effort full-text provenance -- must never fail the extract
            # result (ingest + ledger already succeeded). Separate table so the
            # /documents list stays light; served on demand by /document_text.
            try:
                store.record_document_text(graph, doc_uri, doc)
            except Exception:
                pass
            doc_info = {"doc_uri": doc_uri, "sha256": sha, **record}
```

- [ ] **Step 4: Backfill text on the reuse short-circuit**

In the reuse short-circuit block (`if existing is not None and existing.get("sha256") == sha:`), after `record = store.record_document(...)` and before building `doc_info`, add:

```python
                record = store.record_document(graph, doc_uri, sha, source_type)
                # Backfill text for a graph extracted before this feature (or
                # whose text row was lost) when the same bytes are re-submitted.
                # Only when missing, so normal reuse does no extra work.
                try:
                    if not store.has_document_text(graph, doc_uri):
                        store.record_document_text(graph, doc_uri, doc)
                except Exception:
                    pass
                doc_info = {"doc_uri": doc_uri, "sha256": sha, **record}
```

- [ ] **Step 5: Add the `GET /document_text` endpoint**

Immediately after the `/documents` endpoint, add:

```python
    @app.get("/document_text")
    def document_text(graph: str, doc_uri: str, engine: str = "",
                      session: str | None = None, limit: int = 20000):
        try:
            got = _resolve_compute(session).get_document_text(graph, doc_uri, limit)
            if got is None:
                # No stored text (pre-feature graph or failed capture). Return
                # an empty shape rather than an error so the panel can render a
                # "not captured" note without special-casing an error envelope.
                return {"doc_uri": doc_uri, "text": "", "char_len": 0, "truncated": False}
            return got
        except Exception as e:
            return _err(engine, e)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_extract_endpoint.py -v`
Expected: PASS (new tests + all pre-existing extract-endpoint tests still green)

- [ ] **Step 7: Checkpoint (validate, do NOT commit)**

Run: `cd backend && ./.venv/bin/python -m pytest tests/ -q`
Expected: all pass / live tests skip. Do NOT commit.

---

### Task 3: `gateway.js` `documentText` client method + test

**Files:**
- Modify: `frontend/gateway.js` (next to `documents`/`sourcePreview`, ~lines 142-144)
- Test: `frontend/tests/test_client.mjs`

**Interfaces:**
- Consumes: existing `getJSON`, `q(...)` helpers in `gateway.js`; Task 2's `GET /document_text`.
- Produces: `client.documentText(graph, docUri, limit)` → resolves the `{doc_uri, text, char_len, truncated}` JSON. `limit` is optional; omitted → no `&limit=` param (server default 20000 applies).

- [ ] **Step 1: Write the failing test**

In `frontend/tests/test_client.mjs`, inside the `run` async fn (after an existing assertion, before the final success log), add:

```javascript
  // documentText -> GET /document_text with encoded graph/doc_uri (+ optional limit)
  const dtUrls = [];
  const dtClient = g.makeClient("http://gw", "falkordb", fakeFetch((url) => {
    dtUrls.push(url);
    if (url.startsWith("http://gw/document_text")) {
      return { doc_uri: "text:abc", text: "hello", char_len: 5, truncated: false };
    }
    return {};
  }));
  const dt = await dtClient.documentText("g one", "text:abc", 4);
  assert.deepEqual(dt, { doc_uri: "text:abc", text: "hello", char_len: 5, truncated: false });
  const dtUrl = dtUrls[dtUrls.length - 1];
  assert.ok(dtUrl.includes("graph=g%20one"), "graph is URL-encoded");
  assert.ok(dtUrl.includes("doc_uri=text%3Aabc"), "doc_uri is URL-encoded");
  assert.ok(dtUrl.includes("limit=4"), "limit is included when provided");
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd frontend && node tests/test_client.mjs`
Expected: FAIL — `dtClient.documentText is not a function`.

- [ ] **Step 3: Add the client method**

In `frontend/gateway.js`, after the `sourcePreview` line, add:

```javascript
      documentText: function (graph, docUri, limit) {
        var qs = "/document_text?graph=" + encodeURIComponent(graph) +
                 "&doc_uri=" + encodeURIComponent(docUri) +
                 (limit != null ? "&limit=" + encodeURIComponent(limit) : "");
        return getJSON(q(qs));
      },
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd frontend && node tests/test_client.mjs`
Expected: PASS (prints the existing success line, no assertion errors).

- [ ] **Step 5: Checkpoint (validate, do NOT commit)**

Run both frontend unit suites: `cd frontend && node tests/test_transforms.mjs && node tests/test_client.mjs`
Expected: both pass. Do NOT commit.

---

### Task 4: StoragePanel expandable per-document text + version bump

**Files:**
- Modify: `frontend/XGraph.html` (StoragePanel "Provenance — documents" block ~lines 7797-7815; `EXPLORER_VERSION` line 50)

**Interfaces:**
- Consumes: Task 3's `gwClient.documentText(activeGraph, docUri)`; existing `storageDocs` state and `activeGraph`/`gwClient` props.
- Produces: no new exported interface — UI-only behavior (expand a document row to fetch + show its text).

- [ ] **Step 1: Add expand + text-cache state to StoragePanel**

In `StoragePanel`, next to the existing `storageDocs` state declaration, add two state hooks:

```javascript
    const [openDoc, setOpenDoc] = useState(null);      // doc_uri currently expanded (or null)
    const [docText, setDocText] = useState({});        // doc_uri -> {text,char_len,truncated} | {error} | 'loading'
```

- [ ] **Step 2: Add the toggle/fetch handler**

Inside `StoragePanel` (before the `return`), add a handler that toggles a row and lazily fetches its text once:

```javascript
    function toggleDoc(docUri) {
        if (openDoc === docUri) { setOpenDoc(null); return; }
        setOpenDoc(docUri);
        if (docText[docUri] === undefined) {
            setDocText(function (prev) { var n = Object.assign({}, prev); n[docUri] = 'loading'; return n; });
            gwClient.documentText(activeGraph, docUri).then(function (r) {
                setDocText(function (prev) { var n = Object.assign({}, prev); n[docUri] = r; return n; });
            }).catch(function (err) {
                setDocText(function (prev) { var n = Object.assign({}, prev); n[docUri] = { error: (err && err.message) || String(err) }; return n; });
            });
        }
    }
```

- [ ] **Step 3: Make each provenance row expandable**

Replace the existing `storageDocs.map(...)` block in the "Provenance — documents" section with a clickable header + conditional body. The current block is:

```javascript
                    {storageDocs.map(function(d, i){
                        return <div key={i} style={{ fontSize:13, color:'#2d3436', padding:'6px 0', borderBottom:'1px solid #f1f2f6' }}>
                            {(d.source_type === 'file' ? '📄 ' : '✎ ') + d.doc_uri}
                            <span style={{ fontSize:11, color:'#b2bec3', marginLeft:8 }}>· {d.status} · ingested {String(d.last_ingested_ts || '').slice(0,19).replace('T',' ')}</span>
                        </div>;
                    })}
```

Replace it with:

```javascript
                    {storageDocs.map(function(d, i){
                        var open = openDoc === d.doc_uri;
                        var t = docText[d.doc_uri];
                        return <div key={i} style={{ borderBottom:'1px solid #f1f2f6' }}>
                            <div onClick={function(){ toggleDoc(d.doc_uri); }}
                                 style={{ fontSize:13, color:'#2d3436', padding:'6px 0', cursor:'pointer', userSelect:'none' }}>
                                <span style={{ color:'#b2bec3', marginRight:6 }}>{open ? '▾' : '▸'}</span>
                                {(d.source_type === 'file' ? '📄 ' : '✎ ') + d.doc_uri}
                                <span style={{ fontSize:11, color:'#b2bec3', marginLeft:8 }}>· {d.status} · ingested {String(d.last_ingested_ts || '').slice(0,19).replace('T',' ')}</span>
                            </div>
                            {open && (
                                <div style={{ padding:'0 0 10px' }}>
                                    {t === 'loading' && <span style={{ fontSize:11, color:'#b2bec3' }}>Loading text…</span>}
                                    {t && t.error && <span style={{ fontSize:11, color:'#d63031' }}>{t.error}</span>}
                                    {t && !t.error && t !== 'loading' && (t.char_len > 0 ? (
                                        <div>
                                            <pre style={{ margin:0, padding:12, background:'#2d3436', color:'#dfe6e9', borderRadius:6, fontSize:11, whiteSpace:'pre-wrap', maxHeight:320, overflow:'auto' }}>{t.text}</pre>
                                            {t.truncated && <div style={{ fontSize:11, color:'#b2bec3', marginTop:4 }}>{'showing first ' + t.text.length.toLocaleString() + ' of ' + t.char_len.toLocaleString() + ' characters'}</div>}
                                        </div>
                                    ) : (
                                        <span style={{ fontSize:11, color:'#b2bec3' }}>Source text not captured for this document (extracted before text was stored — re-run Extract on the same document to capture it).</span>
                                    ))}
                                </div>
                            )}
                        </div>;
                    })}
```

- [ ] **Step 4: Bump the version**

Change line 50 `const EXPLORER_VERSION = "0.17.1";` to `const EXPLORER_VERSION = "0.18.0";`.

- [ ] **Step 5: Validate the JSX transpiles**

Run the esbuild JSX check on the `<script type="text/babel">` block (the repo's established validation). Expected output: `ESBUILD_OK`.

```bash
cd frontend && node -e '
const fs=require("fs");const {transformSync}=require("esbuild");
const html=fs.readFileSync("XGraph.html","utf8");
const m=html.match(/<script type="text\/babel">([\s\S]*?)<\/script>/);
if(!m){console.error("NO BABEL BLOCK");process.exit(1);}
try{transformSync(m[1],{loader:"jsx"});console.log("ESBUILD_OK");}
catch(e){console.error(e.message);process.exit(1);}
'
```

- [ ] **Step 6: Validate the gateway still serves the page**

With the gateway running (`./xgraph start` if needed), confirm a 200:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8090/
```
Expected: `200`. (Behavioral verification of the expand/collapse is browser-driven by the user.)

- [ ] **Step 7: Checkpoint (validate, do NOT commit)**

Confirm `ESBUILD_OK` + `200`. Do NOT commit. Feature complete pending the user's browser acceptance.

---

## Self-Review

**1. Spec coverage** — every spec section maps to a task:
- Meta-store table + `record_document_text`/`has_document_text`/`get_document_text` + `clear_graph_metadata` clear → **Task 1**.
- `/extract` full-path record + reuse backfill → **Task 2 Steps 3-4**.
- `GET /document_text` (limit default 20000, empty shape on miss) → **Task 2 Step 5**.
- `gateway.js documentText` → **Task 3**.
- StoragePanel expandable rows, scrollable `<pre>`, truncation note, "not captured" note, version bump → **Task 4**.
- Testing (metadata store unit, endpoint, client) → Tasks 1-3 test steps. Out-of-scope items (paginated DB viewer, file/Kinetica backing, non-extraction routes) intentionally excluded — matches spec §"Out of scope".

**2. Placeholder scan** — no TBD/TODO/"add error handling" placeholders; every code step has concrete content. Error handling is explicit (best-effort `try/except` in `/extract`, empty-shape on missing text, per-row `{error}` cache in the UI).

**3. Type consistency** — method names identical across tasks: `record_document_text`, `has_document_text`, `get_document_text` (Task 1) are the exact names called in Task 2 (`store.*`) and via `documentText` in Tasks 3-4. Return shape `{doc_uri, text, char_len, truncated}` is consistent across the store method, the endpoint, the client, and the UI consumer. `limit` default 20000 matches the Global Constraints value. `EXPLORER_VERSION` 0.17.1 → 0.18.0 is the only version touched.

**Deviation from the writing-plans template:** the standard "Commit" step is replaced by "Checkpoint (validate, do NOT commit)" throughout, per the CLAUDE.md no-commit rule for `xgraph/`.
