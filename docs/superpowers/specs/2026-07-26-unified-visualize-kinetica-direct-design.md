# Unified Visualize Page (Kinetica-Direct) — Design Spec

**Date:** 2026-07-26
**Status:** Approved (design), pending implementation plan
**Component:** `frontend/XGraph.html` (single-file React 18 UMD + Babel, no build step)

## Goal

Merge the two separate action-bar tabs — `Ontology` and `Visualization` — into
**one `Visualize` action** that co-locates the ontology graph, the force-graph
canvas, and the node/edge label donuts on a single page, faithful to the
explorer project's layout (resizable `SplitPane` splits + per-panel
maximize/restore). At the same time, make the **Kinetica** path fetch graph
entities directly from Kinetica REST (explorer's fast typed-array path) instead
of the slow gateway `/entities` paging. FalkorDB and any other gateway-served
engine keep the existing gateway fetch and render onto the *same* page.

## Motivation

- The user reported "Pull+Visualize" on Kinetica graphs
  (`extracted_graph_kinetica1`, `expero.banking_graph`) is slow: the gateway
  `/entities` endpoint returns fat JSON objects (`{id,label,props:{}}`), paged
  10k at a time up to a 100,000 cap, with an O(n²) `concat`, materializing
  ~100k×2 objects only to render ~4000. Explorer, calling Kinetica REST
  directly with typed-array decode, is far faster.
- The user wants explorer's *one-page* experience: "copy all
  ontology+Viz+label donuts of the explorer all in one page exactly into xgraph
  and remove Ontology|Visualization into one Visualize Action" — including the
  panels that "extend to max and back … just like in explorer."
- Direct user decisions captured during brainstorming:
  - Strategy = **Revive & reuse** the machinery xgraph already carries over
    (not a wholesale verbatim paste).
  - Render modes = **Force-graph + donuts + ontology** (geo/WMS/deck.gl
    deferred).
  - Engine scope for the speedup = **Kinetica-only, direct-to-Kinetica**.
  - **No "Max load" field** — explorer has none; xgraph won't either.

## Key finding: the machinery already exists

xgraph's carried-over frontend already contains everything needed to build the
merged page — this is revive-and-wire, not new component work:

- `SplitPane` (`frontend/XGraph.html:526`) — resizable split container.
- `CornerHandle` (`frontend/XGraph.html:470`) — corner-drag resize.
- `OntologyViewer` (from `:1015`) — already accepts `maximized` /
  `onToggleMaximize` and renders a built-in Restore button (`:1151`).
- `CanvasGraph` (from `:5891`) — d3 force-graph; already accepts `maximized` /
  `onToggleMaximize` (`:6430`); bails on WKT (geo) input.
- `LabelChart` — the donut component (two instances today: "Nodes by Label" /
  "Edges by Label").
- `maximizedPanel` state (`:8430`, `null | 'ontology' | 'canvas'`) + an
  Escape-key restore handler (`:8432-8438`).

The current Visualize tab (render at `:9746-9842`) and the separate Ontology tab
(render at `:9843-9853`) simply don't wire these into a single explorer-style
page. FalkorDB's gateway data path (`loadSchema` `:8701`, `handleVisualizeLoad`
`:9439`, `graphTableFromGateway` in `gateway.js`) is already in place and stays.

## Architecture

The Visualize page is **one engine-neutral layout**. Only the *data-fetch*
underneath branches on `engine`. Nothing else forks per engine.

### Section 1 — Unified Visualize page (with resize + maximize)

- Remove the `ontology` entry from the `ACTIONS` list (`frontend/XGraph.html:6508`).
- Delete the standalone `activeAction === 'ontology'` render block (`:9843-9853`).
- Rebuild the `activeAction === 'visualize'` render (`:9746-9842`) to mirror
  explorer's layout (explorer `KineticaGraphExplorer.html:8747-8829`):
  - **Outer horizontal `SplitPane`** (draggable) — left column =
    ontology-over-graph, right column = donuts — plus the `CornerHandle` for
    corner-drag resizing.
  - **Left column: vertical `SplitPane`** — `OntologyViewer` (top) over
    `CanvasGraph` (bottom), both draggable.
  - **Right column:** the two `LabelChart` donuts ("Nodes by Label" /
    "Edges by Label"), same props they receive today.
