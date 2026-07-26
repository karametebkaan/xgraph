# Extraction label folding + facets/axes + document bookkeeping — design

**Date:** 2026-07-17
**Status:** Approved design (revised to include facets/axes), pre-implementation
**Origin:** Port kgr's (`~/github-graph/graph/graphrag/kgr`) extraction ontology-folding, faceted
multi-label ontology, and the timestamped `documents` ledger into xgraph, keeping engine-neutrality.

## Problem

xgraph's `/extract` today:
- `extract.py::extract_document` dedupes entities by `canonical_id(name)` but does **no label
  folding** — `Company`, `Organization`, `Firm` become three distinct labels; the ontology sprawls.
- Each entity carries **exactly one label** — no way to say "Anthropic is a *Company* (structural)
  that is *AI* (industry)". Classification facets are lost.
- `ingest_elements` records **no provenance**: no `documents` ledger, no `sha256` idempotency, no
  timestamps. Re-submitting a document silently re-runs the LLM and re-MERGEs.

kgr solved all three, but its state lives in **Kinetica SQL tables** — Kinetica-only. xgraph is
engine-neutral (FalkorDB + Kinetica + fake).

## What already exists in xgraph (do not rebuild — build on it)

The "attributes live on the nodes/edges, the graph grammar claims only the identification combo, the
rest is Cypher-accessible" model (**formerly called deferred "#3"**) is **already implemented** and
is the embraced design:

- **Kinetica** (`kinetica_adapter.ingest_elements`): per-graph backing tables
  `node_table_name(graph)`/`edge_table_name(graph)`; `discover_attr_columns` → `_evolve_columns`
  **`ALTER TABLE`s new attribute columns as the ontology varies across appends**;
  `create_graph_sql(...)` rebuilds `CREATE GRAPH` over the wide table so the graph engine uses the
  NODE/LABEL identification combo and the remaining columns stay accessible to the Cypher planner.
- **FalkorDB** (`falkordb_adapter.build_ingest_cypher`): `MERGE (n:Entity {NODE:r.id}) SET n:LABEL,
  n.LABEL=$label, n.name=r.name, n += r.attrs` — `n += r.attrs` sets every attribute as a node
  property; schemaless, so varying ontology just adds properties. Cypher-accessible natively.

So attribute-carrying nodes/edges reshaped-on-append is **done**. This spec adds folding, faceted
multi-labels, and the provenance ledger **on top of** that, without disturbing attribute hydration.

**Confirmed live:** FalkorDB supports multiple labels per node — `CREATE (n:Company:AI) RETURN
labels(n)` → `["Company","AI"]`. Faceted multi-label nodes are viable on both engines.

## Decisions (locked during brainstorming)

1. **State store follows the session's selected OLAP engine.** DuckDB session → DuckDB tables;
   Kinetica session → Kinetica tables (kgr schema verbatim + a `graph` column). No mixing.
2. **All in one spec:** folding + facets/axes + bookkeeping (shared store).
3. **Fold logic = kgr hybrid:** deterministic alias lookup, then an LLM synonym check only for a
   genuinely-new type name. Applies to structural labels **and** facet labels.
