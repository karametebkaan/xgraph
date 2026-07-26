# Unified "Build" panel — design (Create + Extract under one roof)

**Date:** 2026-07-19
**Status:** Approved design, pre-implementation
**Origin:** xGraph's graph-creation surfaces are fragmented and the input selection is the weakest UX
piece. Unify Create/Modify (structured, from tables/files) and Extract (from documents) into one
"Build" roof, borrowing explorer's meticulous Kinetica creation UI and generalizing it to DuckDB.

## Problem

xGraph has **three disjoint creation surfaces**:
1. **CreatePanel** (`Create` action) — a crude Parquet form (node table/file/SQL, id/label cols,
   props, edge fields) + a raw Kinetica DDL textarea. Banking-defaulted, confusing.
2. **Create-Helper** (buried *inside* a Query tab, `createHelper` mode) — the meticulous builder
   carried over from explorer: grammar-driven NODES/EDGES/WEIGHTS/RESTRICTIONS components,
   combo-grouping, per-section default-table with `schema.table.column` override, column
   autocomplete, `Recreate | Modify` (`CREATE OR REPLACE GRAPH` vs `ALTER GRAPH … MODIFY`), and a
   round-trip parser that reconstructs the form from an existing CREATE statement. Kinetica-GQL-only,
   hidden.
3. **ExtractPanel** (`Extract` action) — file/text → LLM → MERGE. Accumulates (append),
   sha256-idempotent, ledger-backed. A separate world.

The user's intent: **one "build a graph" roof** where input is either *structured* (tables/files,
Kinetica **or** DuckDB) or *documents* (Extract), each supporting **recreate vs append**, with a
radio to switch frames.

## Key findings (from exploring explorer + xGraph)

- xGraph **already carries** explorer's meticulous builder (`chRows`/`chMode`/`chSectionTable`/
  `chGenerate` in the Query panel). It is polished; it is just Kinetica-only and mislocated.
- Its DDL assembly (`chBuildSection`/`chGenerate`) is **backend-agnostic** — pure row→SQL string
  building; only the final template (`CREATE OR REPLACE … GRAPH … NODES => INPUT_TABLES(…)` /
  `ALTER GRAPH … MODIFY(…)`) is Kinetica-specific. To drive DuckDB/FalkorDB it needs: (a) a
  table-list source, (b) a column-list source, (c) a different build call, plus (d) a small static
  per-engine **grammar** (what a node/edge table needs: id, optional label, geometry).
- **Both engines unify files and tables** — the "files route" is engine-neutral because each engine
  turns a file into a queryable relation:
  - **DuckDB:** a Parquet/CSV/S3 file is already a relation (`SELECT * FROM 'file.parquet'`); register
    it as a view/temp table and it is just "a DuckDB table" with columns.
  - **Kinetica:** `CREATE EXTERNAL TABLE … FILE PATHS '…/*.parquet' FORMAT PARQUET WITH OPTIONS
    (DATA SOURCE = …)` (materialized or `LOGICAL EXTERNAL TABLE` for live), or `LOAD DATA INTO …
    FROM FILE PATHS …` into a real table. The external table then feeds `NODES =>
    INPUT_TABLES((SELECT … FROM ext_table))` exactly like any native table.

  So the structured builder stays uniformly **table/column** for both engines; "add a file" =
  "register a relation (DuckDB view / Kinetica external table), then pick columns." No separate
  file-vs-table UI. **Kinetica wrinkle:** remote files (`s3://`, `az://`, `gs://`, `hdfs://`,
  `jdbc:`, `kafka://`) resolve through a named `DATA SOURCE` (+ `CREDENTIAL`); local files go through
  KiFS. DuckDB needs only a path. This asymmetry lives entirely in the "register a file" step, not in
  the builder.

## Decisions (locked during brainstorming)

1. **Roof shape:** ONE panel with a top-level **Source** radio (`Tables / files` vs `Documents`)
   that swaps the frame below, plus a **Mode** radio (`Append` vs `Recreate`) applying to both.
2. **Action bar:** replace `Create` + `Extract` with a single **`Build`** action opening this panel.
3. **First target:** **fold Extract under the roof first** (Slice A), then generalize the structured
   builder (Slice B).
4. **Extract mode:** `Append` = today's accumulate (sha256-idempotent MERGE); `Recreate` =
   `delete_graph` then extract fresh. The Mode radio is uniform across source types.
5. **Files → tables (both engines):** the "files route" resolves to a relation — a DuckDB view over
   the file, or a Kinetica external table / `LOAD DATA` over a named `DATA SOURCE` (KiFS for local
   uploads). The builder treats everything as tables/columns. No dual file/table UI.

## Architecture

### The unified panel (`BuildPanel`)

