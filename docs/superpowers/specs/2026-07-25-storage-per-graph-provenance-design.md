# Per-graph Storage provenance — design

**Date:** 2026-07-25
**Status:** Approved design, pre-implementation
**Origin:** The Storage action should let a user clearly see, for the active graph, **how the data
gets in** (the actual CSV/Parquet source files it was built from) and **how the graph was created**
(the generating SELECTs / DDL and which build route produced it). Today Storage's "DuckDB source
preview" box is hardcoded to the global `HYDRATE_SOURCE` banking files and shows the *same files
regardless of which graph is active* — it is not per-graph.

## Problem

xGraph builds a graph by one of a few **routes**:

1. **Tables/files → DuckDB → FalkorDB** (`/create` → `adapter.load_graph`): a two-stage pipeline —
   `DuckDBSource` reads Parquet/CSV (Step 1 SELECT per node/edge source) → `FalkorDBSink` writes via
   Cypher `UNWIND/MERGE` (Step 2).
2. **Documents → Extract** (`/extract` → `adapter.ingest_elements`): LLM entity/relationship
   extraction → FalkorDB direct MERGE, or Kinetica backing tables + graph.
3. **Kinetica DDL** (`/create` with raw `spec.ddl`): `CREATE … GRAPH …` executed verbatim.

The **recipe** ("How it was built") is already recorded per graph in the DuckDB meta-store ledger
`xgraph_creations` and rendered in Storage. What is missing is a **per-graph preview of the real
source relations** — for the Tables/files route, the CSV/Parquet files that actually fed the graph.
Storage instead previews a hardcoded banking constant, so the "data in" view is wrong for every
non-banking graph.

## Decisions (locked during brainstorming)

1. **Storage first** — this spec; the Build-UI files-route improvements are a separate follow-up.
2. **Store the structured spec in the ledger** — add a nullable `spec_json` column to
   `xgraph_creations`; `/create` records the structured `spec` alongside the rendered recipe. Storage
   reads it to preview each real source relation. (Chosen over fragile recipe-text parsing as the
   primary mechanism, and over a brand-new endpoint.)
3. **Best-effort legacy fallback** — for graphs built before `spec_json` existed (no structured spec),
   parse the source file paths out of the existing recipe text so old graphs still get previews.
   Primary path is `spec_json`; text-parse is only the fallback; neither → a note.
4. **Make the build route explicit** — Storage labels which route produced the graph and lays out
   the flow as **Route → Data in (source files) → How it was created (recipe)**.
5. **No new endpoint** — widen the existing `/graph_ddl` response instead.

## Architecture

### 1. Ledger schema (DuckDB meta store — `compute/duckdb_engine.py`)

Add a nullable `spec_json VARCHAR` column to `xgraph_creations`:

- **Fresh DB:** include `spec_json` in the `CREATE TABLE IF NOT EXISTS` (currently line ~37).
- **Existing DB (migration):** a guarded `ALTER TABLE xgraph_creations ADD COLUMN spec_json VARCHAR`
  — check the column set first (or try/except the "already exists" error), run once on connect.
- `record_creation(graph, engine, statement, source, spec=None)` — `spec` is optional (backward
  compatible); serialize to JSON via `json.dumps` when present, store `NULL` otherwise. INSERT and
  UPDATE branches both carry it.
- `get_creation(graph)` — select `spec_json`; return it as `spec` (a parsed dict via `json.loads`,
  or `None` if NULL/absent). Tolerate the column being missing on a very old DB (fall back to `None`).

The ledger is DuckDB-only (there is no Kinetica mirror of `record_creation`/`get_creation`), so this
is a single-file change.

### 2. Source-derivation seam (`app.py`)

A pure helper `graph_source_relations(recipe: dict) -> list[dict]` returning
`[{"name", "path", "role"}]`:

- **Primary (spec):** from `recipe["spec"]["tables"]` (a `name → path` map). Infer `role`
  (`"nodes"` / `"edges"`) by scanning which `spec["nodes"][i]["sql"]` / `spec["edges"][i]["sql"]`
  references each table name; `role=None` when ambiguous or absent.
- **Fallback (legacy text):** when there is no spec, parse the recipe `statement`'s
  `-- source tables: name = path, name2 = path2` line into the same shape (`role=None`).
- **Neither:** `[]`.

This helper is attached to the existing **`/graph_ddl`** response, which becomes
`{statement, source, spec, sources}`:

- The ledger branch (`source == "xgraph:create-ledger"`) includes `spec` (from `get_creation`) and
  `sources = graph_source_relations(...)`.
- Kinetica-live (`show_graph`) and schema-synthesized branches carry `spec=None`, `sources=[]`.

