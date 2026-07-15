# xGraph Extract — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]`.

**Goal:** New **Extract** action: PDF/text → LLM entity+relationship extraction (open-ended) → MERGE into a named graph on the session's engine (FalkorDB or Kinetica).

**Architecture:** One backend extractor module (`extract.py`, uses local `_llm`), a new adapter method `ingest_elements` (FalkorDB Cypher MERGE + Kinetica table-upsert/CREATE GRAPH), a `POST /extract` endpoint, and a frontend Extract action. Full detail in `docs/superpowers/specs/2026-07-14-xgraph-extract-design.md` — implementers should read it.

**Tech Stack:** FastAPI (multipart), pypdf, DuckDB/falkordb/gpudb (already present), local `_llm` (claude CLI), React/Babel single-file frontend.

## Global Constraints
- Work in `~/xgraph`. Commit per task (this repo is committed; author already configured). Do NOT touch the superseded `graph/xgraph`.
- Backend runs on `~/xgraph/backend/.venv`; run tests from `backend/` with `./.venv/bin/python -m pytest`. Full suite is **155** now — must stay green; new live tests SKIP if FalkorDB/Kinetica unreachable.
- Self-contained: import nothing from the graphrag/kgr or falkor repos. Reuse the vendored `graph_loader` (e.g. `mapper.safe_ident`) and `xgraph_gateway.llm._llm`.
- Cypher/DDL safety: any label/identifier interpolated into a query MUST pass `graph_loader.mapper.safe_ident`; all data values are query parameters or JSON payloads, never string-interpolated.
- Frontend edits validate via `@babel/standalone` transpile of the single `<script type="text/babel">` block (NODE_PATH=/usr/share/nodejs, `@babel/core` + `@babel/preset-react`). Keep xGraph branding.
- LLM never called in tests — inject a fake `llm` / monkeypatch `extract.extract_document`.

---

### Task 1: Extractor module `xgraph_gateway/extract.py`
**Files:** create `backend/xgraph_gateway/extract.py`; add `pypdf` to `backend/requirements.txt` (+ `./.venv/bin/pip install pypdf`); test `backend/tests/test_extract.py`.
**Interfaces (produce):**
- `canonical_id(name: str) -> str` — `slug(name.lower())[:48] + '-' + sha1(name.lower()).hexdigest()[:8]`; slug = non-alnum runs → `-`, stripped.
- `chunk(text: str, max_chunks: int = 40) -> tuple[list[str], bool]` — split on `re.compile(r"\n\s*\n+")`, strip, drop empties; return `(chunks[:max_chunks], truncated_bool)`.
- `read_document(filename: str, data: bytes) -> str` — `.pdf` → pypdf `PdfReader(io.BytesIO(data))`, join page `extract_text()`; `.txt/.md/.markdown` → `data.decode('utf-8','replace')`; else `raise ValueError("unsupported file type: <ext>")`.
- `extract_document(text, hint=None, llm=None, max_chunks=40) -> {"entities":[{id,label,name,attrs}], "relations":[{id,src,dst,label,attrs}], "truncated": bool}` — per chunk call `call(prompt, schema=_EXTRACT_SCHEMA)` (`call = llm or _get_llm()`, mirroring nlcypher's lazy `_get_llm` importing `.llm._llm`); merge per spec (entities dedup by `canonical_id(name)`, first non-empty label/name wins, attrs shallow-merged; relations: map source/target names→ids via the chunk's name→id map, drop dangling, id=`sha1(f"{src}|{dst}|{label}")[:16]`, dedup by that id).
- `_EXTRACT_SCHEMA` — strict JSON schema: `{entities:[{name:str, label:str, attrs?:object}], relations:[{source:str, target:str, label:str, attrs?:object}]}`, `additionalProperties:false`, `required` on name/label and source/target/label.
- Prompt: instruct Title-Case entity labels, UPPER_SNAKE relation labels, consistent name spelling per real-world entity, `relations.source/target` must equal an entity `name` from the same chunk, fold in optional `hint`. Return-JSON-only instruction.

- [ ] Step 1 — Write failing tests (fake `llm` recording prompts / returning canned per-chunk dicts):
  - `canonical_id("Jerome Powell") == canonical_id("jerome powell")`; format `slug-<8hex>`.
  - `chunk("a\n\nb\n\n\nc")` → `(["a","b","c"], False)`; `chunk` with 50 paras, max_chunks=40 → 40 chunks + `True`.
  - `extract_document`: two chunks each returning the same entity "Apple" under label "Organization" plus distinct relations → merged entities has ONE Apple (id stable); a relation whose target names a missing entity is dropped; duplicate `(src,dst,label)` collapses.
  - `read_document("d.txt", b"hello")` == "hello"; `read_document("d.pdf", <bytes of a 1-page pdf written via pypdf>)` contains the page text (skip if pypdf import fails); `read_document("d.docx", b"x")` raises ValueError.
