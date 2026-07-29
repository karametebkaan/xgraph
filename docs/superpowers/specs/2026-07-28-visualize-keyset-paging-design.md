# Design: Fast Pull + Visualize via keyset pagination (FalkorDB)

**Date:** 2026-07-28
**Status:** approved (design) — local only, not committed (see xgraph no-commit rule)

## Problem

"Pull + Visualize" on a large FalkorDB graph (`banking_graph`: 622,015 nodes /
845,734 edges) is slow. Three causes, ranked:

1. **Deep `SKIP/LIMIT` paging is quadratic (dominant).** `fetch_entities` pages
   with `... SKIP $off LIMIT $l` on both a `MATCH (n)` node scan and a
   `MATCH (a)-[r]->(b)` edge scan. FalkorDB `SKIP` scans and discards skipped
   rows (not an index seek), so page *k* scans *k × pageSize* rows first. Summed
   over a full pull this is O(n²/pageSize) — ~20M skipped-row scans for nodes and
   again for edges. The edge query re-traverses the whole relationship set every
   page.
2. **Sequential page requests.** The frontend awaits one page at a time in a
   `while` loop; total latency is the sum of already-slow pages.
3. **Fat JSON per node.** Each node returns `properties(n)` — the entire property
   bag — even though the viz transform (`graphTableFromGateway`) reads only
   `n.id`/`n.label` and **discards `props`**, and node-detail on click fetches
   props separately via `getRecord`/`get_record`. Pure dead weight.

Additional issue: the normal button is capped at `bigGraphThreshold = 100,000`,
so it silently loads only ~16% of `banking_graph`, and today's independent
node/edge offsets can return edges unrelated to the loaded nodes.

## Goals

- Make each page O(pageSize) instead of O(offset) — eliminate the quadratic cost.
- Stop shipping the unused `properties(n)` payload.
- Make node+edge pages coherent (edges reference loaded nodes; no dup/drop).
- Surface the capped-load state so it is not silent.

Non-goals: parallel page fetching (unnecessary once pages are cheap and each page
needs the prior cursor); changing the 100k default; touching the Kinetica path
(which uses its own direct REST fetch).

## Approach: keyset (cursor) pagination

### Backend contract change — `fetch_entities`

Signature: `fetch_entities(graph, limit, offset=0)` →
**`fetch_entities(graph, limit, after=None)`**.

Return shape: `{"nodes": [...], "edges": [...], "next_cursor": <str|None>}`
(adds `next_cursor`; nodes lose `props`).

**Nodes** — keyset on the indexed `:Entity(NODE)`, no `properties(n)`:

```cypher
-- after is None (first page): omit the WHERE clause
MATCH (n:Entity)
WHERE n.NODE > $after            -- only when after is not None
RETURN n.NODE AS id, n.LABEL AS label
ORDER BY n.NODE
LIMIT $l
```

- Python branches on `after`: first page runs the query without the `WHERE`
  line; subsequent pages bind `$after`.
- `next_cursor` = the last row's `NODE` when the page is full (`len == limit`),
  else `None` (end of graph).
- Uses the range index on `:Entity(NODE)` created at build time →
  O(log n + pageSize) per page.

**Edges** — pulled per node page, keyed off that page's node ids:

```cypher
MATCH (a:Entity)-[r]->(b)
WHERE a.NODE IN $ids
RETURN r.ID AS id, a.NODE AS source, b.NODE AS target, type(r) AS type
```

- `$ids` = the `id`s of the nodes returned in the same page.
- Each edge has exactly one source node, and each node appears in exactly one
  page, so across a full pull **every edge is returned exactly once** — complete
  and dup-free, with no edge offset/cursor.
- No `ORDER BY` on edges (avoids the paging dup/drop hazard entirely).

### Frontend — `handleVisualizeLoad` (FalkorDB branch)

Replace the offset loop with a cursor loop:

```js
var cursor = null, loaded = 0;
while (loaded < target) {
    var page = await gwClient.fetchEntities(activeGraph, VISUALIZE_PAGE_SIZE, cursor);
    var pageNodes = (page && page.nodes) || [];
    if (!pageNodes.length) break;
    allNodes = allNodes.concat(pageNodes);
    allEdges = allEdges.concat((page && page.edges) || []);
    loaded += pageNodes.length;
    cursor = page && page.next_cursor;
    setVizProgress({ loaded: allNodes.length, total: target, loading: true });
    if (!cursor) break;              // reached end of graph
    await new Promise(function(r){ setTimeout(r, 0); });
}
```

After the loop, **filter edges to the induced subgraph** — keep only edges whose
`source` and `target` are both in the loaded node-id set:

- Full load → drops nothing (all endpoints present).
- Capped load → yields a coherent subgraph instead of edges dangling to
  unloaded nodes (a correctness win over today's independent offsets).

`gateway.js fetchEntities(graph, limit, cursor)` builds the URL with
`&after=<cursor>` (URL-encoded) instead of `&offset=`; when `cursor` is null the
param is omitted (or empty), which the backend treats as first page.

### Surface the cap

When `target < graphCounts.nodes` (the pull is capped), render a hint by the viz
progress area, e.g.:

> Showing first 100,000 of 622,015 nodes — raise Viz limit to load more.

No change to the `bigGraphThreshold` default.

## Backend `/entities` endpoint

`GET /entities` gains an `after: str | None = None` param and passes it through;
the legacy `offset` param is removed (all internal, no external consumers, no
commits yet). Delegates to `fetch_entities(graph, limit, after)` as today.

## FakeAdapter

`FakeAdapter.fetch_entities` reimplemented with the cursor signature: sort its
in-memory nodes by id, return the first `limit` with `id > after`, set
`next_cursor` to the last id when the page is full, and return edges whose source
is in the returned page (matching the real adapter's contract) so gateway tests
exercise the same shape.

## Testing

- **Backend unit (FakeAdapter via `create_app`):** paging with the cursor covers
  every node exactly once across pages; `next_cursor` is correct and null at the
  end; edges are complete and dup-free across a full pull; the node payload has
  **no `props` key**.
- **Backend live (FalkorDB, skips if unreachable):** keyset-page a small graph to
  exhaustion, assert node coverage == count and edge coverage == count.
- **Frontend (`gateway.js`, Node):** `fetchEntities` builds `&after=` and omits it
  on a null cursor; `test_client` updated.

## Assumptions / notes

- Keyset assumes `NODE` is a comparable, unique key — true: it is the MERGE
  identity key, indexed as `:Entity(NODE)` by the loader.
- Graphs lacking the `:Entity(NODE)` index still work (ORDER BY falls back to a
  sort) — slower but correct. Documented fallback; the read path does not create
  indexes.
- Pages remain sequential (each needs the prior cursor); each is now cheap, so no
  parallelism is added (YAGNI).
- Kinetica visualize path is untouched.

## Files touched

- `backend/xgraph_gateway/adapters/falkordb_adapter.py` — `fetch_entities`.
- `backend/xgraph_gateway/adapters/fake.py` — `fetch_entities`.
- `backend/xgraph_gateway/app.py` — `/entities` param `offset` → `after`.
- `frontend/gateway.js` — `fetchEntities` uses `after`.
- `frontend/XGraph.html` — cursor loop, induced-subgraph edge filter, cap hint.
- `backend/tests/…`, `frontend/tests/test_client.mjs` — updated/added tests.