4. **Faceted multi-labels ARE in scope.** A node carries a label *vector* (one structural label +
   zero or more facet labels), each label sitting on an **axis** (`LABEL_KEY`). Relations keep a
   single canonical label plus an (optional) edge axis for grouping (kgr's edge model).
5. **Attribute hydration stays as-is** (the existing model above): attributes live on the
   nodes/edges (Kinetica wide columns / FalkorDB properties), not induced from the ontology table.
   The ontology table does **not** store `attr_name`/`attr_sql_type` — attrs are discovered from the
   payload at ingest, as today.
6. **Per-graph scoping.** Ontology + ledger keyed by graph name; one graph's vocabulary never folds
   another's.

## Architecture

### The seam: metadata store on the `ComputeEngine` contract

`ComputeEngine` is the per-session, OLAP-selected, engine-neutral layer (`DuckDBComputeEngine` /
`KineticaComputeEngine`, resolved by `registry.get_compute`). The metadata store is **new methods on
that contract**, implemented by DuckDB, Kinetica, and a Fake. Kinetica session → kgr's tables.

**Storage vs logic separated:** `ComputeEngine` does CRUD only; a new engine-neutral module
**`extract_fold.py`** holds the folding + facet-resolution algorithm (ported from kgr `ontology.py`,
minus the induced-attr columns), depending only on the store interface + an injectable LLM func.

New `ComputeEngine` methods (names indicative):

```
# Document ledger
record_document(graph, doc_uri, sha256, source_type) -> {status, first_ingested_ts, last_ingested_ts}
    # status ∈ {"new","unchanged","updated"}; upsert on PK (graph, doc_uri).
list_documents(graph) -> [ {...ledger row...} ]

# Ontology / folding / axes
get_canonicals(graph, kind) -> [canonical_name, ...]
resolve_canonical(graph, kind, type_name) -> canonical_name | None     # exact + normalized alias
record_type(graph, kind, type_name, canonical_name, axis, source_uri)  # canonical if name==canonical
axis_map(graph, kind) -> {label -> axis}                               # for schema grouping + ingest
```

### Tables (per-graph; kgr schema + `graph` column; no induced-attr columns)

`xgraph_documents` — PK `(graph, doc_uri)`: `sha256`, `source_type`, `first_ingested_ts`,
`last_ingested_ts`, `status`.

`xgraph_ontology` — PK `(graph, type_kind, type_name)`: `canonical_name`, `axis`, `first_seen_uri`,
`first_seen_ts`. A row is a **canonical** when `type_name == canonical_name`, else an **alias**;
`axis` is the label's facet dimension (`EntityType` default for structural, e.g. `Industry`/
`Technology` for facets; a relation axis for edges). `axis` is the normalized source of truth.

**Kinetica only** additionally materializes kgr's `label_keys` / `edge_label_keys` (one row per
axis → array of labels) from `xgraph_ontology`, rebuilt before each `CREATE GRAPH`, and feeds them
into the graph's NODES/EDGES grouping SELECTs. FalkorDB needs no such tables — it derives axis
grouping from `axis_map` for schema/DOT display only.

Deferred from kgr, still omitted: induced attribute columns in the ontology (`attr_name`,
`attr_sql_type`) — attrs are discovered at ingest (existing behavior).

### DuckDB persistence

DuckDB `ComputeEngine` opens `duckdb.connect()` (in-memory) per call today. The ledger + ontology
need a **persistent DuckDB file** (e.g. `XGRAPH_DATA_DIR/xgraph_meta.duckdb`, configurable);
`CREATE TABLE IF NOT EXISTS` on first use. Existing hydrate/OLAP paths stay in-memory. Kinetica
backend issues kgr-style DDL on first use.

## Extraction proposal shape (extends `extract.py`)

`_EXTRACT_SCHEMA` + `_prompt` extended so the LLM proposes, per entity:
- one **structural label** (the primary type), and
- zero or more **facets**, each `{name, axis}` (e.g. `{"name":"AI","axis":"Industry"}`).

Relations keep a single `label` (+ optional axis). `extract_document`'s dedupe/merge is unchanged
except entities now carry `facets` alongside `label`/`attrs`.

## Data flow — `/extract`

1. Resolve adapter + compute(store) from session.
2. `sha256(doc_bytes)`; `doc_uri` = upload filename or `text:<sha256[:12]>`; `source_type` ∈
   {`file`,`text`}.
3. `store.record_document(...)`. **`unchanged`** → short-circuit: skip re-extraction, return
   `reused: true`.
4. `extract_document(...)` → raw proposal (structural labels + facets + attrs).
5. **`fold_proposal(store, graph, proposal, source_uri)`** (`extract_fold.py`):
   - Fold every structural label AND every facet label to its canonical (deterministic →
     LLM synonym check → new canonical), persisting alias/canonical rows with their `axis`.
   - Build each entity's **label vector**: `[structural_canonical, *facet_canonicals]` (deduped),
     plus `label_raw` = the pre-fold labels for provenance.
   - Fold relation labels likewise (single label + axis).
   - LLM fold-check error ⇒ treat as new canonical (never blocks ingest).
6. `ingest_elements(graph, entities, relations)` — extended to accept the label vector + `label_raw`
   (see Adapter changes).
7. Return counts + folding report + document record.

## Adapter changes (`ingest_elements`)

**FalkorDB** (`build_ingest_cypher`): set the full label vector (`SET n:Company:AI`), store
`n.LABEL = $labels` (array), `n.label_raw = $raw`, keep `n += r.attrs`; add provenance
`ON CREATE SET n.first_seen_ts = $ts` / always `SET n.last_seen_ts = $ts`. Edges: single canonical
`LABEL`, `x += attrs`, provenance `ts`.

