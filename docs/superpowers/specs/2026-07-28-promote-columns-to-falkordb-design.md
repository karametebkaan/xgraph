# Promote Source Columns into FalkorDB Properties — Design

**Date:** 2026-07-28
**Status:** Approved (design); ready for implementation plan.
**Engine scope:** FalkorDB only.

## Problem

xgraph builds FalkorDB graphs two ways:

1. **Extraction** (text → LLM → graph): the LLM emits free-form `attrs` per
   entity, and the loader pushes them into FalkorDB's schemaless property maps
   (`n += r.attrs` in `falkordb_adapter._upsert_statements`). Those attributes
   are therefore **mid-traversal filterable** (`WHERE n.population > 1000000`).

2. **Wide-source create** (falcor-style banking Parquet): the graph is kept
   **skinny** — only structural columns (NODE ids, LABEL, edges) live in
   FalkorDB. The wide attribute columns (e.g. `party:party_name`, amounts) stay
   in Parquet/DuckDB and are joined onto results *after* a traversal via
   post-hydration (`graph_loader/hydrate.py`).

The gap: for wide-source graphs, a Cypher query that references a wide column
mid-traversal does **not error** — FalkorDB returns NULL for a missing
property — so `WHERE n.\`party:party_name\` = 'Acme'` silently matches nothing.
The attribute simply isn't in the graph to filter on.

Today the only lever is the create-time projection (`spec.nodes[].properties`),
decided up front at build time. There is no way to bring a wide column into the
graph **on demand**, after the fact, for the columns a query actually needs.

## Goal

Add an explicit **promote** operation: read one or more columns from a wide
source (Parquet/CSV/table) and materialize them as properties on the matching
existing FalkorDB nodes, so they become mid-traversal filterable. This is
"post-hydrate, but into FalkorDB" — the inverse of keeping the graph skinny,
chosen deliberately per column, trading RAM for query-time filterability.

## Non-goals (deferred)

- **Auto-detect from Cypher** (parse a query, find referenced props that don't
  exist, promote them automatically). Explicit promotion is the foundation;
  auto-detect can layer on later. It is harder because the Cypher name
  (`n.party_name`) need not match the real source column (`party:party_name`).
- **Traversal-scoped promotion** (only the ids a query touched). This design
  promotes the **whole column across all nodes**.
- **Kinetica.** Kinetica already evolves real typed columns at extract time
  (`ALTER TABLE ADD COLUMN`); the skinny/wide gap is a FalkorDB concern.
- **Per-column aliasing / renaming.** Property keys are the **verbatim** source
  column names.

## Key decisions (locked during brainstorming)

| Decision | Choice |
|---|---|
| Trigger | Explicit promote; column names come from the source schema (no guessing) |
| Scope | Whole column — all matching nodes in the graph |
| Engines | FalkorDB only |
| UI | A control in the Query panel |
| Property naming | **Verbatim** source column name (e.g. `party:party_name`) |
| Node creation | **Never** — MATCH existing nodes only (no MERGE) |
| Null cells | **Skipped** — a null source value is not written as a property |

## Design

### Data flow

```
Query panel (engine === 'falkordb')
  │  pick source (default "vertexes.parquet"), key (default "NODE"),
  │  columns (multi-select populated from /columns → list_columns(source))
  ▼
POST /promote_columns { session, graph, source, key, columns[] }
  ▼
gateway:
  1. read wide rows via DuckDB:
       SELECT <key>, <col1>, <col2>, ... FROM '<source>'
     (projection pushdown; streamed, not fully materialized)
  2. for each row build attrs = { col: val for col in columns if val is not None }
     drop the row if attrs is empty (all requested cells null) or key is null
  3. write to FalkorDB in batches (5000 rows), per batch:
       UNWIND $rows AS r
       MATCH (n {NODE: r.id})       // label-agnostic; NODE = the configured key
       SET n += r.attrs
  ▼
response { promoted[], nodes_matched, properties_set, source, key }
```

### Naming: verbatim, and why quoting differs by side

The property key is the exact source column string, e.g. `party:party_name`.

- **Write side:** keys travel inside the `$rows[].attrs` **parameter map**, never
  interpolated into the query text. A map key may be any string, so the colon
  is safe with no escaping. `SET n += r.attrs` sets each map entry as a property.
