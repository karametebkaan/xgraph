# xgraph — S0+S1 Design: adapter contracts + vendor-neutral viz on FalkorDB

**Date:** 2026-07-13
**Status:** Approved (brainstorming) — pending spec review, then implementation plan
**Scope of this doc:** the first vertical slice — **S0** (foundation contracts) + **S1** (neutral
visualization on FalkorDB, including a DuckDB hydration pass). Later slices (S2–S4) are described
only enough to keep the S0 contracts platform-shaped.

## 1. What xgraph is

`xgraph` is a new sibling project under `graph/` that unifies three existing projects into a
**vendor-neutral graph workbench** — one UI and one API over multiple graph engines, replacing the
per-vendor UIs:

- **`explorer`** — a pure client-side React UI (force-graph node-link, deck.gl/MapLibre geo,
  Graphviz-WASM ontology, Chart.js). Today it talks REST straight to Kinetica; everything
  Kinetica-specific funnels through one `useKineticaApi` hook. It has no LLM features.
- **`kgr` (graphrag)** — the intelligence layer: ingest (files/URL/RSS/SQL) → Claude entity
  extraction → evolving ontology → graph → NL→Cypher round-trip (generate → validate-against-schema
  → execute → synthesize). Extractors are engine-agnostic; the Cypher dialect/schema/executor are
  Kinetica-bound.
- **`falkor`** — the plumbing: multi-source read (Kinetica SQL, or files/DB/S3 via DuckDB, or
  app-read) → multi-engine load (FalkorDB now; Kinetica; PuppyGraph pending) → DuckDB hydration.

The missing piece is not features — it is a small set of **contracts** that let these compose, plus
a **gateway** that hosts them. `xgraph` provides both.

### Relationship to the existing projects

- `explorer` is **carried over** into `xgraph`'s frontend and reshaped for neutral engine choices.
  The original `explorer/` is **left untouched** as the stable Kinetica-only tool. When `xgraph`
  matures it absorbs explorer's functionality and makes it obsolete — but not before then.
- `falkor` and `kgr` remain independent upstream repos; `xgraph` **imports them as libraries**
  behind its contracts rather than reimplementing them.

## 2. Runtime topology: thin backend gateway

`explorer` is serverless (browser → Kinetica REST). That model cannot hold for `xgraph`, because:

- **FalkorDB speaks RESP (Redis protocol) on `:6379` — a browser cannot reach it directly.**
- DuckDB reads (files/S3/DB), LLM API keys (NL→Cypher), and RSS/document scraping (CORS) must all
  run server-side.

So `xgraph` = a **FastAPI/Python gateway** hosting the contracts, plus the carried-over `explorer`
frontend talking to it over one uniform HTTP/JSON API. Python is chosen because `falkor` and `kgr`
are Python; their code becomes libraries the gateway imports directly.

```
 ┌────────────────────────── xgraph ──────────────────────────┐
 │  frontend/ (explorer carried over, neutralized)             │
 │    renderers kept · useKineticaApi ─► useGatewayApi         │
 │                        │ uniform HTTP/JSON                  │
 │  backend/ (FastAPI) ◄──┘                                    │
 │                                                             │
 │   GraphEngineAdapter  ── FalkorDB(S1) · Kinetica · Puppy    │
 │      (traversal + schema)                                   │
 │                                                             │
 │   ┌── DuckDB Compute layer (embedded, engine-neutral) ──┐   │
 │   │   ① Extract  → files/CSV/Parquet/S3/DB              │   │
 │   │   ② Hydrate  → join wide cols onto graph results    │   │
 │   │                by id, post-traversal                │   │
 │   │   ③ OLAP     → window/ROLLUP/joins over files +     │   │
 │   │                results (the Kinetica-OLAP job)      │   │
 │   └──────────────────────────────────────────────────────┘ │
 │                                                             │
 │   SourceReader  ── duckdb (①) │ app-read │ scraper          │
 │   NLCypher service (S3)                                     │
 └─────────────────────────────────────────────────────────────┘
   imports: falkor/ (duckdb route + hydration), kgr/ (extract, NL→Cypher)
   leaves untouched: explorer/ (Kinetica-only)
```

The HTTP endpoints are **xgraph's own API**, not any vendor's. The gateway's adapter layer
translates each call into the engine's native protocol (FalkorDB RESP/Cypher, Kinetica REST,
PuppyGraph openCypher, DuckDB SQL). The frontend only ever speaks xgraph's API.

**Validation principle — Kinetica is a switchable route from S1 (not deferred to S4).** At every
slice the FalkorDB/DuckDB pipeline is validated against Kinetica ground truth by running the
equivalent query on both engines through the one gateway and comparing (the build-both-and-compare
method proven in the `falkor` verification). So `engine=kinetica` is registered and usable from the
first slice; the S1 Kinetica adapter implements `list_graphs` + `run_query` (the validation path),
while rich Kinetica visualization parity (entity fetch, server-side ontology DOT) remains S4.

