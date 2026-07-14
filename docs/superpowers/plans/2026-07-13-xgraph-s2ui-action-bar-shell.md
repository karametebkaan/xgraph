# xgraph S2-UI — Action-bar shell rebuild (A)

> Executed via superpowers:subagent-driven-development, no-commit (local only). This is chunk (A); the LLM Interact backend+panels are chunk (B/S3), a later slice.

**Goal:** Replace `XGraph.html`'s left-sidebar shell with a **horizontal action bar** — `Setup · Connect · List · Load · Interact① · Query · Interact② · Visualize · Ontology` — where each action owns the full panel beneath it. Wire the seven non-LLM actions to the existing gateway; Setup carries the three engine axes (graph × OLAP × LLM). The two Interact tabs are present but **stubbed** ("LLM step — wired in the next slice").

**Architecture:** Add an `activeAction` state + an `ActionBar` component + a content router that mounts the **existing** renderers (QueryPanel, CanvasGraph, OntologyViewer, LabelChart) per action. Do NOT rewrite the renderers — only the outer shell and the Setup panel change. Connection uses `gwClient.connect(graph, compute, llm)` (session model from S1.5). Ontology is derived live via `getSchema` and shown both at Load and under its own action.

**Tech stack:** unchanged (single-file React 18 + Babel-standalone, no build; `gateway.js`). Gateway on :8090.

## Global Constraints
- **No git commit under `xgraph/`** — local only.
- Only edit `frontend/XGraph.html` (and `frontend/tests/VERIFY.md` in the last task). Reuse `gateway.js` as-is (it already has `connect`, `listGraphs`, `getSchema`, `runQuery`, `fetchEntities`, `getRecord`, `hydrate`).
- **Reuse existing components** (QueryPanel, CanvasGraph, OntologyViewer, LabelChart) — mount them under actions; do not rewrite them.
- After every task: validate by transpiling the `<script type="text/babel">` block through `@babel/standalone` in Node (as prior tasks did) and `curl` the served page for HTTP 200. Runtime/visual correctness is the user's browser acceptance (last task).
- Keep the Kinetica-only gating (`engine==='kinetica'`) working — it now keys off the **graph** engine chosen in Setup.
- Branding stays **xGraph** (already applied); don't reintroduce "Kinetica"/"Graph Explorer" chrome.

## Actions & reachability
| Action | Panel | Reachable when |
|---|---|---|
| Setup | 3-axis selector (below) | always |
| Connect | `connect()` + status + graph count | always |
| List | graph list; pick active graph | connected (session set) |
| Load | fetch entities + schema; show counts + live ontology | a graph is selected |
| Interact① | **stub**: "English→Cypher (LLM) — next slice" | loaded |
| Query | Cypher editor + Results table (existing QueryPanel) | loaded |
| Interact② | **stub**: "Results→English (LLM) — next slice" | loaded |
| Visualize | force-graph canvas (existing CanvasGraph) | loaded |
| Ontology | OntologyViewer + LabelChart (live-derived) | loaded |

---

