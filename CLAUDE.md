# CLAUDE.md

This file guides Claude Code (claude.ai/code) when working in the `xgraph` project.

## What this repo is

`xgraph` is a **vendor-neutral graph workbench** — one UI and one HTTP API over multiple graph
engines. It began by unifying three sibling projects under `graph/` and is now **self-contained**
(no runtime dependency on any of them):

- **`explorer`** — a client-side React viz app (force-graph, deck.gl/MapLibre, Graphviz-WASM,
  Chart.js), Kinetica-only. Its frontend was **carried over and neutralized** into `xgraph/frontend`.
  The original `explorer/` is untouched as the Kinetica baseline until xgraph supersedes it.
- **`kgr` / graphrag** — only its small `_llm` backend was needed; it is **extracted** into
  `backend/xgraph_gateway/llm.py` (Claude via `claude` CLI / Anthropic SDK). No import of `kgr`.
- **`falkor`** — its `graph_loader` package (multi-source read, multi-engine load, DuckDB hydration)
  is **vendored** into `backend/graph_loader/`. No import from `../falkor`. Tradeoff: the vendored
  copy is a fork that can drift from falkor's upstream.

The shape is a **FastAPI/Python gateway** (`backend/`) that hosts the contracts, plus the
carried-over frontend (`frontend/`) talking to it over xgraph's own uniform HTTP/JSON API. A backend
is required because FalkorDB speaks RESP (no browser access) and DuckDB/S3/LLM/scraping must run
server-side.

## Status (2026-07-14)

- **`backend/`: built and verified** — 150 tests pass (unit + live-skipping integration: FalkorDB
  query→hydrate, FalkorDB↔Kinetica parity, Kinetica ontology/entity load, Create, and the Explain
  post-join banking use case). Endpoints: `/engines /connect /graphs /graph_sizes /schema /query
  /entities /record /create /ask /nl2cypher /synthesize /explain /hydrate /sql`.
- **`frontend/`: built and live-verified** — the single-file `XGraph.html` action bar
  (Setup · Connect · Create · List · Load · Ask · Query · Explain · Visualize · Ontology) works
  end to end against the gateway; `gateway.js` client + transforms Node-tested.
- **Explain post-join (2026-07-14): done** — a focus that names a wide attribute (e.g. `party_name`)
  triggers NL→SQL (`nlcypher.generate_join_sql`) → read-only DuckDB post-join over the hydrate
  Parquet (`compute.run_join`) → aggregated table → `synthesize`. Auto-triggers when a focus +
  hydrate source are present; falls back to the plain semantic answer otherwise. Endpoint `/explain`,
  panel surfaces the generated SQL + ranked table.
- **`README.md`** documents the project and features the Explain post-join use case (screenshots in
  `docs/images/`).
- Design spec and implementation plans live in `docs/superpowers/specs/` and
  `docs/superpowers/plans/` — these are **local and uncommitted** (see the no-commit rule below), so
  read them from disk, not git.

## IMPORTANT constraints

- **Do NOT `git commit` anything under `xgraph/`.** The project is developed locally until the user
  explicitly says to commit. Write files freely; never stage/commit them.
- The backend has its **own virtualenv** (`backend/.venv`) and `requirements.txt` (fastapi, uvicorn,
  httpx, duckdb, falkordb, gpudb, typeguard, pyyaml, python-dotenv, pytest). No longer reuses
  falkor's venv.
- **Self-contained:** `graph_loader` is vendored at `backend/graph_loader/`; the LLM backend is
  `backend/xgraph_gateway/llm.py`. Do NOT reintroduce `sys.path` hacks or imports from `../falkor` /
  `../graphrag`. If you fix a bug in vendored `graph_loader`, note that it has diverged from falkor.
- Demo data lives zipped under `data/` (`*.parquet.zip`, tracked); `./scripts/unzip-data.sh` extracts
  the working `data/*.parquet` (git-ignored). The gateway resolves bare source names against
  `XGRAPH_DATA_DIR` (default = repo `data/`) via `config.resolve_data_path`.

## Commands

```bash
# One-time: unzip demo data + create the backend venv
./scripts/unzip-data.sh
cd backend && python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt

# Backend tests (own venv; run from backend/) — 153 tests
cd backend && ./.venv/bin/python -m pytest tests/ -v
# Live tests (FalkorDB query→hydrate, FalkorDB↔Kinetica parity, Explain post-join) self-resolve the
# Parquet; they SKIP if FalkorDB/Kinetica are unreachable or the Parquet is absent.

# Run the gateway (:8090 — :8088 is taken by Kinetica Graph)
cd backend && ./.venv/bin/uvicorn xgraph_gateway.app:app --port 8090
curl -s 'localhost:8090/graphs?engine=falkordb'

# Frontend: pure-JS unit tests (gateway.js client + transforms)
cd frontend && node tests/test_transforms.mjs && node tests/test_client.mjs
# Open the app — no server needed: open frontend/XGraph.html directly (file://).
# It special-cases file:// (skips the local docs HEAD probe); CDN libs need internet;
# gateway.js must sit beside XGraph.html. Optional served page:
cd frontend && python3 -m http.server 8099   # open http://localhost:8099/XGraph.html
```

## Architecture

The frontend speaks **only** xgraph's HTTP API; the gateway's adapter layer translates each call
into the engine's native protocol (FalkorDB RESP/Cypher, Kinetica REST, DuckDB SQL). Three contracts
live in `backend/xgraph_gateway/`:

1. **`GraphEngineAdapter`** (`adapters/base.py`) — graph traversal + schema:
   `list_graphs()`, `get_schema(graph)→{labels,rel_types,dot}`, `run_query(graph,cypher,timeout=60000)
   →{columns,rows}`, `fetch_entities(graph,limit)→{nodes,edges}`, `get_record(graph,id)`.
   Implementations: `falkordb_adapter.py` (S1 reference), `kinetica_adapter.py` (validation),
   `fake.py` (tests). Write side (`load_graph`) is declared for S2, not built.