## 3. The three S0 contracts

DuckDB is a first-class **compute layer**, not merely a source mechanism. It wears three hats and is
**engine-neutral** — it does not care which graph engine produced a result. One embedded DuckDB
instance in the gateway backs all three roles.

1. **`GraphEngineAdapter`** — graph traversal + schema. Read side (needed for S1):
   - `list_graphs() -> [name]`
   - `get_schema(graph) -> {labels, rel_types, dot}`
   - `run_query(graph, cypher, timeout?) -> {columns, rows}`
   - `fetch_entities(graph, limit) -> {nodes, edges}`
   - `get_record(graph, id) -> {…}`
   - Write side `load_graph(spec, source)` is **declared now, implemented in S2**.
   - FalkorDB is the S1 reference implementation.

2. **`ComputeEngine`** — OLAP + hydration, DuckDB implementation. Lifts `falkor`'s `hydrate` /
   `run_hydrated` plus a raw-SQL method:
   - `hydrate(rows, source, key, columns|join_sql) -> enriched_rows` (role ②)
   - `run_sql(sql, sources) -> rows` (role ③ — contract now, UI in S2)
   - Reused directly from `falkor`; already unit-tested there.

3. **`SourceReader`** — ingestion. `read(spec) -> rows|text`, with selectable mechanism:
   - `duckdb` (files/CSV/Parquet/S3/DB) — backed by the same embedded DuckDB (role ①); reuses
     `falkor.duckdb_source`.
   - `app-read`, `scraper` — declared now, implemented in S2+.

## 4. Components (backend)

Each unit has one purpose and is independently testable.

- `gateway/app.py`, `gateway/config.py` — FastAPI wiring; engine/source registry; endpoints and
  credentials from `.env` (same pattern as `falkor`).
- `adapters/graph/base.py` — `GraphEngineAdapter` ABC.
- `adapters/graph/falkordb.py` — FalkorDB implementation; reuses `falkor`'s connection. `get_schema`
  builds a schema DOT **client-side** from `db.labels()` / relationship-types / property-keys
  (FalkorDB has no server-side ontology DOT like Kinetica — this is the core neutralization work of
  S1).
- `compute/duckdb_engine.py` — `ComputeEngine`; one embedded DuckDB; lifts `falkor.hydrate` /
  `run_hydrated` + `run_sql`.
- `sources/base.py` — `SourceReader` ABC; `sources/duckdb_source.py` reuses `falkor.duckdb_source`;
  `app-read`/`scraper` stubbed.

## 5. Gateway HTTP surface

```
GET  /engines                                 → available graph engines + sources
GET  /graphs?engine=falkordb                  → listGraphs
GET  /schema?engine=&graph=                   → {labels, rel_types, dot}
POST /query   {engine,graph,cypher,timeout?}  → {columns, rows}      (traversal)
GET  /entities?engine=&graph=&limit=          → {nodes, edges}
GET  /record?engine=&graph=&id=               → node/edge detail
POST /hydrate {rows|query_ref, source, key,   → enriched rows        (ComputeEngine ②)
               columns|join_sql}
POST /sql     {sql, sources}                  → rows                 (ComputeEngine ③; UI in S2)
```

GET for idempotent reads; POST for calls carrying a Cypher/SQL body. This is a REST convention, not
a semantic distinction.

For `/hydrate` in S1 the request uses `rows` (the NODE ids returned by a prior `/query`) plus a
`columns` list — mapping to `falkor`'s `hydrate()`. The `query_ref` (server-side reference to a prior
result) and `join_sql` (arbitrary post-join SQL, mapping to `falkor`'s `run_hydrated()`) forms are
declared for later slices and are not required for S1.

## 6. Frontend changes

- Replace `useKineticaApi` with `useGatewayApi` — same return shapes (`{columns, rows}`,
  `{nodes, edges}`), pointed at the gateway base URL.
- All renderers (`CanvasGraph`, `DeckMapView`, `OntologyViewer`, `LabelChart`) are unchanged.
- Add one **Hydrate** affordance in the results / node-detail strip that calls `POST /hydrate`.
- Keep explorer's existing single-file React/CDN structure for now (a modernization slice can be
  scheduled before S2/S3 UI growth); do not rebuild on a new toolchain in S1.

## 7. S1 data flow (the demo)

1. Frontend → `GET /graphs?engine=falkordb` → sees `banking_graph` (live from prior `falkor`
   verification work).
2. User runs Cypher in a QueryPanel → `POST /query` → gateway → `FalkorDBAdapter.run_query` →
   `{columns, rows}` → frontend renders table + force-graph node-link.
