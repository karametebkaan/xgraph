# FalkorDB Fast Visualize (A+B) — Design Spec

**Date:** 2026-07-28
**Status:** approved (in-conversation)

## Problem

FalkorDB "Pull + Visualize" is slow on large graphs (banking_graph: 622,015
nodes / 845,734 edges) compared to the Kinetica route, which feels instant.

The asymmetry is architectural, not a tuning gap:

- **Kinetica** (`gateway.js:kineticaFetchGraph`) is **browser-direct** to
  Kinetica's HTTP REST (`/get/graph/entities`, `limit:-1`): the whole node set
  in one request, edges in a second request using **concise edge connectivity**
  (index pairs). It bypasses the gateway entirely.
- **FalkorDB** speaks RESP (raw TCP) — a browser cannot reach it, so everything
  funnels through the Python gateway. There is no "dump the whole graph"
  primitive, so the frontend runs a **paging loop** (`handleVisualizeLoad`,
  `VISUALIZE_PAGE_SIZE=10000`): ~10 pages at the default 100k Viz limit, ~63 for
  a full load — each a full browser→gateway→FalkorDB→gateway→browser round trip
  with a `setTimeout(0)` yield between, and each node/edge shipped as fat JSON.

Two costs dominate: (1) many browser↔gateway round trips, (2) fat JSON size and
parse. Full Kinetica parity is impossible (no direct browser channel, no
whole-graph dump), but both costs can be largely removed.

## Approach: A + B

**A — one server-side call.** Move the paging loop *into* the gateway. One HTTP
request; the gateway pages FalkorDB internally over its fast local socket,
applies the induced-subgraph edge filter server-side, and returns the whole
capped subgraph in one response. Removes the browser round-trip chatter and the
per-loop yields.

**B — slim payload.** Return the subgraph columnar, with edges as **index pairs**
into the node array (the analog of Kinetica's `concise_edge_connectivity`).
Repeating node-id strings in edges — the bulk of the payload — become small
integers, cutting wire size and parse time.

**Progress UX:** single blocking call + indeterminate "loading…" spinner (chosen
over streaming — simplest, matches how Kinetica's fast path already behaves, and
it enables the index-pair payload since all node indices are known before the
response is built).

Non-goals: no change to the Kinetica branch, the `/entities` paged primitive, the
N/E render sliders, or the Viz-limit (`bigGraphThreshold`) semantics.

## Backend

### New adapter method: `fetch_subgraph(graph, limit)`

Declared on `GraphEngineAdapter` (`adapters/base.py`) as a **concrete default**
(NOT `@abstractmethod` — mirrors `ingest_elements`/`storage`) so existing/future
adapters aren't forced to implement it. The default loops the adapter's own
`fetch_entities` and assembles the concise shape, so any adapter with keyset
paging gets a correct (if not maximally optimized) `/visualize` for free.

`FalkorDBAdapter` **overrides** it with the direct implementation.

Returns the **concise columnar** dict:

```python
{
  "ids":    [str, ...],     # node ids,   length N
  "labels": [label, ...],   # node labels, length N (str or list — as fetch_entities returns)
  "src":    [int, ...],     # edge source as INDEX into ids[], length E
  "dst":    [int, ...],     # edge target as INDEX into ids[], length E
  "etype":  [str, ...],     # edge type,   length E
  "total_nodes": int,       # true graph node count (may exceed N when capped)
  "total_edges": int,       # true graph edge count
  "capped": bool,           # True when N < total_nodes
}
```

Rules:
- `limit` caps the number of **nodes** pulled (the Viz limit). Page in
  `PAGE = 10000` chunks via keyset until `len(ids) >= limit` or the graph is
  exhausted.
- **Induced subgraph:** keep only edges whose BOTH endpoints are in the pulled
  node set — so `src`/`dst` indices are always valid and a capped pull renders a
  coherent subgraph (a full pull drops nothing). Edges to nodes beyond the cap
  are dropped.
- `total_nodes`/`total_edges` come from the adapter's `_counts(g)` (the same
  server-side aggregation `get_schema` already uses) — the true totals, so the
  frontend's big-graph threshold and "N of TOTAL" indicator stay accurate.
- No node properties (the viz transform ignores them; node-detail uses
  `get_record`), matching the Phase-1 keyset `fetch_entities`.

FalkorDB override detail: reuse the exact keyset queries already in
`fetch_entities` (nodes: `MATCH (n:Entity) [WHERE n.NODE > $after] RETURN
n.NODE, n.LABEL ORDER BY n.NODE LIMIT $l`). Build a `dict id -> index` as nodes
accumulate; run the edge query per page (`MATCH (a:Entity)-[r]->(b) WHERE a.NODE
IN $ids RETURN a.NODE, b.NODE, type(r)`) and keep an edge only when BOTH endpoints
are already in the index map. (Because a full pull loads every node, no edge is
lost; a capped pull drops only edges leaving the pulled set.)

