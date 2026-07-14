# xgraph S1.5 — Engine-neutral connection (graph engine × OLAP engine, UI-driven)

> **For agentic workers:** executed via superpowers:subagent-driven-development, no-commit (local only). Steps use `- [ ]`.

**Goal:** Let the user independently pick a **graph engine** (`falkordb` | `kinetica`) and an **OLAP/ingest engine** (`duckdb` | `kinetica`), each with its own connection (host/port/password), from the UI — not `.env`. Yields the three combos: FalkorDB+DuckDB (open), FalkorDB+Kinetica (hybrid), Kinetica+Kinetica (native), plus Kinetica+DuckDB.

**Architecture:** The two axes are the two existing contracts — `GraphEngineAdapter` (graph) and `ComputeEngine` (OLAP/hydration). Connection is **session-based**: `POST /connect` stores the chosen engines + connections server-side (in-memory), builds+caches the adapters, and returns a `session` id; other endpoints take `session`. Backward-compatible: when no `session` is passed, endpoints fall back to the current `engine=` + `.env` behavior, so all existing tests keep passing.

**Tech stack:** unchanged (FastAPI backend on falkor's venv; single-file frontend + gateway.js).

## Global Constraints

- **No git commit under `xgraph/`** — local only; "checkpoint" = verify locally.
- Backend runs on falkor's venv; gateway port **8090**.
- Connection shapes: graph `falkordb` = `{host, port, password}`; graph/compute `kinetica` = `{url, user, password}`; compute `duckdb` = `{}` (embedded — the wide-source path is passed per `/hydrate` and `/sql` call, not at connect).
- Passwords must never go into a URL/query string — connection travels only in the `POST /connect` body.
- Preserve backward compatibility: endpoints without a `session` behave exactly as today (`engine=` + `.env`), keeping the existing backend tests green.

---

### Task 1: Adapters + compute accept an explicit connection; add KineticaComputeEngine

**Files:** modify `adapters/falkordb_adapter.py`, `adapters/kinetica_adapter.py`, `compute/duckdb_engine.py`; create `compute/kinetica_engine.py`; modify `registry.py`. Tests: `tests/test_conn_construction.py`, `tests/test_kinetica_compute.py`.

**Interfaces (produce):**
- `FalkorDBAdapter(settings=None, conn=None)` — when `conn={host,port,password}` is given, connect to that; else fall back to `settings`/`.env`.
- `KineticaAdapter(settings=None, conn=None)` — `conn={url,user,password}` overrides; else `.env`.
- `compute/kinetica_engine.py::KineticaComputeEngine(conn)` implementing the `ComputeEngine` shape: `hydrate(rows, source, key="NODE", columns="*") -> list[dict]` runs a Kinetica-side join — `SELECT <columns> FROM <source> WHERE <key> IN (<ids>)` via `graph_loader.kinetica_source.KineticaSource` — merging wide columns onto `rows`; `run_sql(sql) -> list[dict]`. `source` here is a Kinetica **table name** (e.g. `expero.vertexes`), not a Parquet path.
- `registry.get_adapter(engine, conn=None)` and `registry.get_compute(engine, conn=None)` (`duckdb` → `DuckDBComputeEngine` embedded, ignores conn; `kinetica` → `KineticaComputeEngine(conn)`).
- `DuckDBComputeEngine` = the existing `compute/duckdb_engine.py::ComputeEngine` (rename the class to `DuckDBComputeEngine`, keep a `ComputeEngine = DuckDBComputeEngine` alias so existing imports/tests don't break).

- [ ] Step 1 — Failing test: `KineticaComputeEngine.hydrate` merges wide columns by key (unit-level with a fake KineticaSource that returns canned rows for an `IN (...)` query); `_ids_sql_list` safely renders the id list. Also test `registry.get_compute("duckdb")` returns a DuckDB engine and `get_compute("kinetica", conn)` a Kinetica one.
- [ ] Step 2 — Run → fails (module/classes absent).
- [ ] Step 3 — Implement: `KineticaComputeEngine` (build `KineticaSource` from conn; `hydrate` collects `ids=[r[key] for r in rows if r.get(key) is not None]`, runs `SELECT {columns} FROM {source} WHERE {key} IN (<quoted ids>)` — reuse falkor's `safe_ident` for key/source identifier safety and parameter-free IN list built from escaped string literals; coerce Decimal→float via `coerce_row`; merge by key onto `rows`). Adapters gain the `conn` kwarg (dict → connect params). `registry` resolvers. Rename `ComputeEngine`→`DuckDBComputeEngine` + alias.
- [ ] Step 4 — Run tests green; run FULL suite (existing 20 still pass; the `ComputeEngine` alias keeps `app.py`/tests working).
- [ ] Step 5 — Checkpoint (no commit).

### Task 2: Session store + `/connect`, threaded through endpoints (backward-compatible)

**Files:** modify `gateway/app.py`; create `gateway/sessions.py`. Test: `tests/test_sessions.py` (extend `tests/test_app.py` coverage).

**Interfaces (produce):**
- `sessions.py`: an in-memory `SessionStore` — `create(graph_engine, graph_conn, compute_engine, compute_conn) -> session_id` (builds + caches `get_adapter(graph_engine, graph_conn)` and `get_compute(compute_engine, compute_conn)`); `get(session_id) -> {"adapter", "compute", "graph_engine", "compute_engine"}`; raises `KeyError` for unknown ids. Session id is a short opaque token (derive it deterministically from a monotonic counter — NOT `random`/`uuid` at import; a per-store incrementing int rendered as a string is fine).
- `POST /connect` body `{graph:{engine,conn}, compute:{engine,conn}}` → `{"session": id, "graphs": [...] }` (also returns the graph list so the UI does one round-trip). Errors use the standard envelope.
- Every other endpoint resolves its adapter/compute from `session` when present, else from `engine=` (+`.env`) as today:
  - reads accept `?session=`; `POST` bodies accept `"session"`.
  - `/query,/schema,/graphs,/entities,/record` → session's **graph adapter** (fall back to `adapter_factory(engine)`).
  - `/hydrate,/sql` → session's **compute engine** (fall back to the injected default DuckDB `compute`).
- `create_app(adapter_factory=..., compute=..., store=None)` — inject a `SessionStore` for tests (default builds one using `registry`).

- [ ] Step 1 — Failing TestClient tests: `POST /connect` with a fake graph+compute store returns a session + graphs; a subsequent `POST /query {session, graph, cypher}` routes to that session's adapter; `/hydrate {session,...}` routes to the session's compute; and the pre-existing `engine=`/no-session tests still pass (back-compat).
- [ ] Step 2 — Run → fails.
- [ ] Step 3 — Implement `SessionStore` + `/connect` + per-endpoint session-or-engine resolution helper (`_resolve_adapter(payload/params)`, `_resolve_compute(...)`).
- [ ] Step 4 — Run FULL suite green (new session tests + all existing).
- [ ] Step 5 — Checkpoint.

### Task 3: Live cross-combo verification

**Files:** `tests/test_e2e_combos_live.py`. SKIP if services down.

**Interfaces (consume):** live FalkorDB (`banking_graph`), live Kinetica (`expero.vertexes`).

- [ ] Step 1 — Tests (skip-guarded):
  - **Hybrid (FalkorDB graph + Kinetica OLAP):** `POST /connect {graph:{engine:"falkordb",conn:{...from env...}}, compute:{engine:"kinetica",conn:{...}}}`; run a `MATCH (b:bank) RETURN b.NODE AS NODE LIMIT 5`; `POST /hydrate {session, rows, source:"expero.vertexes", key:"NODE", columns:'id, "bank:bank_number"'}` → rows include `bank:bank_number` (hydrated from **Kinetica**, not DuckDB).
  - **Native (Kinetica + Kinetica):** connect both to kinetica; `/graphs` lists Kinetica graphs; a Kinetica count query via `/query` returns a row.
  - **Open (FalkorDB + DuckDB):** the existing S1 path still works via a session too (hydrate from the Parquet).
- [ ] Step 2 — Run against live services; PASS or SKIP. Report the hydrated value provenance.
- [ ] Step 3 — Checkpoint.

### Task 4: gateway.js — session-aware client

**Files:** modify `frontend/gateway.js`; extend `frontend/tests/test_client.mjs`.

**Interfaces (produce):**
- `makeClient(base, fetchImpl)` gains `connect(graph, compute)` → `POST /connect`, stores the returned `session` on the client, returns `{session, graphs}`. All existing methods (`listGraphs/getSchema/runQuery/fetchEntities/getRecord/hydrate`) include the stored `session` (query param for GET, body field for POST) when present; `hydrate(rows, source, key, columns)` unchanged in signature (session added internally).
- Keep the current `makeClient(base, engine)` call form working (engine optional) so nothing breaks before the UI switches to `connect()`.

- [ ] Step 1 — Failing Node test (injected fake fetch): `connect({engine:'falkordb',conn:{...}},{engine:'duckdb',conn:{}})` posts to `/connect`, stores session `s1`; a subsequent `runQuery`/`hydrate` includes `session:'s1'`. `transforms` test still passes.
- [ ] Step 2 — Run → fails.
- [ ] Step 3 — Implement.
- [ ] Step 4 — `node tests/test_client.mjs && node tests/test_transforms.mjs` green.
- [ ] Step 5 — Checkpoint.

### Task 5: Connection UI — two independent radio groups + conditional connection fields

**Files:** modify `frontend/XGraph.html` (Sidebar connection panel + App connect handler).

**Interfaces (consume):** `gwClient.connect(graph, compute)`.

- [ ] Step 1 — Replace the single engine `<select>` with two radio groups in the Sidebar:
  - **GRAPH ENGINE:** ( ) Kinetica ( ) FalkorDB (default FalkorDB).
  - **OLAP / INGEST:** ( ) Kinetica ( ) DuckDB (default DuckDB).
  Below each, render connection fields **conditionally** on the selection:
  - FalkorDB → `host` / `port` / `password` (defaults `localhost` / `6379` / blank).
  - Kinetica (graph or OLAP) → `url` / `user` / `password` (defaults from the gateway URL context; blanks OK).
  - DuckDB → no connection fields; show "(embedded)". The wide-source path stays `HYDRATE_SOURCE` for now (a field can come later).
  Keep the "Gateway URL" field (where the gateway itself lives, default `http://localhost:8090`).
- [ ] Step 2 — On **Connect & List**: build `graph={engine, conn}` and `compute={engine, conn}` from the radios+fields and call `gwClient.connect(graph, compute)`; populate the graph list from the response. Set App state `engine`/`computeEngine` so the existing Kinetica-only gating (`engine === 'kinetica'`) still keys off the chosen **graph** engine.
- [ ] Step 3 — Hydrate button: pass the correct `HYDRATE_SOURCE` — a Parquet path when compute=duckdb, or the Kinetica table `expero.vertexes` when compute=kinetica. (Derive from the selected compute engine; a constant map is fine for S1.5.)
- [ ] Step 4 — Verify headlessly: grep the radio groups + `gwClient.connect`; run the `<script type="text/babel">` block through `@babel/standalone` in Node (PASS); serve + curl 200.
- [ ] Step 5 — Checkpoint.

### Task 6: Browser acceptance (USER-DRIVEN)

Update `frontend/tests/VERIFY.md` with the three-combo connection flow and hand off. Steps: pick each combo, Connect, confirm graph list + query + hydrate; for the hybrid combo confirm the hydrated column comes back (from Kinetica). Kinetica-only UI features gate off the **graph** engine selection.

## Self-Review
- Two axes → two contracts (GraphEngineAdapter × ComputeEngine): Tasks 1–2. ✓
- Per-engine host/port/password from UI, no creds in URL: session model (Task 2), UI fields (Task 5). ✓
- Kinetica as OLAP engine (hybrid combo): `KineticaComputeEngine` (Task 1), live proof (Task 3). ✓
- Backward compatibility (existing 20 tests): additive session resolution + `ComputeEngine` alias (Tasks 1–2). ✓
- No placeholders: interface contracts are explicit; implementers (capable model) fill code to contract with TDD.