3. User clicks **Hydrate** → `POST /hydrate {rows: NODE ids from step 2, source:
   data/vertexes.parquet, key: NODE, columns:[…]}` → `ComputeEngine`(DuckDB) joins wide columns →
   enriched rows → frontend shows never-ingested columns (e.g. `bank:bank_number`, `full_address`)
   in the node-detail panel.

Net: the same explorer UI, now showing a **FalkorDB** graph, a Cypher result, and DuckDB-hydrated
attributes that were never loaded into the graph — through one gateway, no Kinetica.

## 8. Error handling

Uniform JSON envelope: `{error: {code, message, engine, detail}}`.

- Engine unreachable → HTTP 502.
- Query timeout (observed live with FalkorDB on large graphs) → HTTP 504 with the timeout value;
  support a per-request `timeout` parameter.
- Bad Cypher → HTTP 400 with the engine's message.
- Hydrate with null/missing key → reuse `falkor`'s existing drop logic.
- Missing engine configuration → fail fast at gateway startup.
- The frontend surfaces these in the QueryPanel's existing error area.

## 9. Testing

Mirrors `falkor`'s philosophy: pure/unit tests need no services; integration tests SKIP (not fail)
if the engine is unreachable.

- `ComputeEngine`/DuckDB — unit tests read tmp Parquet in-process, no services (like `falkor`'s
  `test_hydrate`).
- `FalkorDBAdapter` — unit tests with a mock client for parsing (schema→DOT, result shaping);
  integration tests hit live FalkorDB and SKIP if unreachable.
- Gateway — FastAPI `TestClient` with adapters injected/mocked: assert request → adapter call →
  response envelope; no live engine required.
- Frontend — keep explorer's `tests/`; add a smoke test that `useGatewayApi` calls the right
  endpoints (mocked `fetch`).
- End-to-end — a `verify`-style manual drive of the UI against live FalkorDB + the Parquet.

## 10. Repo layout

```
xgraph/
  backend/
    gateway/{app,config}.py
    adapters/graph/{base,falkordb}.py     # kinetica, puppygraph added in S4
    compute/duckdb_engine.py
    sources/{base,duckdb_source}.py       # appread, scraper added in S2+
    tests/
    requirements.txt
  frontend/
    XGraph.html                           # explorer carried over, neutralized
    tests/
  data/                                   # gitignored — local Parquet for hydration demo
  docs/superpowers/specs/
  README.md
  CLAUDE.md
```

## 11. S1 acceptance criteria

1. `GET /graphs?engine=falkordb` returns `banking_graph`.
2. `GET /schema?engine=falkordb&graph=banking_graph` returns labels/rel-types and a DOT string that
   `OntologyViewer` renders.
3. `POST /query` with a bank→wire traversal returns `{columns, rows}`; frontend renders table +
   force-graph node-link.
4. `POST /hydrate` with NODE ids from criterion 3 + `data/vertexes.parquet` returns enriched rows
   including a column never in the graph (`bank:bank_number`); frontend shows it in node-detail.
5. The carried-over explorer UI renders criteria 1–4 against **FalkorDB**, with no Kinetica involved.
6. Backend unit tests pass with no services; FalkorDB + gateway integration tests pass against live
   FalkorDB (SKIP if down).
7. The original `explorer/` is untouched.

## 12. Later slices (context only — not built in this spec)

Kept in view so the S0 contracts do not need reshaping later:

- **S2 — Ingestion → graph.** Wire `SourceReader`s (files/DB/RSS/documents) + `ComputeEngine.run_sql`
  into a "build graph" action, engine-selectable; implements the `GraphEngineAdapter` write side and
  the DuckDB OLAP UI.
- **S3 — NL→Cypher round-trip (north star).** Generalize `kgr.qa` behind the adapter (dialect +
  schema + executor), expose in the UI, add client-side WASM path viz. **This end state must keep
  the file/DB/S3-via-DuckDB reads — the NL→Cypher route does not exclude multi-source ingestion.**
- **S4 — Engine breadth.** Full Kinetica visualization parity (entity fetch + server-side ontology
  DOT, reusing explorer's REST) and PuppyGraph (depends on the separately-tracked PuppyGraph/Iceberg
  evaluation). Note: Kinetica as a *validation* route (`list_graphs` + `run_query`) already lands in
  S1 per the validation principle in §2 — S4 only adds the richer viz surface.

## 13. Non-goals for S0+S1

- No ingestion UI, no NL→Cypher, no scraping, no OLAP UI (contracts declared, not surfaced).
- No Kinetica or PuppyGraph adapter yet.
- No frontend toolchain migration (single-file React/CDN retained).
- No changes to `explorer/`, `falkor/`, or `kgr/` beyond importing the latter two as libraries.