2. **`ComputeEngine`** (`compute/duckdb_engine.py`) — one embedded DuckDB, **engine-neutral**, three
   roles: extract, **hydrate** (`hydrate(rows, source, key, columns)`, delegates to
   `graph_loader.hydrate`), and **OLAP** (`run_sql`). This is where wide attribute columns are joined
   onto graph results *after* a traversal, keeping the graph skinny.
3. **`SourceReader`** (`sources/base.py`) — ingestion; `duckdb` mechanism reuses
   `graph_loader.duckdb_source` (S2 uses this).

`registry.py` maps `engine=fake|falkordb|kinetica` → an adapter. `app.py` exposes the HTTP surface
(GET `/engines /graphs /schema /entities /record`; POST `/query /hydrate /sql`) with a uniform error
envelope `{"error":{code,message,engine,detail}}` and status 400 (bad query) / 502 (unreachable) /
504 (timeout). Adapters connect lazily (inside `get_adapter`), so importing the app opens no
connection.

**Kinetica is a first-class validation route from S1** (not deferred): every slice validates the
FalkorDB/DuckDB pipeline against Kinetica ground truth by running the equivalent query on both
engines through the one gateway and comparing (the build-both-and-compare method from the `falkor`
verification). Full Kinetica *visualization* parity is S4.

### Frontend (`frontend/`)

Carried-over `explorer` as a **single HTML file** (`XGraph.html`) — React 18 UMD + Babel-standalone
via CDN, **no build step**. Neutralization strategy: the gateway returns clean shapes; a small UMD
module `gateway.js` (loaded via `<script>`, also `require`-able in Node for tests) holds the gateway
**client** (`makeClient(base, engine)`) and **pure transforms** that convert the clean shapes into
the exact objects explorer's renderers already consume — so the renderers stay untouched:

- `tableFromGateway({columns,rows})` → `{headers, datatypes, rows}` (Results tab).
- `graphTableFromGateway({nodes,edges})` → the `graphTableData` shape `CanvasGraph` consumes
  (`{nodes:{records:[{NODE_NAME,NODE_LABEL}]}, edges:{records:[{NODE1_NAME,NODE2_NAME,EDGE_LABEL}]}}`).
- `recordFromGateway({id,label,props})` → flat `{NODE, LABEL, ...props}` (node-detail).

The old `useKineticaApi` is replaced by a gateway client; the Sidebar's URL/user/pass profiles become
an **engine picker** + gateway-URL field. `GATEWAY_BASE` and `HYDRATE_SOURCE` (an **absolute** path,
resolved server-side by the gateway) are consts near the top of `XGraph.html`. Kinetica-only features
(WMS tiles, Create/Solve/Match grammar helpers, geo MapView) are gated behind `engine==='kinetica'`,
not deleted (their neutralization is S4).

## Slices

S0 contracts → **S1 neutral viz on FalkorDB incl. a DuckDB hydration pass** (current) → S2
ingestion→graph (write side + OLAP UI) → **S3 NL→Cypher round-trip (north star; must keep
multi-source DuckDB reads)** → S4 engine breadth (full Kinetica viz + PuppyGraph, the latter pending a
PuppyGraph/Iceberg evaluation).

## Testing

Mirrors `falkor`'s philosophy: pure/unit tests need no services; integration tests **SKIP** (not
fail) when the engine is unreachable. Backend: pytest with a `FakeAdapter` injected via
`create_app(adapter_factory=...)` for gateway tests; live FalkorDB/Kinetica tests skip if down.
Frontend: `gateway.js` transforms and client are Node-tested (`tests/*.mjs`) with an injected fake
`fetch`. The React app itself **cannot be runtime-verified headlessly** — the syntax check is running
the `<script type="text/babel">` block through `@babel/standalone` in Node; true acceptance is
browser-driven (the user drives it).

## Gotchas / non-obvious constraints

- **No commits under `xgraph/`** (above). Everything is local until the user says otherwise.
- **DuckDB returns DECIMAL columns as Python `Decimal`** — coerce to `float` before handing to the
  FalkorDB client. `graph_loader.duckdb_source.coerce_row` (reused) does this.
- **Never put `ORDER BY` in paged Kinetica SQL** — `graph_loader.kinetica_source` offset-paging over
  an `ORDER BY` result duplicates and drops rows while the count still looks right (documented in
  `falkor`'s CLAUDE.md). Read unsorted; sort downstream in DuckDB if needed.
- **`requirements.txt` is standalone** — a clean `python3 -m venv .venv && ./.venv/bin/pip install -r
  requirements.txt` gives a working backend (fastapi, uvicorn, httpx, duckdb, falkordb, gpudb,
  typeguard, pyyaml, python-dotenv, pytest). `gpudb` is a hard import (adapters load at app import),
  so it must be installed even for FalkorDB-only use.
- **Frontend edits to the 8,900-line `XGraph.html`** are anchored search-and-replace against verbatim
  code strings (line numbers shift); validate with the Babel transpile + a `curl` 200, and defer real
  behavior checks to the browser.

## Deferred (do not treat as gaps in S1)

`_dot_from_triples` doesn't escape quotes in labels; `get_record` returns `{}` for not-found; `/sql`
accepts but ignores `sources`; per-engine adapter caching (a fresh adapter/connection is built per
request); `load_graph` write side; frontend session-restore still passes the old `{url,user,pass}`
shape; per-query hop-subgraph rendering (a Kinetica-GQL artifact the gateway doesn't synthesize — the
Visualization tab renders the graph browse instead).