```
┌─ Build ─────────────────────────────────────────────────────────┐
│ HOW <activeGraph> WAS CREATED   (ledger-backed provenance)       │
│ Source:  ( ) Tables / files     (•) Documents                    │
│ Mode:    (•) Append             ( ) Recreate                     │
│ Graph name: [ … ]                                                │
│ ───────────────────────────────────────────────────────────────│
│  «FRAME» (swaps on Source)                                       │
│   Documents  → file / paste text / hint        → [ Build ]       │
│   Tables/fls → structured NODES/EDGES builder   → [ Build ]      │
└──────────────────────────────────────────────────────────────────┘
```

- **Source = Documents** → the current Extract form (file upload / paste text / hint).
- **Source = Tables/files** → the structured builder (Slice A: the current Create form; Slice B: the
  promoted+generalized meticulous builder).
- **Mode = Append** → Documents: accumulate (existing `/extract`); Structured: `ALTER GRAPH … MODIFY`
  (Kinetica) / additive `/create` (DuckDB→FalkorDB) [Slice B].
- **Mode = Recreate** → Documents: `delete_graph` then `/extract`; Structured: `CREATE OR REPLACE
  GRAPH`.
- **Provenance banner** stays on top, ledger-backed (the `/documents`-derived detection already
  shipped), engine-agnostic.

### Slices

- **Slice A — the roof + Extract folded in (frontend only, no backend change).**
  New `BuildPanel` shell with the Source/Mode radios. It renders the *existing* `ExtractPanel`
  (Documents) or `CreatePanel` (Tables/files) unchanged below the radios — a pure relocation, so each
  sub-panel keeps its own graph-name field (a shared roof-level name is deferred to Slice B). Extract
  gains a `buildMode`: `Append` = today's accumulate; `Recreate` = `delete_graph` → `extract`. For
  Tables/files, `CreatePanel` already does `CREATE OR REPLACE` (Recreate); structured Append is
  Slice B, so the Append option is disabled there. Action bar: `Create` + `Extract` → one `Build`
  button. Delivers "same roof" immediately, reusing `/extract`, `/delete_graph`, `/create`.