### Task 1: Shell scaffold — action bar + activeAction router
**Files:** modify `frontend/XGraph.html`.
**Interfaces (produce):** App state `activeAction` (default `"setup"`); `ACTIONS` array of `{key,label,reachable(state)}`; an `ActionBar` component (horizontal row of buttons under the xGraph wordmark, active highlighted, unreachable dimmed+disabled, "Interact" rendered twice with distinct keys `interact1`/`interact2`); a content region below that switches on `activeAction`. In this task every action renders a labeled **placeholder** div; the existing sidebar/split-pane layout is removed from the main render (its components are re-mounted in Tasks 2-4). Keep App state that the components need (graphs, activeGraph, gwClient, engine, graphTableData, dotString, labelData, query panels) intact.
- [ ] Step 1 — Add `activeAction` state + `ACTIONS` + `ActionBar`; replace the top-level layout (Sidebar + main SplitPane) with `<ActionBar/>` + a `<div className content>` that renders a placeholder per action. Preserve all existing state declarations and helper functions (they're used in later tasks); only the JSX layout changes.
- [ ] Step 2 — Reachability: dim/disable actions per the table (derive from `graphs.length`/`session`, `activeGraph`, and a `loaded` flag set true after Load). Clicking a reachable action sets `activeAction`.
- [ ] Step 3 — Validate: babel transpile PASS; serve + curl 200; grep confirms `activeAction`, `ActionBar`, all nine action keys.
- [ ] Step 4 — Checkpoint (no commit).

### Task 2: Setup + Connect panels (three-axis, engine-neutral)
**Files:** modify `frontend/XGraph.html`.
**Interfaces (consume):** `gwClient.connect(graph, compute, llm)`, `xgraphGateway.makeClient`.
**Interfaces (produce):** App state for the three axes + their conns; a `SetupPanel` (rendered under `setup`) and a `ConnectPanel` (under `connect`).
- [ ] Step 1 — SetupPanel: three labeled groups —
  - **Graph engine** radios `Kinetica | FalkorDB` (default FalkorDB) → conditional fields: FalkorDB `host/port/password` (localhost/6379/blank); Kinetica `url/user/password`.
  - **OLAP / ingest** radios `Kinetica | DuckDB` (default DuckDB) → DuckDB shows "(embedded)"; Kinetica shows `url/user/password`.
  - **LLM** button/selector `Claude` (single option, selected) with an optional `API key` field and a small note "via claude CLI / Anthropic SDK (model claude-opus-4-7)" — this mirrors kgr; the value is carried in the connect payload's `llm` block for the later LLM slice.
  - Plus the existing **Gateway URL** field (default `http://localhost:8090`).
  Reuse the existing inline-style vocabulary (no new palette).
- [ ] Step 2 — ConnectPanel (and/or a Connect button in Setup): build `graph={engine,conn}`, `compute={engine,conn}`, `llm={engine:'claude',conn:{apiKey?}}`; call `gwClient.connect(graph, compute, llm)`; on success store `session` (client does), set `graphs` from the response, set App `engine`=graph engine (so Kinetica gating keys off it), show "Connected — N graphs". On error show the envelope message inline. Advance `activeAction` to `list` on success.
- [ ] Step 3 — Validate: babel PASS; serve 200; grep the three radio groups + `gwClient.connect` + the `llm` payload block.
- [ ] Step 4 — Checkpoint.

### Task 3: List + Load panels (+ live ontology at Load)
**Files:** modify `frontend/XGraph.html`.
**Interfaces (consume):** `gwClient.listGraphs/fetchEntities/getSchema`, `xgraphGateway.graphTableFromGateway`, existing `setGraphTableData`, `setDotString`, `setLabelData`.
- [ ] Step 1 — ListPanel (under `list`): render `graphs` as a selectable list; selecting sets `activeGraph`. (If `connect` already returned graphs, use them; else `listGraphs()`.)
- [ ] Step 2 — LoadPanel (under `load`): a Load button for `activeGraph` that calls `fetchEntities(activeGraph, limit)` → `graphTableFromGateway` → `setGraphTableData`, and `getSchema(activeGraph)` → `setDotString`/`setLabelData`; set the `loaded` flag; show node/edge counts. Render the **live ontology** (OntologyViewer on `dotString`) inline in the Load panel too, so it appears at Load and re-derives on each Load/graph change.
- [ ] Step 3 — Validate: babel PASS; serve 200; grep the Load wiring + inline OntologyViewer.
- [ ] Step 4 — Checkpoint.

### Task 4: Query + Visualize + Ontology panels; Interact stubs
**Files:** modify `frontend/XGraph.html`.
**Interfaces (consume):** existing `QueryPanel`, `CanvasGraph`, `OntologyViewer`, `LabelChart` components + their required props (`gwClient`, `graphName=activeGraph`, `graphTableData`, `dotString`, `labelData`).
- [ ] Step 1 — Query panel (under `query`): mount the existing `QueryPanel` with `gwClient`+`graphName`; results/table as today. (One QueryPanel instance in the content area is fine; drop the old draggable multi-panel windowing for the action-bar layout.)
- [ ] Step 2 — Visualize (under `visualize`): mount `CanvasGraph` with `graphTableData`+`gwClient`+`graphName`. Ontology (under `ontology`): mount `OntologyViewer`(dotString) + `LabelChart`(labelData), full-size.
- [ ] Step 3 — Interact① and Interact② panels: static stub content — a titled panel reading e.g. "Interact — ask in English; the LLM writes Cypher. Wired in the next slice." (Interact②: "The LLM explains the results in English. Wired in the next slice.") No LLM calls yet.
- [ ] Step 4 — Validate: babel PASS; serve 200; grep the mounted components + the two stub panels.
- [ ] Step 5 — Checkpoint.

### Task 5: Verification + handoff
**Files:** modify `frontend/tests/VERIFY.md`.
- [ ] Step 1 — Rewrite VERIFY.md for the action-bar flow: Setup (pick engines + Claude) → Connect → List → Load (see graph counts + live ontology) → Query (run Cypher, see table) → Visualize (force-graph) → Ontology (full diagram) → the two Interact tabs show the "next slice" stubs; Kinetica-only features gate off the chosen graph engine.
- [ ] Step 2 — Run `node tests/test_client.mjs && node tests/test_transforms.mjs` (still green) and the backend suite (still green); final babel transpile PASS; serve 200.
- [ ] Step 3 — Hand off to the user for browser acceptance (can't verify headlessly).

## Self-Review
- Horizontal action bar, each action owns the panel: Task 1. ✓
- Three-axis Setup incl. Claude LLM button + connect(): Task 2. ✓
- List/Load + live ontology at Load: Task 3. ✓
- Query/Visualize/Ontology reuse existing renderers; Interact stubs: Task 4. ✓
- Reuse (not rewrite) renderers; branding stays xGraph; Kinetica gating off graph engine: Global Constraints + Tasks 1/4. ✓
- LLM Interact (real) = chunk B/S3, explicitly out of this plan (stubs only). ✓