### New endpoint: `GET /visualize`

```python
@app.get("/visualize")
def visualize(graph: str, engine: str = "", limit: int = 100000,
              session: str | None = None):
    try:
        return _resolve_adapter(session, engine).fetch_subgraph(graph, limit)
    except Exception as e:
        return _err(engine, e)
```

Same error envelope as `/entities`.

## Frontend

### `gateway.js`

- **Client method** `visualize(graph, limit)` → `GET /visualize?graph=&limit=`
  (via the existing `q(...)` session/engine helper), returns the concise dict.
- **Transform** `graphTableFromConcise(concise)` → the `graphTableData` shape
  `CanvasGraph` consumes, reconstructing edge endpoints from the index pairs:

  ```js
  {
    nodes: { records: ids.map((id,i) => ({ NODE_NAME: id, NODE_LABEL: labelToString(labels[i]) })),
             headers: ["NODE_NAME","NODE_LABEL"], total: total_nodes },
    edges: { records: src.map((s,i) => ({ NODE1_NAME: ids[s], NODE2_NAME: ids[dst[i]], EDGE_LABEL: labelToString(etype[i]) })),
             headers: ["NODE1_NAME","NODE2_NAME","EDGE_LABEL"], total: total_edges },
    nodeTable: "gateway (entities/nodes)", edgeTable: "gateway (entities)",
  }
  ```

  `total` uses the true graph totals from the response (not the pulled length),
  matching the existing `graphTableFromGateway`/Kinetica behavior. Reuses the
  existing `labelToString` helper.

### `XGraph.html` — `handleVisualizeLoad` FalkorDB branch

Replace the `while` cursor loop with a single call:

```js
var graphTotal = graphCounts.nodes || 0;
var target = loadAll === true ? (graphTotal || bigGraphThreshold)
                              : Math.min(graphTotal || bigGraphThreshold, bigGraphThreshold);
setVizProgress({ loaded: 0, total: target, loading: true,
                 capped: (graphTotal > 0 && target < graphTotal), graphTotal: graphTotal });
try {
    var concise = await gwClient.visualize(activeGraph, target);
    var gt = window.xgraphGateway.graphTableFromConcise(concise);
    setGraphTableData(gt);
    setVizProgress({ loaded: gt.nodes.records.length, total: target, loading: false,
                     capped: !!concise.capped, graphTotal: concise.total_nodes || graphTotal });
} catch (err) {
    setVizLoadError(err.message);
    setVizProgress(function(p){ return Object.assign({}, p, { loading:false }); });
}
```

- While `loading` is true and no incremental count is available, the progress
  bar renders in its existing indeterminate/animated state (the `loaded/total`
  bar is fine showing `0/target` during the single call, then jumps to full).
- The cap-hint JSX ("Showing first N of TOTAL nodes — raise Viz limit") already
  reads `vizProgress.capped`/`graphTotal`; both are set from the response, so it
  keeps working.
- Auto-viz (small graphs) calls `handleVisualizeLoad()` unchanged.

The Kinetica branch (browser-direct `kineticaFetchGraph`) is **untouched**.

Bump the frontend version const `v0.23.0` → `v0.24.0`.

## Testing

- **Backend unit** (`tests/`, FakeAdapter): `fetch_subgraph` returns the concise
  shape; index pairs resolve to the right endpoints; the induced-subgraph filter
  drops edges to nodes beyond a small cap; `capped`/`total_nodes`/`total_edges`
  are correct. `/visualize` endpoint returns the shape and uses the standard
  error envelope for a bad graph.
- **Backend live (skips if FalkorDB down)**: `fetch_subgraph` on a real graph
  returns a fully coherent subgraph (every edge index valid), and a full pull
  (`limit >= total`) drops no edges — matches counts from `_counts`.
- **Frontend Node** (`tests/*.mjs`, injected fake `fetch`): `graphTableFromConcise`
  reconstructs endpoints from index pairs and carries the true totals;
  `visualize()` client builds the right URL and parses the response.
- **Frontend Babel transpile + `curl` 200**: the `handleVisualizeLoad` edit
  parses and the app still serves; real behavior is browser-driven (user).

## Constraints

- No git commits under `xgraph/` (standing repo rule).
- Backend own venv (`backend/.venv`); tests run from `backend/`.
- Frontend edits to `XGraph.html` are anchored search-and-replace against
  verbatim strings; validate with the Babel transpile + `curl` 200.
