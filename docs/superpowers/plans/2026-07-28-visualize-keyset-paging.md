# Fast Pull + Visualize (keyset paging) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace O(offset²) `SKIP/LIMIT` entity paging with keyset (cursor) pagination so Pull + Visualize on large FalkorDB graphs is fast, coherent, and slim.

**Architecture:** `fetch_entities` pages nodes by the indexed `:Entity(NODE)` key (`WHERE n.NODE > $after ORDER BY n.NODE LIMIT $l`) and pulls each page's edges by their source node id (`WHERE a.NODE IN $ids`), returning a `next_cursor`. The `properties(n)` payload is dropped (the viz ignores it; node-detail uses `get_record`). The frontend loops on the cursor and renders the induced subgraph.

**Tech Stack:** FastAPI/Python gateway + FalkorDB (openCypher/RESP); single-file React (`XGraph.html`) + `gateway.js`; pytest (backend), Node `.mjs` (frontend).

## Global Constraints

- **NO git commits under `xgraph/`** (CLAUDE.md). Ignore the commit steps in the writing-plans template. Each task ends with a **checkpoint: run the full test suite green** instead of a commit.
- Backend tests run from `backend/` with its own venv: `./.venv/bin/python -m pytest tests/ -v`.
- Live FalkorDB tests **SKIP** (never fail) when the engine is unreachable.
- Frontend `XGraph.html` edits are verbatim anchored search-and-replace; validate with a gateway `curl` 200 (Babel/browser acceptance is user-driven).
- `VISUALIZE_PAGE_SIZE` stays 10000; `bigGraphThreshold` default stays 100000.
- Contract shape produced by `fetch_entities`: `{"nodes":[{"id","label"}], "edges":[{"id","source","target","type"}], "next_cursor": <str|null>}`. Nodes carry **no** `props`.

---

### Task 1: FakeAdapter cursor paging + `/entities` `after` param

**Files:**
- Modify: `backend/xgraph_gateway/adapters/fake.py:19-20` (`fetch_entities`)
- Modify: `backend/xgraph_gateway/app.py:279-285` (`/entities` endpoint)
- Test: `backend/tests/test_paging.py` (rewrite the fake + endpoint tests)

**Interfaces:**
- Produces: `FakeAdapter.fetch_entities(self, graph, limit, after=None) -> {"nodes":[{"id","label"}], "edges":[...], "next_cursor": str|None}`
- Produces: `GET /entities?graph=&limit=&after=&engine=&session=` → same dict.

- [ ] **Step 1: Write the failing tests** — replace the whole body of `backend/tests/test_paging.py` with:

```python
import pytest
from fastapi.testclient import TestClient
from xgraph_gateway.app import create_app
from xgraph_gateway.adapters.fake import FakeAdapter
from xgraph_gateway import config


def test_fake_adapter_first_page_no_after():
    a = FakeAdapter()
    page = a.fetch_entities("demo_graph", 1)
    assert page["nodes"][0]["id"] == "b1"
    assert "props" not in page["nodes"][0]        # slim payload
    assert page["next_cursor"] == "b1"            # full page -> cursor


def test_fake_adapter_second_page_via_cursor():
    a = FakeAdapter()
    page = a.fetch_entities("demo_graph", 1, after="b1")
    assert page["nodes"][0]["id"] == "w1"


def test_fake_adapter_end_returns_null_cursor():
    a = FakeAdapter()
    page = a.fetch_entities("demo_graph", 10)     # both nodes, not a full page
    assert {n["id"] for n in page["nodes"]} == {"b1", "w1"}
    assert page["next_cursor"] is None


def test_fake_adapter_edges_scoped_to_page_source():
    a = FakeAdapter()
    # e1 has source b1: present on the page that contains b1, absent otherwise
    with_b1 = a.fetch_entities("demo_graph", 1, after=None)      # -> b1
    assert [e["id"] for e in with_b1["edges"]] == ["e1"]
    only_w1 = a.fetch_entities("demo_graph", 1, after="b1")      # -> w1
    assert only_w1["edges"] == []


def test_fake_full_coverage_no_dups():
    a = FakeAdapter()
    seen, cursor = [], None
    while True:
        page = a.fetch_entities("demo_graph", 1, after=cursor)
        if not page["nodes"]:
            break
        seen += [n["id"] for n in page["nodes"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == ["b1", "w1"]                   # every node once, in key order


def _client():
    return TestClient(create_app(adapter_factory=lambda e: FakeAdapter()))


def test_entities_endpoint_first_page():
    r = _client().get("/entities", params={"engine": "fake", "graph": "g", "limit": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["nodes"][0]["id"] == "b1"
    assert body["next_cursor"] == "b1"


def test_entities_endpoint_after_cursor():
    r = _client().get("/entities", params={"engine": "fake", "graph": "g", "limit": 1, "after": "b1"})
    assert r.status_code == 200
    assert r.json()["nodes"][0]["id"] == "w1"


def _falkordb_or_skip():
    from xgraph_gateway.adapters.falkordb_adapter import FalkorDBAdapter
    try:
        a = FalkorDBAdapter(config.load_settings())
        a.list_graphs()
        return a
    except Exception as e:
        pytest.skip(f"FalkorDB unreachable: {e}")


def test_live_falkordb_keyset_full_coverage():
    a = _falkordb_or_skip()
    if "banking_graph" not in a.list_graphs():
        pytest.skip("banking_graph not loaded")
    total = a.get_schema("banking_graph")["counts"]["nodes"]
    seen, cursor, pages = set(), None, 0
    while True:
        page = a.fetch_entities("banking_graph", 5000, after=cursor)
        ns = page["nodes"]
        if not ns:
            break
        before = len(seen)
        seen.update(n["id"] for n in ns)
        assert len(seen) == before + len(ns)      # no dup ids across pages
        assert "props" not in ns[0]               # slim payload
        cursor = page["next_cursor"]
        pages += 1
        if cursor is None:
            break
    assert len(seen) == total                      # complete coverage
    assert pages > 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_paging.py -v`