- [ ] Step 2 — Run `cd backend && ./.venv/bin/python -m pytest tests/test_extract.py -v` → FAIL.
- [ ] Step 3 — Implement `extract.py`; `pip install pypdf`; add to requirements.txt.
- [ ] Step 4 — Tests PASS; FULL suite green.
- [ ] Step 5 — Commit.

### Task 2: `ingest_elements` ABC + FalkorDB impl
**Files:** modify `adapters/base.py` (declare method), `adapters/falkordb_adapter.py`; test `tests/test_ingest_falkordb.py`.
**Interfaces:**
- `GraphEngineAdapter.ingest_elements(self, graph, nodes, edges) -> dict` on the ABC (`nodes`: `[{id,label,name,attrs}]`, `edges`: `[{id,src,dst,label,attrs}]`; returns `{"nodes":int,"edges":int,"labels":{"node_labels":[...],"edge_labels":[...]}}`).
- Extract a PURE helper `build_ingest_cypher(nodes, edges) -> list[(cypher, params)]` (module-level in falkordb_adapter) so it's unit-testable without a DB: group nodes by label (each label via `safe_ident`), emit `UNWIND $rows AS r MERGE (n:Entity {NODE:r.id}) SET n:`+L+`, n.LABEL=$label, n.name=r.name, n += r.attrs` with params `{rows, label}`; group edges by label (`safe_ident`), emit `UNWIND $rows AS e MATCH (a:Entity {NODE:e.src}),(b:Entity {NODE:e.dst}) MERGE (a)-[x:`+T+` {ID:e.id}]->(b) SET x.LABEL=$label, x += e.attrs`. Skip rows with null id/src/dst.
- `FalkorDBAdapter.ingest_elements` runs each (cypher, params) against the graph (create if new), summing created-node/relationship stats; collects distinct labels for the return.

- [ ] Step 1 — Failing unit test for `build_ingest_cypher`: given 2 node labels + 1 edge label, returns the right number of statements, each interpolated label passed through safe_ident, data only in params (assert no entity name appears in the cypher string). A malicious label `"a` b"` → the builder rejects via safe_ident (raises).
- [ ] Step 2 — Failing live test (SKIP if FalkorDB down): `ingest_elements('extract_test_graph', [2 nodes], [1 edge])` returns nodes>=2/edges>=1; a follow-up `run_query` MATCH returns the nodes; re-running ingest doesn't double them (MERGE). Clean up the test graph.
- [ ] Step 3 — Run tests → FAIL (unit) / SKIP or FAIL (live).
- [ ] Step 4 — Implement ABC method + `build_ingest_cypher` + `FalkorDBAdapter.ingest_elements`.
- [ ] Step 5 — Tests PASS (unit; live PASS if FalkorDB up); FULL suite green. Commit.

### Task 3: Kinetica `ingest_elements` impl
**Files:** modify `adapters/kinetica_adapter.py`; test `tests/test_ingest_kinetica.py`.
**Interfaces:**
- `KineticaAdapter.ingest_elements(graph, nodes, edges) -> dict` (same contract as Task 2).
- PURE builders (module-level, unit-testable): `node_table_name(graph)`/`edge_table_name(graph)` (each dotted part via `safe_ident`, suffix `_nodes`/`_edges`); `create_table_sql(...)` for `(NODE VARCHAR(256) PRIMARY_KEY, LABEL VARCHAR(256), name VARCHAR(1024))` nodes and `(edge_key VARCHAR(64) PRIMARY_KEY, NODE1 VARCHAR(256), NODE2 VARCHAR(256), LABEL VARCHAR(256))` edges; `create_graph_sql(graph, node_table, edge_table)` → the `CREATE OR REPLACE DIRECTED GRAPH` per spec; `node_rows(nodes)`/`edge_rows(edges)` → JSON-payload dicts (edge_key=edge id, NODE1=src, NODE2=dst).
- Impl: create schema if needed, create tables if absent, `db.insert_records_json(json.dumps(rows), table, options={"update_on_existing_pk":"true"})` for nodes then edges, then run the CREATE GRAPH DDL via the adapter's db/execute. Return counts (len rows) + labels. Never raise on empty inputs.