- **Maximize / restore:** wire `onToggleMaximize` on `OntologyViewer`
  (→ `setMaximizedPanel('ontology')`) and `CanvasGraph`
  (→ `setMaximizedPanel('canvas')`). Render two full-viewport overlay blocks —
  `maximizedPanel === 'ontology'` and `maximizedPanel === 'canvas'` — mirroring
  explorer `:8869-8907`, but **non-geo only** (no `MapView` / `DeckMapView`
  branches — just `OntologyViewer` and `CanvasGraph`). Each maximized panel gets
  `maximized={true}` and an `onToggleMaximize={() => setMaximizedPanel(null)}`;
  the Escape handler already present (`:8432-8438`) restores.

### Section 2 — Kinetica-direct fast fetch

- In `handleVisualizeLoad`, add a branch: when `engine === 'kinetica'`, fetch
  directly from Kinetica REST instead of the gateway.
- Port explorer's **non-geo** fetch path
  (`fetchGraphEntities` / `fetchEntitiesBatched`):
  - `POST {url}/get/graph/entities` with `Authorization: Basic btoa(user+':'+pass)`.
  - Fetch **nodes and edges separately** via `options.entity_type` = `'node'`
    then `'edge'`.
  - Probe with `limit:0` + `concise_edge_connectivity:'true'` to size the graph.
  - Small graphs (≤ `BATCH_THRESHOLD` = 300000 edges): `limit:-1` (all at once).
    Large graphs: batched — `batchSize = total > 1000000 ? 2000000 : 100000`.
  - Decode typed arrays by `info.payload_type`:
    - int/string: stride-2 nodes `[id,label]`, stride-4 edges `[eid,n1,n2,label]`.
    - concise edges: always `entities_int` stride-4 `[eid,v0,v1,label]` where
      `v0`/`v1` are **integer indices** into the node array (expand via cached
      `conciseNodeIds` / `conciseNodeCoords`, matching explorer).
    - `outer.labels` is 1-based; `resolveLabelStr` maps index → `'["label"]'`.
  - **Skip the geo path entirely** — do NOT decode `entities_double`
    (WKT/geo stride 3/6); geo is deferred (see Section 3).
- Build the `graphTableData` shape `CanvasGraph` consumes directly from the
  decoded typed arrays (nodes: `{NODE_NAME,NODE_LABEL}`; edges:
  `{NODE1_NAME,NODE2_NAME,EDGE_LABEL}`), i.e. produce the same shape
  `graphTableFromGateway` yields so `CanvasGraph` needs no change.
- **Label donuts (Kinetica):** enrich `labelData` with real per-label counts
  from `POST {url}/show/graph {export_graph_schema:'true'}` → `info.labeljson`
  → `{node_labels:[{labels,count}], edge_labels:[{labels,count}],
  total_labeled_nodes/edges, ...}`, matching explorer. This feeds both
  `LabelChart` donuts and the CanvasGraph color map with weighted counts.
- **Credentials:** reuse the browser-held Kinetica connection
  (`graphConn` / `olapConn`: `url`, `user`, `password`) to authenticate the
  direct POSTs. Plumb these into the `credentials` object for
  `engine === 'kinetica'`. (Today `credentials` is a vestigial gateway-base
  stub; the direct path needs the real Kinetica REST base + basic-auth creds.)

### Section 3 — Scope, deferrals, validation

- **No "Max load" field.** Remove the current "Max load" number input
  (`:9767`) and stop relying on a paging cap for the Kinetica path. The
  Kinetica-direct fetch pulls the whole graph (`limit:-1`, or batched for very
  large graphs), exactly like explorer.
- **Viz gating = explorer's model.** Keep only the big-graph confirm modal:
  compare graph counts against `bigGraphThreshold` (the 10K/100K/1M/10M/100M/∞
  selector explorer uses, explorer `:8804-8807`); if over, pop the
  "Large graph … Continue / Cancel" confirm before fetching. No silent cap.
- **Deferred:** geo / WMS / deck.gl — `DeckMapView` and `MapView` stay dead
  (unmounted); full WKT rendering stays out (`CanvasGraph` already bails on
  WKT). These are explicitly out of scope for this spec.