- **Read side (user's Cypher):** a property name with a colon or other
  non-identifier character must be backtick-quoted:
  `` WHERE n.`party:party_name` = 'Acme' ``.

After a successful promote, the UI shows a copy-paste hint with the exact
backtick-quoted form so the user does not have to guess.

### MATCH-only semantics (never creates nodes)

The `MATCH` is **label-agnostic**, keyed only on the node-key property (default
`NODE`). This is required: wide-source (falcor create) graphs label nodes with
their own labels (e.g. `:party`) via `mapper.node_batches`, not `:Entity` — a
`:Entity`-constrained match would miss exactly the graphs this feature targets.
Label-agnostic keying on `NODE` matches both extraction and create graphs, and
follows the convention already used by `attributes_for` and `get_record`.

Promotion uses `MATCH`, not `MERGE`. Consequences, all intended:

- A source row whose `key` has no matching graph node → **ignored** (no node
  created).
- A graph node with no source row → keeps NULL for that column.
- Re-promoting the same column → idempotent overwrite (a refresh).

This is a deliberate difference from `ingest_elements` (which MERGEs and would
create orphan nodes from unmatched source rows).

### Null handling

Per row, `attrs` is built from only non-null cells. A node whose every
requested cell is null contributes no write. This keeps NULLs out of the
property maps (Cypher treats an absent property and NULL identically, and it
saves per-node RAM).

**Documented consequence:** because null cells are skipped rather than written,
a re-promote will **not** overwrite a previously-set value with a newly-null
one (a stale-value edge case). Acceptable: promotion is a snapshot; a full
refresh of a changed source is a re-promote, and clearing values is out of
scope.

### Snapshot / freshness

Promotion is a point-in-time snapshot of the source. If the source file
changes, promoted properties go stale until re-promoted. This mirrors any
denormalization and is called out in the UI hint ("snapshot of `<source>`").

### Endpoint contract

`POST /promote_columns`

Request body:
```json
{
  "session": "<session id, optional>",
  "graph": "<graph name>",
  "source": "vertexes.parquet",
  "key": "NODE",
  "columns": ["party:party_name", "party:risk_score"]
}
```

Response (200):
```json
{
  "promoted": ["party:party_name", "party:risk_score"],
  "nodes_matched": 12345,
  "properties_set": 24680,
  "source": "vertexes.parquet",
  "key": "NODE"
}
```

- `nodes_matched` = distinct nodes that received at least one property.
- `properties_set` = total non-null cells written across all nodes/columns.

Errors (uniform `{"error":{code,message,engine,detail}}` envelope):

- engine ≠ falkordb → **400** `"promotion not supported for <engine>"`
  (message notes Kinetica materializes real columns at extract time).
- `columns` empty/missing → **400**.
- unreadable/invalid `source` → surfaced by the existing `describe_source`
  guard (400 bad path / 502 unreachable).
- `key` not a safe identifier → **400** (via `safe_ident`).

### Components / files

- **`backend/xgraph_gateway/adapters/base.py`** — declare
  `promote_columns(graph, source, key, columns) -> dict` on the adapter
  contract (default: raise NotImplementedError / "unsupported").
- **`backend/xgraph_gateway/adapters/falkordb_adapter.py`** — implement
  `promote_columns`: read via the compute engine's whole-column reader, build
  null-stripped attrs, batch the MATCH-only `SET n += r.attrs` (reuse the
  5000-row batching style already in `mapper`/`build_ingest_cypher`).
- **`backend/xgraph_gateway/compute/duckdb_engine.py`** — a whole-column reader
  `read_columns(source, key, columns) -> list[dict]` (the hydrate projection
  read without the `WHERE key IN (...)` id filter; reuse `coerce_row` for the
  Decimal→float coercion and `describe_source` for validation).
- **`backend/xgraph_gateway/adapters/kinetica_adapter.py`** — `promote_columns`
  raises the "unsupported for kinetica" error (explicit, not silent).
- **`backend/xgraph_gateway/app.py`** — `POST /promote_columns` route: resolve
  adapter + compute for the session, engine-gate, validate, call adapter,
  return the response shape.
- **`frontend/gateway.js`** — `promoteColumns(graph, source, key, columns)`
  client method (POST, returns the response object).
- **`frontend/XGraph.html`** — a collapsible "Promote columns" control in the
  Query panel (gated to `engine === 'falkordb'`): source field (default
  `HYDRATE_SOURCE`), key field (default `NODE`), a column multi-select fetched
  via `/columns`, a Promote button, and a result line with the backtick-quoted
  Cypher hint. Bump `EXPLORER_VERSION`.

### Testing

- **Unit (backend):** the statement builder emits a MATCH-only `SET n += r.attrs`
  (no MERGE) with verbatim keys; null cells are stripped from attrs; an
  all-null row is dropped.
- **Gateway (Fake adapter):** `/promote_columns` calls `promote_columns` with
  the parsed args; engine-gating returns 400 for a non-FalkorDB engine; empty
  `columns` returns 400.
- **Live FalkorDB (skip if unreachable):** build a tiny graph, promote a column
  from a small Parquet/CSV fixture, then run a Cypher `WHERE` on the
  backtick-quoted promoted property and assert it filters correctly; assert a
  node with a null source cell has no such property.
- **Frontend:** `gateway.js promoteColumns()` posts the right body and returns
  the response (Node test with injected fake `fetch`).

## Alternatives considered

- **Reuse `ingest_elements`** — rejected: MERGE creates orphan nodes for
  unmatched source rows and forces label handling.
- **Rebuild via the create spec** (`spec.nodes[].properties`) — works today but
  requires a full rebuild, is not on-demand, and loses per-query ergonomics.
  Remains available as the up-front alternative; this feature is the on-demand
  path.

## Risks

- **Write cost at scale:** whole-column promotion on a large graph (e.g. the
  622k-node banking graph) is a one-time batched write proportional to node
  count per promoted column. Batching (5000) bounds memory; the operation is
  synchronous — the UI shows progress/result. Acceptable for the demo scale; a
  future async/streaming variant is out of scope.
- **RAM growth:** the explicit tradeoff the feature exists to make — promoted
  columns increase FalkorDB memory per node. Bounded by promoting only the
  columns actually needed.
- **Staleness:** snapshot semantics (see above), documented in the UI.