- [ ] Step 1 — Failing unit tests for the pure builders: table names are safe (dotted graph → schema-qualified, parts safe_ident'd; bad name raises); `create_graph_sql` contains `CREATE OR REPLACE DIRECTED GRAPH` + both table names + `NODE1`/`NODE2`; `node_rows`/`edge_rows` shape (edge_key from id, NODE1/NODE2 from src/dst); no entity data string-interpolated into any SQL (rows go through insert_records_json payload).
- [ ] Step 2 — Run → FAIL.
- [ ] Step 3 — Implement builders + `ingest_elements` (reuse the adapter's existing GPUdb handle / `_db`).
- [ ] Step 4 — Unit tests PASS; add a live test that SKIPs if Kinetica unreachable (ingest small set into `xgraph_extract_test`, assert graph appears via `graph_sizes()`/`list_graphs()`, then drop). FULL suite green. Commit.

### Task 4: `POST /extract` endpoint + `gateway.js`
**Files:** modify `app.py`, `frontend/gateway.js`; test `tests/test_extract_endpoint.py`.
**Interfaces:**
- `POST /extract` (multipart): `file` (UploadFile|None), `text` (str form|None), `graph` (str), `hint` (str|None), `session` (str|None), `engine` (str). Flow: `text = read_document(file.filename, await file.read())` if file else the `text` field; require non-empty; `res = extract.extract_document(text, hint)`; `out = adapter.ingest_elements(graph, res["entities"], res["relations"])`; return `{graph, entities: out["nodes"], relations: out["edges"], labels: out["labels"], truncated: res["truncated"]}`. Wrap in `_err`. Use `_resolve_adapter(session, engine)`. Use `from fastapi import UploadFile, Form, File` and accept `file: UploadFile = File(None)`, `text: str = Form(None)`, etc.
- `gateway.js`: `extract(graph, fileOrText, hint)` — if `fileOrText` is a File/Blob, send `FormData` with `file`; else send `text`. Always append `graph`, `hint`, `session`. POST `/extract` (multipart; do not set JSON content-type — let the browser set the boundary). Return parsed JSON.

- [ ] Step 1 — Failing TestClient tests: monkeypatch `xgraph_gateway.extract.extract_document` → canned `{entities:[…], relations:[…], truncated:False}`; a `FakeAdapter.ingest_elements` → canned counts/labels. POST multipart with `text` field + `graph` → 200 with counts/labels/truncated. POST with a `file` (small .txt bytes) → 200. Unsupported extension via `read_document` → error envelope. (Use `client.post('/extract', data={...}, files={...})`.)
- [ ] Step 2 — Run → FAIL.
- [ ] Step 3 — Implement endpoint + `gateway.js extract()`. Add `ingest_elements` to `FakeAdapter` (canned) so the test + real fake route work.
- [ ] Step 4 — Tests PASS; FULL suite green. Commit.

### Task 5: Extract action (frontend)
**Files:** modify `frontend/XGraph.html`.
**Interfaces (consume):** `gwClient.extract(graph, fileOrText, hint)`, `gwClient.listGraphs()`, `setActiveGraph`, `setActiveAction`.
- [ ] Step 1 — Add `'extract'` to the `ACTIONS` array right after `'create'` (label **Extract**); reachable when connected (session set), same gating as Create.
- [ ] Step 2 — `ExtractPanel` under `activeAction==='extract'`: a file `<input type="file" accept=".pdf,.txt,.md,.markdown">` AND a paste `<textarea>` (use whichever is provided; file wins), a target-graph text input (default `extracted_graph`), an optional focus/hint input, and an **Extract & Build** button → `await gwClient.extract(graphName, file || pasteText, hint)` with a busy spinner (note it can take a while). On success: result card with entities/relations counts, discovered node/edge label chips (from `resp.labels`), a "truncated — only first N chunks" note if `resp.truncated`, and buttons **Visualize** / **Ontology** that `setActiveGraph(resp.graph)`, refresh `gwClient.listGraphs()`, and `setActiveAction('visualize'|'ontology')`. Inline error on failure. Match the file's inline-style vocabulary.
- [ ] Step 3 — Validate: `@babel/standalone` transpile PASS; grep confirms `gwClient.extract(` present and `'extract'` in ACTIONS.
- [ ] Step 4 — Commit. (Browser acceptance by the user.)

## Self-Review
- PDF/text → LLM extraction (open-ended) → Task 1. ✓
- Both engines, accumulate/MERGE → Task 2 (FalkorDB) + Task 3 (Kinetica). ✓
- Endpoint + client → Task 4. Frontend action → Task 5. ✓
- Self-contained, safe_ident on identifiers, values parameterized, live tests SKIP → Global + per-task. ✓
- No placeholders: signatures, schema, Cypher/DDL templates, dedupe rules, test cases all explicit.