- **Slice B — promote & generalize the structured builder.** Split into B1 (Kinetica-first) then B2
  (generalize), per the user's chosen sequence.

  **Orphaned-builder finding (2026-07-19):** xGraph *already carries* explorer's full Create-Helper
  (`chRows`/`chSectionTable`/`chGenerate`, the NODES/EDGES/WEIGHTS/RESTRICTIONS grammar sections,
  column autocomplete, `Recreate | Modify`, and the `parseCreateGraphStmt`/`buildAlterSqlFromCh`
  round-trip) — but it is **dead code**: it lives inside `QueryPanel` behind `props.createHelper`, and
  **nothing in the live build mounts a `QueryPanel` with `createHelper=true`** (the `+ Create`
  `DashboardHeader` and the floating `queryPanels` array are never rendered). That is why the builder
  has never been visible. B1 makes it live for the first time.

  **B1 — Kinetica structured builder in Build → Tables/files (frontend-only).**
  - Hoist the two pure helpers `chGenericId` + `chParseRef` (already shared by the Solve/Match
    helpers) to module scope, plus the Modify round-trip `parseCreateGraphStmt` / `buildAlterSqlFromCh`.
  - Extract a standalone **`CreateHelperPanel`** that owns the builder state (`chRows`,
    `chSectionTable`, `chMode`, `chOptions`, graph-name/directed) + the section UI + a **Generate**
    button, and emits the assembled DDL through one `onGenerate(ddl)` callback. It owns **no**
    run/results/window-chrome — that tangle stays behind.
  - Mount `<CreateHelperPanel>` in `CreatePanel`'s **Kinetica** branch, above the existing DDL
    textarea; `onGenerate` fills `createDdl`, and the existing **Create / Replace graph** button
    (`gwClient.create({graph, ddl})`) executes it — the correct, already-working execute path. The
    DDL stays visible and hand-editable before running.
  - De-dup safely by **sequencing**: stand the new component up and prove it live first, then remove
    `QueryPanel`'s now-redundant orphaned Create-Helper (its JSX block + CH-only state + session-save
    references) as the final step, so B1 still ships value even if that removal is deferred.
  - **B1 limitations, resolved in B2 (stated, not hidden):** the section **table dropdowns are empty**
    (no `/tables` yet — the user types each section's DEFAULT TABLE, e.g. `expero.vertexes`), and
    **column autocomplete is best-effort** (the existing fetcher hits Kinetica's `/get/records`
    directly via `credentials.url`, not the gateway, so it may be unreachable — manual `table.column`
    entry always works). The grammar is the built-in `DEFAULT_GRAMMAR`.

  **B2 — generalize beyond Kinetica.** A per-engine **grammar** (Kinetica from `/show/graph/grammar`
  with the static fallback; DuckDB/FalkorDB a static grammar — id, optional label, geometry),
  **table-list** and **column-list** sources per engine (new gateway `/tables` + `/columns`, replacing
  the direct-to-Kinetica autocomplete), and the correct per-engine build call. Source = tables
  including **file-backed relations**, per engine: DuckDB registers `CREATE VIEW v AS SELECT * FROM
  '<path>'` (or accepts a path directly); Kinetica registers `CREATE EXTERNAL TABLE … FILE PATHS …
  FORMAT … WITH OPTIONS (DATA SOURCE = …)` (or `LOAD DATA INTO …`), local files via KiFS and remote
  files via a named `DATA SOURCE` + `CREDENTIAL`. Either way the result is a table the picker
  consumes. New backend read/register endpoints (below). Keep Recreate vs Append (`CREATE OR REPLACE`
  vs `MODIFY`/additive) and the round-trip parse-from-existing.

- **Slice C — polish.** WKT/geo grammar entries, multi-table combos, live-DDL preview parity, and
  folding the Solve/Match helpers onto the same generic "grammar-driven INPUT_TABLES section"
  component so xGraph doesn't carry three near-duplicates.

### Backend

- **Slice A:** none. Reuses `/extract`, `/delete_graph`, `/create`.
- **Slice B:** new read/register endpoints the builder's autocomplete needs, uniform across engines:
  - `GET /tables?engine=&session=` → list tables/relations (Kinetica `/show/table` + external tables
    and `SHOW DATA SOURCE`; DuckDB `SHOW TABLES` plus registered file-views).
  - `GET /columns?engine=&session=&table=` → column names for a table/relation (Kinetica record probe;
    DuckDB `DESCRIBE`). xGraph has neither today.
  - `POST /register_file` → turn a file path into a relation for the picker: DuckDB `CREATE VIEW …
    SELECT * FROM '<path>'`; Kinetica `CREATE EXTERNAL TABLE … FILE PATHS … WITH OPTIONS (DATA SOURCE
    = …)` (or KiFS upload for local files). Returns the resulting table name.
- Build calls are unchanged: `/create` (Parquet/table → FalkorDB, or Kinetica DDL), `/extract`.

## Data flow

- **Documents · Append:** `POST /extract` (as today).
- **Documents · Recreate:** `POST /delete_graph` → `POST /extract`.
- **Structured · Recreate:** builder emits `CREATE OR REPLACE … GRAPH …` → `POST /create` (Kinetica
  DDL) or `/create` spec (DuckDB→FalkorDB from tables/views).
- **Structured · Append:** builder emits `ALTER GRAPH … MODIFY(…)` (Kinetica) / additive `/create`
  (DuckDB→FalkorDB) [Slice B].

## Error handling

- Recreate-from-documents: `delete_graph` is best-effort/idempotent; a failure surfaces via the
  existing `_err` envelope before extraction runs.
- Structured build validation (missing required id column, empty section) mirrors explorer's
  client-side guards; the backend build call still returns the uniform error envelope.
- Table/column endpoints degrade to an empty list on an unreachable engine (autocomplete just has no
  suggestions); never block manual entry.

## Testing

- **Slice A (frontend):** `gateway.js` unchanged (reuses existing client calls); the React panel is
  browser-verified (no headless runtime). Validate via Babel/esbuild transpile + a `curl` smoke of
  `/extract` (append) and `/delete_graph`→`/extract` (recreate). Backend suite unaffected.
- **Slice B (backend):** unit + live-skipping tests for `/tables` and `/columns` per engine
  (Kinetica live-skip; DuckDB embedded, incl. a file-backed view); the builder's row→DDL assembly is
  pure and Node-testable if extracted into `gateway.js`.
- Regression: the existing Create/Extract flows keep working through the new roof.

## Files (indicative)

- **Frontend:** new `BuildPanel` (wraps the Extract frame + the structured frame with the
  Source/Mode radios); action bar `Create`+`Extract` → `Build`; the meticulous builder relocated out
  of the Query panel and generalized (Slice B). Version bump.
- **Backend (Slice B):** `app.py` `GET /tables` / `GET /columns`; adapter methods `list_tables()` /
  `list_columns(table)` (FalkorDB via graph_loader/DuckDB; Kinetica via gpudb); DuckDB compute
  helpers for `SHOW TABLES`/`DESCRIBE` and file-view registration.

## Deferred / out of scope

- Slice C polish (WKT/geo grammar, live-DDL preview parity, unifying Solve/Match helpers).
- Any change to how graphs are physically built (`/create`, `/extract` internals) — this is a
  UX-unification effort, not a change to the build mechanics.