- **Validation:**
  - `cd frontend && node tests/test_transforms.mjs && node tests/test_client.mjs`
    (gateway.js transforms + client, still green).
  - esbuild JSX transpile of the `text/babel` block
    (`frontend/node_modules/esbuild`, `loader:'jsx'`) — syntax gate.
  - `curl` the gateway root for a 200 (the gateway serves `XGraph.html`).
  - Bump `EXPLORER_VERSION` (`:50`) on the frontend change.
  - **True acceptance is browser-driven by the user** against
    `expero.banking_graph` and `extracted_graph_kinetica1` — the React app
    cannot be runtime-verified headlessly.

### Section 4 — Engine matrix (same page, different means)

The unified page renders identically for every engine; the table shows only
where each panel's data comes from.

| Data feeding the page | Kinetica | FalkorDB / other gateway engines |
|---|---|---|
| **Ontology** (`dotString` → `OntologyViewer`) | gateway `getSchema().dot` (`loadSchema`, `:8705`) | same — gateway `getSchema().dot` |
| **Graph entities** (nodes/edges → `CanvasGraph`) | **direct Kinetica REST** fast typed-array fetch (new branch) | **gateway `/entities`** paging → `graphTableFromGateway` (existing `handleVisualizeLoad`, `:9439`) |
| **Label donuts** (`labelData` → 2× `LabelChart`) | enriched with **real per-label counts** from `show/graph` `labeljson` | schema-derived names (`loadSchema`, `:8712`, `count:0`) — donuts render label breakdown, no weighting |

Consequences:

- Ontology + the whole layout (SplitPane resize, maximize/restore, donuts) are
  shared by every engine — one code path renders the page regardless of engine.
- The only `engine === 'kinetica'` fork is *how node/edge data and label counts
  are obtained*. FalkorDB (and any other gateway-served engine, including
  DuckDB-backed data) keeps today's gateway path untouched.
- No regression for FalkorDB: it gets the same merged ontology+graph+donuts page
  with resize/maximize; it simply doesn't gain Kinetica's direct-fetch speedup
  or weighted donut counts (neither exists for FalkorDB today).

## Components (units of change)

All changes are within `frontend/XGraph.html`; no backend change.

1. **`ACTIONS` list** (`:6508`) — remove the `ontology` entry.
2. **Visualize render** (`:9746-9842`) — replace with the explorer-style
   two-column `SplitPane` layout (ontology + graph + donuts) and remove the
   "Max load" input.
3. **Ontology render** (`:9843-9853`) — delete (folded into Visualize).
4. **Maximized overlays** — add `maximizedPanel === 'ontology'` and
   `=== 'canvas'` full-viewport overlay blocks (non-geo).
5. **`handleVisualizeLoad`** (`:9439`) — add the `engine === 'kinetica'`
   direct-fetch branch (port explorer's non-geo `fetchGraphEntities` /
   `fetchEntitiesBatched` + `show/graph` labeljson enrichment); keep the
   existing gateway path for all other engines.
6. **Credentials plumbing** — populate `credentials` with the real Kinetica
   REST base + basic-auth creds (`graphConn`/`olapConn`) when
   `engine === 'kinetica'`.
7. **`EXPLORER_VERSION`** (`:50`) — bump.

## Error handling

- Kinetica-direct fetch failures (network, auth, unexpected `payload_type`)
  surface in the existing viz progress/error UI; the page must not crash —
  fall back to the existing empty/"click to load" state, not a blank screen.
- `show/graph` labeljson failure → donuts fall back to schema-derived label
  names (the FalkorDB behavior), so the page still renders.
- The big-graph confirm modal must gate *before* any fetch begins.

## Testing

- Node unit tests (`test_transforms.mjs`, `test_client.mjs`) must stay green —
  `graphTableFromGateway` and the client are unchanged.
- esbuild JSX transpile of the `text/babel` block passes (no syntax errors).
- `curl localhost:8090/` returns 200.
- Browser acceptance (user): open the Visualize tab on
  `expero.banking_graph` and `extracted_graph_kinetica1`; confirm ontology +
  force-graph + donuts render on one page, panels resize and maximize/restore,
  Kinetica fetch is fast, and FalkorDB still renders via the gateway path.

## Out of scope / deferred

- Geo/WMS/deck.gl rendering (`MapView`, `DeckMapView`) and WKT decode.
- Any backend/gateway change.
- Per-engine label-count weighting for FalkorDB (no source for it today).
