# Graph creation-SQL viewer — design

**Date:** 2026-07-20
**Status:** Approved design, pre-implementation
**Origin:** Users want to see (and copy) the SQL/recipe that built a graph — the `CREATE GRAPH` DDL for Kinetica (from `show_graph`) and the equivalent build recipe for FalkorDB — accessible both per-graph in **List** and for the active graph in **Build**.

## Problem

The backend already exposes `GET /graph_ddl` → `adapter.creation_statement(graph)`:
- **Kinetica** returns the authoritative `CREATE GRAPH` DDL from `show_graph`.
- **FalkorDB** inherits the base default `{"statement": null}` — no server-side DDL exists.

The Build panel already renders this (with session-only `createHistory` fallbacks), but:
1. **FalkorDB has no persistent recipe** — a graph built via `/create` leaves no server-side record, so List (which has no session state) can show nothing, and Build shows nothing after a reload.
2. **List has no DDL affordance** at all.

## Decisions (locked during brainstorming)

1. **Surface in both List and Build.** Per-graph expander in List; the active graph's statement in Build (largely already present).
2. **FalkorDB shows the recorded build recipe only** — the `/create` spec (or extract provenance) that actually built it; blank ("No recorded creation recipe") for graphs not built/extracted through xGraph. No schema-synthesis.
3. **Persist creation recipes** in the existing meta DuckDB so they survive reloads and are available without session state.
4. **Deferred:** no back-fill for graphs created before this ships (they read null until rebuilt); read-only viewer (no editing DDL from it).

## Architecture

### Backend

- **New meta table** `xgraph_creations(graph VARCHAR, engine VARCHAR, statement VARCHAR, source VARCHAR, ts TIMESTAMP, PRIMARY KEY (graph, engine))`, created in `DuckDBComputeEngine._meta_con()` alongside `xgraph_documents`/`xgraph_ontology`.
- **Store methods** on `DuckDBComputeEngine` (mirroring `record_document`/`get_document`):
  - `record_creation(self, graph, engine, statement, source) -> dict` — UPSERT keyed on `(graph, engine)`, naive-UTC `ts`.
  - `get_creation(self, graph) -> dict | None` — latest row for `graph` (any engine), `{graph, engine, statement, source, ts}` with ISO ts.
  - `clear_graph_metadata` extended to also `DELETE FROM xgraph_creations WHERE graph = ?`.
- **Pure recipe renderer** `render_create_recipe(spec) -> str` (module scope in `app.py`, unit-testable): if `spec.get("ddl")` → return it verbatim (Kinetica-via-gateway); else render the FalkorDB `{tables, nodes, edges}` spec into a readable pseudo-recipe, e.g.:
  ```
  -- FalkorDB graph "banking_graph" (built via xGraph /create)
  -- NODES: SELECT id AS NODE FROM vertexes.parquet
  -- EDGES: SELECT src AS SRC, dst AS DST FROM edges.parquet
  ```
- **`POST /create`** — after a successful `load_graph`, record the recipe: `_resolve_compute(session).record_creation(spec["graph"], engine, render_create_recipe(spec), "create")` (best-effort; a recording failure never fails the build).
- **`GET /graph_ddl`** — call `adapter.creation_statement(graph)`; if its `statement` is null, fall back to `_resolve_compute(session).get_creation(graph)` and return `{"statement": <stored>, "source": "xgraph:create-ledger"}` (or the ledger/extract provenance). Kinetica's live `show_graph` DDL still wins (non-null), so no behavior change there.

### Frontend

- **List panel** (`ListPanel`): thread `gwClient` in from the App mount. Each graph row gains a **"⌄ SQL"** toggle (with `e.stopPropagation()` so it doesn't select the graph); on first expand, `gwClient.graphDdl(name)` fetches into a per-row `ddlByGraph` state map and renders a copyable `<pre>` (or "No recorded creation recipe" when null). Collapse hides it.
- **Build panel** (`CreatePanel`): unchanged logic — its existing recipe viewer already shows `activeDdl.statement` first, which now carries the FalkorDB recorded recipe via the `/graph_ddl` fallback. (Only a caption tweak so the source label reads sensibly for the ledger case.)
- `gwClient.graphDdl(graph)` already exists.

## Data flow

- **Build a graph** → `POST /create` → `load_graph` succeeds → `record_creation(graph, engine, render_create_recipe(spec), "create")`.
- **View in List** → expand "⌄ SQL" → `GET /graph_ddl?graph=` → Kinetica: `show_graph` DDL; FalkorDB: recorded recipe from `xgraph_creations`.
- **View in Build** → active-graph effect already calls `graphDdl` → same result.

## Error handling

- Recording is best-effort inside `/create` (wrapped so it never fails the build). `/graph_ddl` keeps the uniform error envelope; a missing ledger row → `{statement: null}` → the UI shows the "no recorded recipe" fallback. List fetch failures leave the row showing a soft error/empty.

## Testing

- **Backend:** unit — `render_create_recipe` (DDL passthrough + FalkorDB spec render); embedded meta DB — `record_creation`/`get_creation` round-trip + `clear_graph_metadata`. Gateway — `/create` writes a creation row (Fake), `/graph_ddl` returns the recorded recipe for a FalkorDB-style graph and null for an unrecorded one; Kinetica `show_graph` DDL live-skip unchanged.
- **Frontend:** `graphDdl` already Node-tested; the List expander is browser-verified (React app not headless).

## Files (indicative)

- **Backend:** `compute/duckdb_engine.py` (table + `record_creation`/`get_creation` + `clear_graph_metadata`), `app.py` (`render_create_recipe`, `/create` recording, `/graph_ddl` fallback), tests.
- **Frontend:** `XGraph.html` (`ListPanel` gwClient + per-row DDL expander; `<ListPanel gwClient=…>` mount; minor Build caption), version bump.

## Deferred / out of scope

- Back-fill of pre-existing graphs' recipes.
- Editing/re-running DDL from the viewer (read-only + copy).
- Schema-synthesized FalkorDB statements (explicitly rejected in favor of recorded recipes).