### 3. `/create` endpoint (`app.py`)

Pass the structured `spec` into the best-effort `record_creation` call it already makes:
`record_creation(spec["graph"], engine, render_create_recipe(spec), "create", spec=spec)`.

### 4. StoragePanel (`frontend/XGraph.html`)

Reshape the panel into a clear **Route → Data in → How it was created** flow, per active graph:

- **Route banner:** derive the route from the recipe/ledger + document ledger:
  - documents present → **"Built from documents (Extract)"**
  - `sources.length` (or `spec.tables`) → **"Built from files (DuckDB → FalkorDB)"**
  - Kinetica `show_graph` DDL → **"Built via Kinetica DDL"**
  - else → **"Built externally / route not recorded"**
- **Data in — source files:** for each entry in `recipe.sources`, call `gwClient.sourcePreview(path)`
  and render one `StorageTable` labeled `name · role · path` (columns + up to 25 sample rows). This
  **replaces** the two hardcoded `HYDRATE_SOURCE` / `HYDRATE_EDGE_SOURCE` tables.
- **How it was created — recipe:** the existing "How it was built" `<pre>` (unchanged).
- **Branch keeps:** extracted graphs keep the document-provenance list; Kinetica graphs keep the
  existing `storage()` backing-table preview (their `sources` is empty because the spec carries DDL,
  not file tables); a graph with no sources and no recipe shows the note *"Source files not recorded
  for this graph — rebuild to capture them."*

### Data flow

```
Build (Tables/files) → POST /create(spec)
    → adapter.load_graph(spec)                     # DuckDB SELECT → FalkorDB sink
    → record_creation(graph, engine, recipe, "create", spec=spec)   # stores statement + spec_json

Storage mount → GET /graph_ddl(graph)
    → { statement, source, spec, sources:[{name,path,role}] }
    → for each source: GET /source_preview(path) → {columns, rows} → StorageTable
```

## Error handling

- **Migration:** the `ADD COLUMN` is guarded and idempotent; `get_creation` tolerates the column
  being absent (returns `spec=None`).
- **NULL spec:** falls through to the recipe-text parse; if that yields nothing, `sources=[]` → note.
- **Per-source preview failure:** rendered inline for that one source; sibling previews and the
  recipe box are unaffected (Storage already isolates per-source fetches with try/catch).
- **Kinetica / extracted:** untouched code paths; `sources=[]` there by construction.

## Testing

- **Backend unit (`tests/test_metadata_store.py`):** `record_creation` with a `spec` →
  `get_creation` returns the parsed dict; a legacy row (NULL `spec_json`) → `spec=None`; migration on
  a table created without the column adds it without error.
- **Backend unit (`graph_source_relations`):** spec path infers `nodes`/`edges` roles; legacy
  text-parse extracts `name = path` pairs; empty input → `[]`.
- **Endpoint (`/graph_ddl`):** with a recorded spec (FakeAdapter + a real DuckDB compute store),
  the response includes `sources` with resolved paths.
- **Frontend:** esbuild JSX check (`ESBUILD_OK`) + gateway `curl` 200; real behavior is
  browser-driven (CLAUDE.md — React app not headlessly verifiable). Extend the `gateway.js` client
  test only if a changed response-shape assertion requires it.

## Files

- `backend/xgraph_gateway/compute/duckdb_engine.py` — `spec_json` column + migration;
  `record_creation(..., spec=None)`; `get_creation` returns `spec`.
- `backend/xgraph_gateway/app.py` — `/create` passes `spec`; new `graph_source_relations` helper;
  `/graph_ddl` widened with `spec` + `sources`.
- `backend/tests/test_metadata_store.py` — spec round-trip + migration + `graph_source_relations`
  cases (plus a `/graph_ddl` endpoint assertion, here or in the existing endpoint test).
- `frontend/XGraph.html` — StoragePanel Route → Data in → recipe reshape; version bump.

## Out of scope (follow-ups)

- **Build-UI files route (part A)** — making the DuckDB "drop a file → it's a relation → pick
  columns → build" flow first-class/visible (FalkorDB `list_tables()` returns `[]`, so there is no
  file picker today). Separate spec.
- **Edge folding parity** — edges get single-label canonicalization only (no `label_raw`, no axis
  surfaced). Separate backend spec.
- Storing full structured specs for Kinetica-DDL graphs (they carry raw DDL, not file tables).

## Note on committing

Per `CLAUDE.md` (*"Do NOT git commit anything under xgraph/"*), this spec is written to disk but
**not committed**. The brainstorming skill's "commit the design doc" step is overridden by that
project rule until the user says otherwise.
