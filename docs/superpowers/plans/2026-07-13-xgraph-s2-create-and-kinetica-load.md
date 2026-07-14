# xgraph S2 — Create (CREATE OR REPLACE GRAPH) + Kinetica Load fix

> subagent-driven, no-commit (local). Reuses falkor's build pipeline + kgr-style Kinetica ontology.

**Goal:** (1) Make **Load** work for Kinetica graphs (ontology + entity browse). (2) Add a **Create** action — CREATE OR REPLACE GRAPH — with two routes: DuckDB-SQL-over-Parquet → FalkorDB (reusing `falkor`'s tested build pipeline), and Kinetica `CREATE OR REPLACE DIRECTED GRAPH` DDL.

**Architecture:** Both map onto the existing contracts. Kinetica Load fills the S4-deferred `KineticaAdapter.get_schema`/`fetch_entities`. Create is the `GraphEngineAdapter.load_graph` write side + a `/create` endpoint. For FalkorDB, `load_graph` = `falkor`'s `run_build(Mapping, DuckDBSource(tables), FalkorDBSink)` (wipe+rebuild = create-or-replace). For Kinetica, `load_graph` runs the CREATE-GRAPH DDL via `run_query`.

## Global Constraints
- **No git commit under `xgraph/`** — local only; "checkpoint" = verify locally. Gateway on :8090 (restart after backend edits — I'll manage the background instance).
- Backend on falkor's venv; reuse `graph_loader` (`run_build`, `config.Mapping/NodeSpec/EdgeSpec`, `duckdb_source.DuckDBSource`, `falkordb_sink.FalkorDBSink`, `kinetica_source`). Don't reimplement.
- Backend edits: run FULL suite (currently 43) — must stay green; new tests skip if services down.
- Frontend edits: validate via `@babel/standalone` transpile of the `<script type="text/babel">` block + serve/curl 200. No "Kinetica"/"Graph Explorer" chrome; branding stays xGraph.

---

### Task 1: Kinetica Load — real ontology + entity browse
**Files:** modify `xgraph_gateway/adapters/kinetica_adapter.py`; test `tests/test_kinetica_load.py`.
**Interfaces (produce):**
- `KineticaAdapter.get_schema(graph)` → `{labels, rel_types, dot}` where `dot` is Kinetica's **server-side ontology DOT**: call the graph show endpoint with the schema-export option (`self._db.show_graph(graph_name=graph, options={'export_graph_schema':'true'})`) and pull the DOT string out of the response (investigate the live response keys — the original explorer read it from `/show/graph` with `export_graph_schema:'true'`). Also populate `labels`/`rel_types` from the response if present.
- `KineticaAdapter.fetch_entities(graph, limit)` → `{nodes:[{id,label,props}], edges:[{id,source,target,type}]}`. Implement by discovering the graph's backing node/edge tables from `show_graph(graph)` (investigate the live response — it exposes the CREATE statement / input tables), then sampling: nodes `SELECT id, label FROM <vtable> LIMIT :limit`, edges `SELECT id, source_name, target_name, label FROM <etable> LIMIT :limit`, via the existing `KineticaSource`. If the backing tables can't be discovered, return `{"nodes":[],"edges":[]}` (Load still succeeds: ontology renders, browse is empty) — do NOT raise. Remove the old `NotImplementedError`.

- [ ] Step 1 — Probe live Kinetica: write a throwaway script that calls `show_graph('expero.banking_graph')` and `show_graph(..., options={'export_graph_schema':'true'})` and prints the response keys, so you know the exact fields holding the DOT and the backing tables. Record what you found in the report.
- [ ] Step 2 — Failing test: unit-test the DOT-extraction + table-discovery helpers with a canned `show_graph` response (fake the db). Assert `get_schema` returns a non-empty `dot` and `fetch_entities` shapes rows correctly / returns empty (never raises).
- [ ] Step 3 — Implement `get_schema` + `fetch_entities` per the probe findings; extract the parsing into small pure helpers so they're unit-testable without live Kinetica.
- [ ] Step 4 — Add a live integration test (skip if Kinetica down): `get_schema('expero.banking_graph')` has a `dot` starting with `digraph`/`strict`/graph tokens; `fetch_entities(...,limit=25)` returns ≤25 nodes with `id`/`label`. Run FULL suite green.
- [ ] Step 5 — Checkpoint.

### Task 2: Create — load_graph write side + `/create` endpoint
**Files:** modify `adapters/base.py` (declare `load_graph`), `adapters/falkordb_adapter.py`, `adapters/kinetica_adapter.py`, `app.py`, `frontend/gateway.js`; tests `tests/test_create.py`.
**Interfaces (produce):**
- `GraphEngineAdapter.load_graph(spec) -> dict` (add to ABC).
- `FalkorDBAdapter.load_graph(spec)`: build a `graph_loader.config.Mapping` from `spec` and run `graph_loader.cli.run_build(mapping, DuckDBSource.connect(spec['tables']), FalkorDBSink.connect(spec['graph'], host=<conn>, port=<conn>, password=<conn>))`; return the `run_build` counts `{"nodes":{...},"edges":{...}}`. `spec` shape:
  ```
  { "graph": "<name>",
    "tables": { "<tablename>": "<parquet/csv path>", ... },
    "nodes": [ { "sql","id","id_property"?, "label_column","label_property"?, "properties":[...] } ],
    "edges": [ { "sql","id","id_property"?, "type_column","type_property"?, "source_key","target_key","properties":[...] } ],
    "node_key_property": "NODE" }
  ```
  Construct `NodeSpec`/`EdgeSpec` directly from those dicts (defaults: id_property "NODE", label_property/type_property "LABEL", edge id_property "ID"). Reuse the adapter's own conn for the sink.
- `KineticaAdapter.load_graph(spec)`: if `spec` has a `ddl` string, run it via the adapter (`run_query`/execute) — a `CREATE OR REPLACE DIRECTED GRAPH …`; return `{"status":"ok","graph":spec.get('graph')}`. (Kinetica ingest-from-files is out of scope; Kinetica Create = run the provided DDL.)
- `POST /create` body `{session, spec}` → resolves the session's graph adapter, calls `load_graph(spec)`, returns its result (or `_err`). Reuses `_resolve_adapter`.
- `gateway.js`: `create(spec)` → `POST /create {session, spec}` (session added like other POSTs).

- [ ] Step 1 — Failing tests (no services): a `FakeAdapter.load_graph` returning a canned count; TestClient `POST /create {session,spec}` routes to the session adapter's `load_graph` and returns its result; unknown/loadless spec → error envelope. Plus a unit test that `FalkorDBAdapter`'s spec→`NodeSpec`/`EdgeSpec` construction maps fields correctly (test the builder helper without a live DB).
- [ ] Step 2 — Implement ABC method + both adapters + `/create` + `gateway.js create()`. Extract the spec→Mapping builder as a pure helper for the unit test.
- [ ] Step 3 — Live test (skip if down): `POST /create` with FalkorDB engine + a small spec pointing at `/home/kkaramete/github-graph/graph/falkor/data/vertexes.parquet` (+ edges.parquet), graph name `xgraph_create_test`, the banking node/edge mapping → returns node/edge counts > 0; then `/graphs` lists it; then delete it (or leave). Run FULL suite green.
- [ ] Step 4 — Checkpoint.

### Task 3: Create action (frontend)
**Files:** modify `frontend/XGraph.html`.
**Interfaces (consume):** `gwClient.create(spec)`, `gwClient.listGraphs()`.
- [ ] Step 1 — Add `create` to the `ACTIONS` array right after `connect` (`Setup · Connect · Create · List · Load · …`); reachable when connected (session set). Update reachability accordingly.
- [ ] Step 2 — `CreatePanel` (under `activeAction==='create'`), pre-seeded (editable) with the banking mapping so it works out-of-the-box against the existing `data/*.parquet`:
  - graph name (default e.g. `banking_graph_duckdb`),
  - a node-source row: table name (`expero.vertexes`) + file path (`/home/kkaramete/github-graph/graph/falkor/data/vertexes.parquet`) + node SQL (the falkor `mapping.yaml` node SELECT) + id/label columns + properties,
  - an edge-source row: table name (`expero.edges`) + file path + edge SQL + id/source/target/type,
  - a **"Create / Replace graph"** button → build the `spec` (tables map + nodes[] + edges[]) → `await gwClient.create(spec)` → on success show the returned counts, `setGraphs(await gwClient.listGraphs())`, and advance to `list`; inline error on failure.
  For the **Kinetica** graph engine, instead show a DDL textarea (pre-seeded with a `CREATE OR REPLACE DIRECTED GRAPH …` template) → `gwClient.create({graph, ddl})`.
  Keep the file's inline-style vocabulary.
- [ ] Step 3 — Validate: grep `create` action + `gwClient.create`; @babel/standalone transpile PASS; serve + curl 200.
- [ ] Step 4 — Checkpoint. (Browser acceptance by the user.)

## Self-Review
- Kinetica Load (ontology + browse) → Task 1. ✓
- Create = CREATE OR REPLACE GRAPH; DuckDB→FalkorDB via falkor run_build; Kinetica DDL → Task 2. ✓
- Create action UI pre-seeded with banking mapping → Task 3. ✓
- Reuse (not reimplement) falkor pipeline; contracts unchanged except additive `load_graph` → Global + Task 2. ✓
- No placeholders: spec shape + interfaces explicit; implementers probe live Kinetica for exact response fields (Task 1 Step 1).
