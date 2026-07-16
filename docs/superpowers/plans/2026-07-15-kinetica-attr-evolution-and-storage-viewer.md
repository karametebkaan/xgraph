# xGraph — Kinetica Extract attribute evolution + Storage viewer

**Goal (2 tasks):**
1. **Kinetica Extract evolves its schema** — extracted node/edge attributes become real, typed columns on `<graph>_nodes`/`<graph>_edges`, added via `ALTER TABLE ADD COLUMN` as new attributes appear (kgr-style), populated on upsert, and exposed in `CREATE OR REPLACE DIRECTED GRAPH` so they're queryable in GQL (no hydration — the columns are in the table, for joins/filters). FalkorDB already stores attrs as node/edge properties, so no FalkorDB change.
2. **Storage viewer** — an action + endpoint to inspect the underlying storage: Kinetica extract tables (columns + sample rows) and DuckDB source files (DESCRIBE + sample). DuckDB is stateless (views over files), so there's nothing persistent — only file previews.

## Constraints
- Work in `~/xgraph`; commit per task. Backend on `~/xgraph/backend/.venv`; full suite (227 now) stays green; live tests SKIP if services down.
- Identifiers via `graph_loader.mapper.safe_ident`; data values via `insert_records_json` payload / parameters, never string-interpolated.

---

### Task 1: Kinetica Extract attribute evolution (`kinetica_adapter.py`)
- Type inference `_infer_col_type(value)`: bool→`BOOLEAN`, int→`BIGINT`, float→`DOUBLE`, else `VARCHAR(1024)`. First non-null value wins per key.
- Attr-column discovery: union `attrs` keys across incoming nodes (and edges separately); `safe_ident` each key; skip keys colliding with base cols (`NODE,LABEL,name,entity_name` for nodes; `edge_key,NODE1,NODE2,LABEL` for edges).
- `ingest_elements` (Kinetica), per table (nodes, then edges):
  1. ensure schema + base table (as today).
  2. read the table's CURRENT columns (via the GPUdb `show_table` response type).
  3. for each attr key not present: `ALTER TABLE <table> ADD COLUMN <safe_key> <inferred_type>` (run via the same execute path the adapter already uses for DDL).
  4. build row payloads INCLUDING attrs (base cols + one field per attr key, values coerced best-effort to the column's type; unconvertible → null); upsert via `insert_records_json(..., {"update_on_existing_pk":"true"})`.
  5. rebuild `CREATE OR REPLACE DIRECTED GRAPH` — the NODES select now lists `NODE, LABEL, name AS entity_name, <each node attr col>`; EDGES lists `NODE1, NODE2, LABEL, <each edge attr col>` (only include columns that exist; keep the `name AS entity_name` landmine alias).
  - Never raise on empty inputs. Column types never change once declared (kgr rule).
- `get_schema`: already reads the node backing-table columns into `properties` — confirm it now surfaces the new attr columns (and add edge attr columns if it doesn't already), so NL→Cypher can filter/return them.
- Tests: unit (`_infer_col_type`; attr-column discovery; `create_graph_sql` includes attr columns; ALTER statement built correctly + safe). Live (SKIP if Kinetica down): ingest nodes with `attrs={"population": 5000}` on a Location → column added; a GQL `... WHERE o.population > 1000 RETURN ...` returns it; RE-ingest with a NEW attr key → new column added, prior rows null; base label MATCH still works (no NODE_NAME regression).
- Full suite green. Commit `feat(extract): Kinetica extract evolves typed attribute columns (ALTER ADD COLUMN, exposed in CREATE GRAPH)`.

### Task 2: Storage viewer (endpoint + action)
- Backend `GET /storage?graph=&engine=&session=` → for the resolved adapter:
  - **Kinetica**: `{"kind":"kinetica","tables":[{"name":"<graph>_nodes","columns":[...],"rows":[... up to 25 ...]}, {edges...}]}` — discover the extract backing tables (via `node_table_name`/`edge_table_name`); if they don't exist (non-extract graph) return `{"kind":"kinetica","tables":[]}` with a note.
  - **FalkorDB**: `{"kind":"falkordb","note":"FalkorDB stores the graph itself — use Visualize/Ontology.","tables":[]}`.
  - Add `storage(self, graph)` to the adapter ABC (concrete default returns the FalkorDB-style note) + Kinetica override; FalkorDB uses the default.
  - Also a DuckDB source preview: reuse `POST /sql` or add `GET /source_preview?source=` → `compute.describe_source(source)` + a `SELECT * ... LIMIT 25`. (DuckDB is stateless; this previews the file.)
- `gateway.js`: `storage(graph)` and `sourcePreview(source)`.
- Frontend **Storage** action (after Ontology, or in List): for the active graph's engine, show the backing tables (Kinetica: columns + sample grid) or the FalkorDB note; plus a small "DuckDB source" preview box that DESCRIBEs+samples the configured `HYDRATE_SOURCE`. Babel-verify.
- Tests: endpoint with FakeAdapter.storage; Kinetica live (SKIP if down) returns the two tables for an extract graph. Commit `feat(storage): /storage + /source_preview endpoints + Storage action`.