Expected: FAIL (FakeAdapter has no `after`/`next_cursor`; endpoint has no `after`).

- [ ] **Step 3: Rewrite `FakeAdapter.fetch_entities`** — replace lines 19-20 of `backend/xgraph_gateway/adapters/fake.py`:

```python
    def fetch_entities(self, graph, limit, after=None):
        ordered = sorted(_NODES, key=lambda n: n["id"])
        rest = [n for n in ordered if after is None or n["id"] > after]
        page = rest[:limit]
        slim = [{"id": n["id"], "label": n["label"]} for n in page]
        ids = {n["id"] for n in page}
        edges = [e for e in _EDGES if e["source"] in ids]
        next_cursor = page[-1]["id"] if len(page) == limit and page else None
        return {"nodes": slim, "edges": edges, "next_cursor": next_cursor}
```

- [ ] **Step 4: Update the `/entities` endpoint** — replace lines 279-285 of `backend/xgraph_gateway/app.py`:

```python
    @app.get("/entities")
    def entities(graph: str, engine: str = "", limit: int = 1000,
                 after: str | None = None, session: str | None = None):
        try:
            return _resolve_adapter(session, engine).fetch_entities(graph, limit, after)
        except Exception as e:
            return _err(engine, e)
```

- [ ] **Step 5: Run tests to verify they pass** (live test skips if FalkorDB is down)

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_paging.py -v`
Expected: PASS (or SKIP for the live test).

- [ ] **Step 6: Checkpoint — full backend suite green**

Run: `cd backend && ./.venv/bin/python -m pytest tests/ -v`
Expected: all pass/skip; no failures. (No commit — xgraph rule.)

---

### Task 2: FalkorDBAdapter keyset `fetch_entities`

**Files:**
- Modify: `backend/xgraph_gateway/adapters/falkordb_adapter.py:486-494` (`fetch_entities`)
- Test: covered by `test_live_falkordb_keyset_full_coverage` (Task 1) — skips if FalkorDB down.

**Interfaces:**
- Consumes: `self._graph(graph)`, the `:Entity(NODE)` range index (built by the loader).
- Produces: `FalkorDBAdapter.fetch_entities(self, graph, limit, after=None)` → the Global-Constraints contract shape.

- [ ] **Step 1: Confirm the live test currently fails/skips against the old adapter**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_paging.py::test_live_falkordb_keyset_full_coverage -v`
Expected: SKIP if FalkorDB down; if up, FAIL (old adapter returns `props` and no `next_cursor`).

- [ ] **Step 2: Replace `fetch_entities`** — replace lines 486-494 of `backend/xgraph_gateway/adapters/falkordb_adapter.py`:

```python
    def fetch_entities(self, graph, limit, after=None):
        # Keyset (cursor) pagination on the indexed :Entity(NODE) key -- each
        # page is O(log n + pageSize) instead of SKIP's O(offset). Edges are
        # pulled per page by their SOURCE node id: every edge has exactly one
        # source and every node lands in exactly one page, so across a full
        # pull each edge is returned exactly once (complete, dup-free). No
        # properties(n): the viz transform ignores node props and node-detail
        # fetches them separately via get_record.
        g = self._graph(graph)
        if after is None:
            nq = ("MATCH (n:Entity) RETURN n.NODE, n.LABEL "
                  "ORDER BY n.NODE LIMIT $l")
            params = {"l": limit}
        else:
            nq = ("MATCH (n:Entity) WHERE n.NODE > $after "
                  "RETURN n.NODE, n.LABEL ORDER BY n.NODE LIMIT $l")
            params = {"l": limit, "after": after}
        nodes = [{"id": r[0], "label": r[1]} for r in
                 g.query(nq, params, timeout=60000).result_set]
        ids = [n["id"] for n in nodes]
        edges = []
        if ids:
            eq = ("MATCH (a:Entity)-[r]->(b) WHERE a.NODE IN $ids "
                  "RETURN r.ID, a.NODE, b.NODE, type(r)")
            edges = [{"id": r[0], "source": r[1], "target": r[2], "type": r[3]}
                     for r in g.query(eq, {"ids": ids}, timeout=60000).result_set]
        next_cursor = nodes[-1]["id"] if len(nodes) == limit and nodes else None
        return {"nodes": nodes, "edges": edges, "next_cursor": next_cursor}
```

- [ ] **Step 3: Run the live test to verify it passes (or skips)**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_paging.py -v`
Expected: PASS if FalkorDB + `banking_graph` present; SKIP otherwise.

- [ ] **Step 4: Checkpoint — full backend suite green**

Run: `cd backend && ./.venv/bin/python -m pytest tests/ -v`
Expected: all pass/skip. (No commit.)

---

### Task 3: `gateway.js fetchEntities` uses `after`

**Files:**
- Modify: `frontend/gateway.js:291` (`fetchEntities`)
- Test: `frontend/tests/test_client.mjs` (add a case)

**Interfaces:**
- Consumes: Task 1 endpoint (`&after=`).
- Produces: `client.fetchEntities(graph, limit, after)` → GET `/entities?graph=&limit=&after=` (omits `after` when null/empty).

- [ ] **Step 1: Write the failing test** — append to `frontend/tests/test_client.mjs` (before its final summary/exit line; follow the file's existing fake-fetch + assert style):

```javascript
// fetchEntities keyset: sends after when given, omits it when null
{
  let captured = [];
  const fakeFetch = async (url) => { captured.push(url); return { ok: true, json: async () => ({ nodes: [], edges: [], next_cursor: null }) }; };
  const gw = require("../gateway.js");
  const client = gw.makeClient("http://gw", "falkordb", { fetchImpl: fakeFetch });
  await client.fetchEntities("g", 10, "abc");
  await client.fetchEntities("g", 10, null);
  assert(captured[0].includes("after=abc"), "after sent when provided");
  assert(!captured[1].includes("after="), "after omitted when null");
  console.log("ok: fetchEntities after param");
}
```

> If `makeClient`'s fetch-injection signature differs, match how the existing tests in this file inject `fetch` (read the top of `test_client.mjs` first) — the assertions on `captured` URLs stay the same.

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && node tests/test_client.mjs`
Expected: FAIL (old `fetchEntities` builds `&offset=`, not `&after=`).

- [ ] **Step 3: Update `fetchEntities`** — replace line 291 of `frontend/gateway.js`:

```javascript
      fetchEntities: function (graph, limit, after) {
        var url = "/entities?graph=" + encodeURIComponent(graph) + "&limit=" + (limit || 1000);
        if (after != null && after !== "") url += "&after=" + encodeURIComponent(after);
        return getJSON(q(url));
      },
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd frontend && node tests/test_client.mjs`
Expected: PASS ("ok: fetchEntities after param").

- [ ] **Step 5: Checkpoint — frontend JS tests green**

Run: `cd frontend && node tests/test_transforms.mjs && node tests/test_client.mjs`
Expected: `transforms OK` + `client OK`. (No commit.)

---

### Task 4: Frontend cursor loop + induced-subgraph filter + cap hint

**Files:**
- Modify: `frontend/XGraph.html:9738-9767` (`handleVisualizeLoad` FalkorDB branch)
- Modify: `frontend/XGraph.html:6586` area (CanvasGraph viz-progress render — add cap hint)

**Interfaces:**
- Consumes: `gwClient.fetchEntities(graph, limit, cursor)` (Task 3); `page.next_cursor` (Tasks 1-2).
- Produces: `vizProgress` now also carries `{capped: bool, graphTotal: number}` for the hint.

- [ ] **Step 1: Replace the pull loop** — replace lines 9738-9767 of `frontend/XGraph.html` (the block from `var allNodes = [];` through `setGraphTableData(gt);`) with:

```javascript
        var allNodes = [];
        var allEdges = [];
        var graphTotal = graphCounts.nodes || 0;
        var target = loadAll === true ? (graphTotal || bigGraphThreshold) : Math.min(graphTotal || bigGraphThreshold, bigGraphThreshold);
        var capped = graphTotal > 0 && target < graphTotal;
        setVizProgress({ loaded: 0, total: target, loading: true, capped: capped, graphTotal: graphTotal });
        try {
            var cursor = null;
            while (allNodes.length < target) {
                var page = await gwClient.fetchEntities(activeGraph, VISUALIZE_PAGE_SIZE, cursor);
                var pageNodes = (page && page.nodes) || [];
                if (pageNodes.length === 0) break;   // end of data
                allNodes = allNodes.concat(pageNodes);
                allEdges = allEdges.concat((page && page.edges) || []);
                // Do NOT redraw the canvas per batch: a force-directed layout
                // re-solves on every data change, so streaming batches into it
                // makes social (non-geo) graphs flash/jitter during the load.
                // Only advance the progress bar here; the canvas is drawn ONCE
                // after the full load completes (below), so the layout solves once.
                setVizProgress({ loaded: allNodes.length, total: target, loading: true, capped: capped, graphTotal: graphTotal });
                cursor = page && page.next_cursor;
                if (!cursor) break;                  // reached end of graph
                // Yield a macrotask so the progress bar repaints between fetches.
                await new Promise(function(r){ setTimeout(r, 0); });
            }
            // Induced subgraph: keep only edges whose source AND target were both
            // loaded, so a capped pull renders a coherent subgraph instead of
            // edges dangling to nodes beyond the cap. A full load drops nothing.
            var nodeIdSet = {};
            for (var i = 0; i < allNodes.length; i++) nodeIdSet[allNodes[i].id] = true;
            allEdges = allEdges.filter(function(e){ return nodeIdSet[e.source] && nodeIdSet[e.target]; });
            // Feed CanvasGraph the REAL total node/edge counts (not the
            // accumulated page length) so its big-graph-threshold comparison
            // and "N of TOTAL" indicator are accurate.
            var gt = window.xgraphGateway.graphTableFromGateway({ nodes: allNodes, edges: allEdges });
            if (typeof graphCounts.nodes === 'number') gt.nodes.total = graphCounts.nodes;
            if (typeof graphCounts.edges === 'number') gt.edges.total = graphCounts.edges;
            setGraphTableData(gt);
```

- [ ] **Step 2: Add the cap hint** — in `frontend/XGraph.html`, immediately AFTER the `vizLoadError` span (line 6586):

```javascript
                {vizLoadError && <span style={{ fontSize:10, color:"#d63031", fontWeight:600, whiteSpace:"nowrap", flexShrink:0 }}>{vizLoadError}</span>}
```

insert:

```javascript
                {vizProgress.capped && !vizProgress.loading && vizProgress.graphTotal > 0 && (
                    <span style={{ fontSize:10, color:"#e17055", fontWeight:600, whiteSpace:"nowrap", flexShrink:0 }} title="Increase the Viz limit dropdown to pull more">
                        Showing first {vizProgress.total.toLocaleString()} of {vizProgress.graphTotal.toLocaleString()} nodes — raise Viz limit
                    </span>
                )}
```

- [ ] **Step 3: Validate the gateway still serves the page**

Run: `curl -s -o /dev/null -w '%{http_code}\n' localhost:8090/`
Expected: `200`. (If the gateway isn't running: `./xgraph start` first.)

- [ ] **Step 4: Live browser acceptance (user-driven)** — reload http://localhost:8090/, open Visualize on `banking_graph`:
  - Pull + Visualize completes far faster; progress advances smoothly to 100,000.
  - The orange hint "Showing first 100,000 of 622,015 nodes — raise Viz limit" appears.
  - Raise Viz limit to ∞ (0) / 1M and re-pull: loads the full graph without the quadratic stall; edges connect loaded nodes (no dangling).

- [ ] **Step 5: Checkpoint — full suites green**

Run: `cd backend && ./.venv/bin/python -m pytest tests/ -v && cd ../frontend && node tests/test_transforms.mjs && node tests/test_client.mjs`
Expected: backend all pass/skip; `transforms OK`; `client OK`. (No commit.)

---

## Notes / non-obvious

- Other test stubs (`backend/tests/test_promote_cypher.py:73`, `backend/tests/test_kinetica_file_import.py:58`) define `fetch_entities(self, graph, limit, offset=0)` and return `{}`. The endpoint now calls positionally `fetch_entities(graph, limit, after)`, which binds to their third param harmlessly (they return `{}` regardless and never hit `/entities`). No change required; rename to `after=None` only if you want signature parity.
- Keyset assumes `NODE` is comparable + unique (the MERGE identity key, indexed as `:Entity(NODE)`). Graphs without that index still work via `ORDER BY` (slower). The read path never creates indexes.
- Kinetica visualize path (the `graphEngine === 'kinetica'` branch above line 9738) is untouched.