**Kinetica**: `LABEL` becomes `VARCHAR[]` (array) on the backing node table; add `label_raw
VARCHAR[]` and `first_seen_ts`/`last_seen_ts` columns; keep the existing attr-column evolution;
materialize `label_keys`/`edge_label_keys` from `xgraph_ontology` and include the grouping SELECTs in
`create_graph_sql`.

**Fake**: mirror the shape for tests.

## Schema / DOT (`get_schema`)

`get_schema` groups a graph's labels by axis (via `axis_map`) so the ontology view / DOT shows facet
dimensions (`EntityType → [Company, Person]`, `Industry → [AI, Technology]`). Kinetica can read this
from `/show/graph` label_keys; FalkorDB composes it from `axis_map` + `labels()`.

## API changes

`POST /extract` response gains:
```
"folded":   [ {kind, from, to, axis}, ... ]     # raw label → canonical (+ its axis) applied
"document": { doc_uri, sha256, status, first_ingested_ts, last_ingested_ts, reused }
```
Entities in the response carry their label vector. Optional `GET /documents?engine=&graph=` →
`list_documents` for a provenance view (drop if it grows the slice).

## Error handling

- Fold-check LLM failure → new canonical, ingest proceeds.
- `record_document` PK upsert is race-safe.
- Store backend unreachable → propagates via existing `_err` envelope (502). DuckDB metadata file is
  local, always available.
- Multi-label / array `LABEL`: downstream transforms (`graphTableFromGateway`, node-detail) must
  tolerate an array `LABEL` (pick the structural label for display); covered in frontend follow-up.

## Engine-neutrality

- `ComputeEngine` gains metadata methods; DuckDB + Kinetica + Fake implement them.
- `FakeComputeEngine` holds in-memory dicts — folding + ledger + axes unit-test with no services.
- `extract_fold.py` is pure/neutral: store interface + injectable LLM func only.

## Testing

- **Unit (no services):**
  - fold logic: deterministic alias hit; LLM synonym hit (fake LLM); genuinely-new canonical; LLM
    error → new canonical; facet label folded + axis persisted.
  - label-vector build: structural + facets deduped; `label_raw` captured.
  - ledger idempotency: new / unchanged / updated — Fake **and** embedded DuckDB.
  - `/extract` gateway test (FakeAdapter + FakeComputeEngine): folded vector, response shape.
- **Integration (SKIP if down):**
  - Kinetica metadata round-trip + `label_keys` materialization + `CREATE GRAPH` grouping.
  - full `/extract` folding + multi-label on FalkorDB; re-ingest → `reused`.
- **Regression:** `test_extract_folding.py` — `Company`/`Firm`/`Organization` → one canonical;
  facet `AI` lands on the `Industry` axis; re-ingest of identical bytes → `reused: true`, zero new
  nodes.

## Files

- `compute/` — extend the contract with metadata methods; persistent-file handling in
  `duckdb_engine.py`; kgr-style DDL + `label_keys` materialization in `kinetica_engine.py`; add a
  `FakeComputeEngine`.
- **new** `xgraph_gateway/extract_fold.py` — neutral folding + facet/axis resolution (ported from
  kgr `ontology.py`, minus induced-attr columns).
- `extract.py` — extend `_EXTRACT_SCHEMA` + `_prompt` for structural label + facets; carry `facets`
  through `extract_document`.
- `adapters/falkordb_adapter.py` — multi-label + `label_raw` + provenance ts in `build_ingest_cypher`.
- `adapters/kinetica_adapter.py` — array `LABEL`, `label_raw`, provenance ts, `label_keys` grouping.
- `adapters/fake.py` — mirror for tests.
- `app.py::extract_endpoint` — wire steps 2–7; response fields; optional `/documents`.
- `get_schema` (both adapters) — axis grouping.
- **new** tests as above.

## Deferred (out of scope)

- Frontend handling of array `LABEL` / facet display in the React app (its own follow-up; backend
  returns the vector + structural label so the UI can adopt incrementally).
- Induced attribute *columns from the ontology* (kgr `attr_name`/`attr_sql_type` + `ALTER TABLE`
  driven by ontology) — **not needed**: xgraph already evolves attr columns from the ingest payload
  (Kinetica) / sets them as properties (FalkorDB), which is the embraced model.
