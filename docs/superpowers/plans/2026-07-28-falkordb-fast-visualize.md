# FalkorDB Fast Visualize (A+B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make FalkorDB "Pull + Visualize" fast by pulling the whole capped subgraph in ONE server-side call (A) with a slim columnar index-pair payload (B), replacing the frontend's multi-round-trip paging loop.

**Architecture:** New adapter method `fetch_subgraph(graph, limit)` (concrete default on base that loops `fetch_entities`; FalkorDB overrides with a direct keyset implementation) returns a concise columnar dict (node id/label arrays + edge index-pair arrays + true totals + capped flag). New `GET /visualize` endpoint exposes it. Frontend gains a `visualize()` client + `graphTableFromConcise()` transform; `handleVisualizeLoad`'s FalkorDB branch collapses to a single `await`. Kinetica branch, `/entities`, render sliders, and Viz-limit semantics are untouched.

**Tech Stack:** FastAPI/Python gateway (`backend/`), FalkorDB (openCypher/RESP), single-file React `frontend/XGraph.html` + `gateway.js` UMD (Node-tested), pytest.

> **Implementation note (2026-07-28, as-built):** The FalkorDB `fetch_subgraph` diverged from the
> per-page keyset design below after a live test surfaced a real bug: FalkorDB **silently truncates
> every result set to `RESULTSET_SIZE` (default 10000 rows)**, so a full pull of `banking_graph`
> returned only 622,780 of 845,734 edges. The fast path now raises the cap
> (`_ensure_unbounded_results()` → `config.set RESULTSET_SIZE -1`, best-effort + cached) and **bulk-pulls
> nodes + edges in one query each** — ~14–17s for the full 620k-node/846k-edge graph and ~1s for a
> capped pull, vs ~100s for cap-safe SKIP paging. Keyset node paging (`_pull_nodes`) and `ORDER BY`+SKIP
> edge paging (`_pull_edges`, deterministic order ending in the unique `r.ID`) remain as the correct
> fallback when config is read-only. The concise payload shape, `/visualize` endpoint, and the frontend
> single-call branch are exactly as specified below.

## Global Constraints

- No git commits under `xgraph/` (standing repo rule) — write files, never stage/commit.
- Backend has its own venv `backend/.venv`; run tests from `backend/`.
- The concise response shape is EXACTLY: `{"ids":[str], "labels":[str|list], "src":[int], "dst":[int], "etype":[str], "total_nodes":int, "total_edges":int, "capped":bool}`. `src`/`dst` are 0-based INDICES into `ids`. Arrays `src`/`dst`/`etype` are equal length E; `ids`/`labels` are equal length N.
- Induced subgraph: an edge is kept ONLY if BOTH endpoints are in the pulled node set (so every index is valid). A full pull (`limit >= total_nodes`) drops no edges.
- `total_nodes`/`total_edges` are the TRUE graph counts (from `_counts`), which may exceed N/E when capped.
- `fetch_subgraph` is NOT `@abstractmethod` — a concrete default on the base class, mirroring `ingest_elements`/`storage`.
- Frontend `XGraph.html` edits are anchored search-and-replace against verbatim strings; validate with the Babel transpile + `curl` 200.
- Page size for internal keyset paging: `PAGE = 10000` (matches the frontend's old `VISUALIZE_PAGE_SIZE`).

---

### Task 1: `fetch_subgraph` base default + FalkorDB override

**Files:**
- Modify: `backend/xgraph_gateway/adapters/base.py` (add concrete `fetch_subgraph`)
- Modify: `backend/xgraph_gateway/adapters/falkordb_adapter.py` (override `fetch_subgraph`)
- Test: `backend/tests/test_fast_visualize.py` (new)

**Interfaces:**
- Consumes: existing `fetch_entities(graph, limit, after=None) -> {"nodes":[{"id","label"}], "edges":[{"id","source","target","type"}], "next_cursor"}`; FalkorDB `_graph(graph)`, `_counts(g) -> {"nodes":int,"edges":int}`.
- Produces: `fetch_subgraph(graph, limit) -> {"ids","labels","src","dst","etype","total_nodes","total_edges","capped"}` (the concise shape from Global Constraints), consumed by Task 2 (`/visualize`) and Task 4 (`graphTableFromConcise`).

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_fast_visualize.py`. The base default is exercised through `FakeAdapter` (which implements `fetch_entities`); the concise shape and induced-subgraph filter are the contract.

```python
from __future__ import annotations
from xgraph_gateway.adapters.fake import FakeAdapter


def _concise(adapter, graph, limit):
    return adapter.fetch_subgraph(graph, limit)


def test_fetch_subgraph_concise_shape_full_pull():
    a = FakeAdapter()
    out = a.fetch_subgraph("demo_graph", 1000)
    # required keys
    for k in ("ids", "labels", "src", "dst", "etype", "total_nodes", "total_edges", "capped"):
        assert k in out, k
    # parallel-array invariants
    assert len(out["ids"]) == len(out["labels"])
    assert len(out["src"]) == len(out["dst"]) == len(out["etype"])
    # every edge index is a valid position in ids[]
    n = len(out["ids"])
    assert all(0 <= i < n for i in out["src"])
    assert all(0 <= i < n for i in out["dst"])
    # a generous limit is not capped
    assert out["capped"] is False
    assert out["total_nodes"] == n


def test_fetch_subgraph_index_pairs_resolve_to_fetch_entities_endpoints():
    # The concise src/dst indices must reconstruct the SAME endpoint ids that
    # the row-based fetch_entities returns for a full pull.
    a = FakeAdapter()
    ent = a.fetch_entities("demo_graph", 1000)
    sub = a.fetch_subgraph("demo_graph", 1000)
    ids = sub["ids"]
    concise_edges = {(ids[s], ids[d], t)
                     for s, d, t in zip(sub["src"], sub["dst"], sub["etype"])}
    row_edges = {(e["source"], e["target"], e["type"]) for e in ent["edges"]}
    assert concise_edges == row_edges


def test_fetch_subgraph_induced_filter_and_capped_flag():
    # Cap below the node total: only edges whose BOTH endpoints survived the cap
    # are kept, and capped is True with the true totals preserved.
    a = FakeAdapter()
    full = a.fetch_subgraph("demo_graph", 1000)
    cap = 1
    sub = a.fetch_subgraph("demo_graph", cap)
    assert len(sub["ids"]) <= cap
    assert sub["capped"] is (len(sub["ids"]) < full["total_nodes"])
    assert sub["total_nodes"] == full["total_nodes"]
    kept = set(range(len(sub["ids"])))
    assert all(s in kept and d in kept for s, d in zip(sub["src"], sub["dst"]))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_fast_visualize.py -v`
Expected: FAIL — `AttributeError` / `fetch_subgraph` not defined (base has no such method yet).

- [ ] **Step 3: Add the concrete default to `base.py`**

Insert into `GraphEngineAdapter` (after `fetch_entities`'s abstract declaration, before `get_record`). It loops the adapter's own `fetch_entities`, builds an id→index map, and applies the induced-subgraph filter. `total_*` falls back to the pulled length when the adapter exposes no cheap count (base default has no `_counts`).

```python
    def fetch_subgraph(self, graph: str, limit: int) -> dict:
        """Whole capped subgraph in ONE call, concise/columnar (index-pair
        edges) — the fast Visualize path. Default: page this adapter's own
        fetch_entities until `limit` nodes (or exhaustion), keep only edges with
        BOTH endpoints in the pulled set, and emit index pairs. Adapters with a
        cheaper bulk path (FalkorDB) override this."""
        PAGE = 10000
        ids: list = []
        labels: list = []
        index: dict = {}
        edge_rows: list = []  # (source_id, target_id, type)
        after = None
        while len(ids) < limit:
            page = self.fetch_entities(graph, min(PAGE, limit - len(ids)), after)
            page_nodes = page.get("nodes") or []
            if not page_nodes:
                break
            for nd in page_nodes:
                nid = nd.get("id")
                if nid in index:
                    continue
                index[nid] = len(ids)
                ids.append(nid)
                labels.append(nd.get("label"))
            for ed in (page.get("edges") or []):
                edge_rows.append((ed.get("source"), ed.get("target"), ed.get("type")))
            after = page.get("next_cursor")
            if not after:
                break
        src: list = []
        dst: list = []
        etype: list = []
        for s, d, t in edge_rows:
            si = index.get(s)
            di = index.get(d)
            if si is None or di is None:
                continue  # induced subgraph: edge to a node beyond the cap
            src.append(si)
            dst.append(di)
            etype.append(t)
        total_nodes, total_edges = self._subgraph_totals(graph, len(ids), len(src))
        return {"ids": ids, "labels": labels, "src": src, "dst": dst,
                "etype": etype, "total_nodes": total_nodes,
                "total_edges": total_edges, "capped": len(ids) < total_nodes}

    def _subgraph_totals(self, graph: str, pulled_nodes: int, pulled_edges: int):
        """True (graph-wide) node/edge counts for fetch_subgraph. Default: no
        cheap count source, so report the pulled counts (never < what we
        returned). Adapters with a count query override this."""
        return pulled_nodes, pulled_edges
```

- [ ] **Step 4: Add the FalkorDB override to `falkordb_adapter.py`**

Add these two methods to `FalkorDBAdapter` (right after `fetch_entities`, before `fetch_node_attrs`). The override runs the SAME keyset node query as `fetch_entities`, builds the index map inline, runs the per-page edge query, and applies the induced filter; `_subgraph_totals` reuses `_counts`.

```python
    def fetch_subgraph(self, graph, limit):
        # A+B fast Visualize: whole capped subgraph in ONE gateway call, concise
        # (index-pair edges). Same keyset node query as fetch_entities, paged
        # server-side over the local socket; edges pulled per page by source id
        # and kept only when BOTH endpoints are in the pulled set (induced
        # subgraph). A full pull (limit >= node count) drops no edge.
        PAGE = 10000
        g = self._graph(graph)
        ids = []
        labels = []
        index = {}
        edge_rows = []  # (source_id, target_id, type)
        after = None
        while len(ids) < limit:
            take = min(PAGE, limit - len(ids))
            if after is None:
                nq = ("MATCH (n:Entity) RETURN n.NODE, n.LABEL "
                      "ORDER BY n.NODE LIMIT $l")
                params = {"l": take}
            else:
                nq = ("MATCH (n:Entity) WHERE n.NODE > $after "
                      "RETURN n.NODE, n.LABEL ORDER BY n.NODE LIMIT $l")
                params = {"l": take, "after": after}
            page_nodes = g.query(nq, params, timeout=60000).result_set
            if not page_nodes:
                break
            page_ids = []
            for nid, lbl in page_nodes:
                if nid not in index:
                    index[nid] = len(ids)
                    ids.append(nid)
                    labels.append(lbl)
                page_ids.append(nid)
            eq = ("MATCH (a:Entity)-[r]->(b) WHERE a.NODE IN $ids "
                  "RETURN a.NODE, b.NODE, type(r)")
            for s, d, t in g.query(eq, {"ids": page_ids}, timeout=60000).result_set:
                edge_rows.append((s, d, t))
            after = page_ids[-1] if len(page_nodes) == take else None
            if not after:
                break
        src, dst, etype = [], [], []
        for s, d, t in edge_rows:
            si = index.get(s)
            di = index.get(d)
            if si is None or di is None:
                continue
            src.append(si)
            dst.append(di)
            etype.append(t)
        total_nodes, total_edges = self._subgraph_totals(graph, len(ids), len(src))
        return {"ids": ids, "labels": labels, "src": src, "dst": dst,
                "etype": etype, "total_nodes": total_nodes,
                "total_edges": total_edges, "capped": len(ids) < total_nodes}

    def _subgraph_totals(self, graph, pulled_nodes, pulled_edges):
        c = self._counts(self._graph(graph))
        return c["nodes"], c["edges"]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_fast_visualize.py -v`
Expected: PASS (all three).

- [ ] **Step 6: Run the full backend suite (no regressions)**

Run: `cd backend && ./.venv/bin/python -m pytest tests/ -q`
Expected: all pass / skip as before (live tests skip if engines down).

- [ ] **Step 7: Commit — SKIPPED (no commits under xgraph/). Leave in working tree.**

---

### Task 2: `GET /visualize` endpoint

**Files:**
- Modify: `backend/xgraph_gateway/app.py` (add route after `/entities`)
- Test: `backend/tests/test_fast_visualize.py` (append endpoint tests)

**Interfaces:**
- Consumes: `fetch_subgraph(graph, limit)` from Task 1; existing `create_app(adapter_factory=...)`, `_resolve_adapter`, `_err`.
- Produces: `GET /visualize?graph=&limit=&engine=&session=` returning the concise dict or the `{"error":{...}}` envelope.

- [ ] **Step 1: Write the failing tests** (append to `backend/tests/test_fast_visualize.py`)

```python
from fastapi.testclient import TestClient
from xgraph_gateway.app import create_app


def _client():
    return TestClient(create_app(adapter_factory=lambda e: FakeAdapter()))


def test_visualize_endpoint_returns_concise_shape():
    c = _client()
    r = c.get("/visualize", params={"engine": "fake", "graph": "demo_graph", "limit": 1000})
    assert r.status_code == 200
    body = r.json()
    for k in ("ids", "labels", "src", "dst", "etype", "total_nodes", "total_edges", "capped"):
        assert k in body, k
    n = len(body["ids"])
    assert all(0 <= i < n for i in body["src"])


def test_visualize_endpoint_error_envelope_on_bad_graph():
    # FakeAdapter raises for an unknown graph -> uniform error envelope, not 200.
    class Boom(FakeAdapter):
        def fetch_subgraph(self, graph, limit):
            raise ValueError("no such graph")
    c = TestClient(create_app(adapter_factory=lambda e: Boom()))
    r = c.get("/visualize", params={"engine": "fake", "graph": "nope", "limit": 10})
    assert "error" in r.json()
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_fast_visualize.py -k visualize_endpoint -v`
Expected: FAIL — 404 (route not defined) so `"error"`/keys missing.

- [ ] **Step 3: Add the route** in `app.py` immediately after the `/entities` handler (around line 285):

```python
    @app.get("/visualize")
    def visualize(graph: str, engine: str = "", limit: int = 100000,
                  session: str | None = None):
        try:
            return _resolve_adapter(session, engine).fetch_subgraph(graph, limit)
        except Exception as e:
            return _err(engine, e)
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_fast_visualize.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit — SKIPPED (no commits under xgraph/).**

---

### Task 3: Live FalkorDB coherence test (skips if down)

**Files:**
- Test: `backend/tests/test_fast_visualize_live.py` (new)

**Interfaces:**
- Consumes: `FalkorDBAdapter.fetch_subgraph`; a settings/adapter factory matching the existing live FalkorDB tests' skip pattern.

- [ ] **Step 1: Write the live test** — mirror the skip pattern used by the existing FalkorDB live tests (find one, e.g. in `tests/` that builds a `FalkorDBAdapter` and skips on connection error). Use the same graph those tests use (e.g. `banking_graph`) or a small known graph.

```python
from __future__ import annotations
import pytest

# Mirror the existing live-FalkorDB skip pattern in this repo's tests.
falkordb = pytest.importorskip("falkordb")
from xgraph_gateway.adapters.falkordb_adapter import FalkorDBAdapter
from xgraph_gateway.config import Settings  # adjust import to match existing live tests


def _adapter_or_skip():
    try:
        a = FalkorDBAdapter(settings=Settings())
        a.list_graphs()  # forces a connection
        return a
    except Exception as e:
        pytest.skip(f"FalkorDB unreachable: {e}")


GRAPH = "banking_graph"  # adjust if the local live graph differs


def test_live_fetch_subgraph_is_coherent_and_full_pull_keeps_all_edges():
    a = _adapter_or_skip()
    if GRAPH not in a.list_graphs():
        pytest.skip(f"{GRAPH} not present")
    counts = a._counts(a._graph(GRAPH))
    # Full pull: limit >= node count -> every node and every edge present.
    full = a.fetch_subgraph(GRAPH, counts["nodes"])
    n = len(full["ids"])
    assert n == counts["nodes"]
    # every edge index valid (coherent subgraph)
    assert all(0 <= i < n for i in full["src"])
    assert all(0 <= i < n for i in full["dst"])
    # a full pull drops no edge
    assert len(full["src"]) == counts["edges"]
    assert full["capped"] is False
    assert full["total_nodes"] == counts["nodes"]
```

- [ ] **Step 2: Run it**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_fast_visualize_live.py -v`
Expected: PASS if FalkorDB + graph present, else SKIP. (If it fails on the graph name/import, adjust `GRAPH`/`Settings` import to match the sibling live tests, then re-run.)

- [ ] **Step 3: Commit — SKIPPED (no commits under xgraph/).**

---

### Task 4: `gateway.js` — `visualize()` client + `graphTableFromConcise()` transform

**Files:**
- Modify: `frontend/gateway.js` (add transform near `graphTableFromGateway`; add client method in `makeClient`; export the transform)
- Test: `frontend/tests/test_transforms.mjs` and `frontend/tests/test_client.mjs` (append)

**Interfaces:**
- Consumes: the concise dict from Task 2; existing `labelToString`, the `makeClient` `q(...)` URL helper and `getJSON`.
- Produces: `window.xgraphGateway.graphTableFromConcise(concise)` → `graphTableData` shape; client `visualize(graph, limit)` → concise dict. Consumed by Task 5.

- [ ] **Step 1: Write the failing transform test** (append to `frontend/tests/test_transforms.mjs`, following its existing require/assert style)

```js
// --- graphTableFromConcise: index-pair edges -> graphTableData shape ---
{
  const concise = {
    ids: ["a", "b", "c"], labels: ["Person", "Bank", ["X", "Y"]],
    src: [0, 1], dst: [1, 2], etype: ["MEMBER_OF", "OWNS"],
    total_nodes: 100, total_edges: 250, capped: true,
  };
  const gt = gateway.graphTableFromConcise(concise);
  assert.equal(gt.nodes.records.length, 3);
  assert.equal(gt.nodes.records[0].NODE_NAME, "a");
  assert.equal(gt.nodes.records[0].NODE_LABEL, "Person");
  // array label coerced to JSON-array-string via labelToString (same as graphTableFromGateway)
  assert.equal(gt.nodes.records[2].NODE_LABEL, gateway.graphTableFromGateway({ nodes: [{ id: "c", label: ["X", "Y"] }], edges: [] }).nodes.records[0].NODE_LABEL);
  // index pairs reconstruct endpoint ids
  assert.equal(gt.edges.records[0].NODE1_NAME, "a");
  assert.equal(gt.edges.records[0].NODE2_NAME, "b");
  assert.equal(gt.edges.records[0].EDGE_LABEL, "MEMBER_OF");
  assert.equal(gt.edges.records[1].NODE1_NAME, "b");
  assert.equal(gt.edges.records[1].NODE2_NAME, "c");
  // true totals carried, not the pulled length
  assert.equal(gt.nodes.total, 100);
  assert.equal(gt.edges.total, 250);
  console.log("ok graphTableFromConcise");
}
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && node tests/test_transforms.mjs`
Expected: FAIL — `gateway.graphTableFromConcise is not a function`.

- [ ] **Step 3: Add the transform** in `gateway.js` (right after `graphTableFromGateway`, before `recordFromGateway`):

```js
  // Concise/columnar subgraph (from GET /visualize) -> the graphTableData shape.
  // Edges arrive as INDEX PAIRS (src[i]/dst[i] index into ids[]) — the Kinetica
  // concise-connectivity analog — so reconstruct endpoint ids here. `total` uses
  // the TRUE graph counts from the response, not the pulled length.
  function graphTableFromConcise(c) {
    c = c || {};
    var ids = c.ids || [], labels = c.labels || [];
    var src = c.src || [], dst = c.dst || [], etype = c.etype || [];
    return {
      nodes: {
        records: ids.map(function (id, i) { return { NODE_NAME: id, NODE_LABEL: labelToString(labels[i]) }; }),
        headers: ["NODE_NAME", "NODE_LABEL"],
        total: (typeof c.total_nodes === "number" ? c.total_nodes : ids.length),
      },
      edges: {
        records: src.map(function (s, i) { return { NODE1_NAME: ids[s], NODE2_NAME: ids[dst[i]], EDGE_LABEL: labelToString(etype[i]) }; }),
        headers: ["NODE1_NAME", "NODE2_NAME", "EDGE_LABEL"],
        total: (typeof c.total_edges === "number" ? c.total_edges : src.length),
      },
      edgeTable: "gateway (entities)", nodeTable: "gateway (entities/nodes)",
    };
  }
```

Add `graphTableFromConcise: graphTableFromConcise,` to the module's export object (the same object that exports `graphTableFromGateway` — find that key and add the new one beside it).

- [ ] **Step 4: Run to verify the transform test passes**

Run: `cd frontend && node tests/test_transforms.mjs`
Expected: PASS (`ok graphTableFromConcise` + all existing).

- [ ] **Step 5: Write the failing client test** (append to `frontend/tests/test_client.mjs`, following its injected-fake-`fetch` style)

```js
// --- visualize(): GET /visualize with graph+limit, parses concise dict ---
{
  let calledUrl = null;
  const fakeFetch = async (url) => {
    calledUrl = url;
    return { json: async () => ({ ids: ["a"], labels: ["P"], src: [], dst: [], etype: [], total_nodes: 1, total_edges: 0, capped: false }) };
  };
  const client = gateway.makeClient("http://gw", "falkordb", fakeFetch);
  const out = await client.visualize("g1", 500);
  assert.ok(calledUrl.indexOf("/visualize") >= 0, "hits /visualize");
  assert.ok(calledUrl.indexOf("graph=g1") >= 0, "carries graph");
  assert.ok(calledUrl.indexOf("limit=500") >= 0, "carries limit");
  assert.equal(out.ids[0], "a");
  console.log("ok visualize client");
}
```

- [ ] **Step 6: Run to verify it fails**

Run: `cd frontend && node tests/test_client.mjs`
Expected: FAIL — `client.visualize is not a function`.

- [ ] **Step 7: Add the client method** inside `makeClient`, next to where `fetchEntities` is defined and returned. Definition:

```js
    async function visualize(graph, limit) {
      return getJSON(q("/visualize?graph=" + encodeURIComponent(graph) + "&limit=" + encodeURIComponent(limit)));
    }
```

Add `visualize: visualize,` to the object `makeClient` returns (beside `fetchEntities`).

- [ ] **Step 8: Run to verify it passes**

Run: `cd frontend && node tests/test_client.mjs`
Expected: PASS (`ok visualize client` + all existing).

- [ ] **Step 9: Commit — SKIPPED (no commits under xgraph/).**

---

### Task 5: `XGraph.html` — single-call FalkorDB Visualize branch + version bump

**Files:**
- Modify: `frontend/XGraph.html` (`handleVisualizeLoad` FalkorDB branch; version const)

**Interfaces:**
- Consumes: `gwClient.visualize(graph, limit)` and `window.xgraphGateway.graphTableFromConcise(...)` from Task 4; existing `graphCounts`, `bigGraphThreshold`, `setVizProgress`, `setGraphTableData`, `setVizLoadError`.
- Produces: none (leaf UI change).

- [ ] **Step 1: Replace the FalkorDB paging loop.** In `handleVisualizeLoad` (currently `XGraph.html:9743`–`9785`), replace the block that starts at `var allNodes = [];` and ends at the `finally { ... }` closing the `try` (the non-Kinetica path) with the single-call version:

```js
        var graphTotal = graphCounts.nodes || 0;
        var target = loadAll === true ? (graphTotal || bigGraphThreshold) : Math.min(graphTotal || bigGraphThreshold, bigGraphThreshold);
        var capped = graphTotal > 0 && target < graphTotal;
        setVizProgress({ loaded: 0, total: target, loading: true, capped: capped, graphTotal: graphTotal });
        try {
            // A+B fast path: ONE gateway call returns the whole capped subgraph
            // concise (index-pair edges); no per-page browser round trips.
            var concise = await gwClient.visualize(activeGraph, target);
            var gt = window.xgraphGateway.graphTableFromConcise(concise);
            setGraphTableData(gt);
            setVizProgress({
                loaded: gt.nodes.records.length, total: target, loading: false,
                capped: !!concise.capped,
                graphTotal: (typeof concise.total_nodes === 'number' ? concise.total_nodes : graphTotal),
            });
        } catch (err) {
            setVizLoadError(err.message);
            setVizProgress(function(p) { return Object.assign({}, p, { loading: false }); });
        }
```

Keep the `if (graphEngine === 'kinetica') { ... return; }` block above it EXACTLY as-is, and keep the `useCallback` dependency array line (`}, [activeGraph, gwClient, graphCounts, bigGraphThreshold, graphEngine, credentials]);`) unchanged.

- [ ] **Step 2: Verify the induced-subgraph filter and per-batch redraw code is fully removed** — the old `nodeIdSet`, `allEdges.filter`, `graphTableFromGateway`, and `while (allNodes.length < target)` lines must be gone from the FalkorDB branch (the server now does the induced filter; the transform is `graphTableFromConcise`). Grep to confirm:

Run: `cd frontend && grep -n "allNodes\|VISUALIZE_PAGE_SIZE" XGraph.html`
Expected: `VISUALIZE_PAGE_SIZE` may remain only in the top comment/const if still referenced elsewhere; `allNodes` should no longer appear in `handleVisualizeLoad`. (If `VISUALIZE_PAGE_SIZE` is now unused everywhere, leave the const — removing it is out of scope and risks an anchor mismatch.)

- [ ] **Step 3: Bump the version const** from `v0.23.0` to `v0.24.0`.

Run: `cd frontend && grep -n "v0.23.0" XGraph.html`
Then replace that exact string with `v0.24.0`.

- [ ] **Step 4: Validate the Babel transpile** (the app cannot be runtime-verified headlessly; transpile is the syntax gate).

Run: `cd frontend && node -e "const babel=require('@babel/standalone'); const fs=require('fs'); const html=fs.readFileSync('XGraph.html','utf8'); const m=html.match(/<script type=\"text\/babel\">([\s\S]*?)<\/script>/); babel.transform(m[1], {presets:['react']}); console.log('babel ok');"`
Expected: `babel ok` (no SyntaxError). If `@babel/standalone` isn't installed as a node module, instead open the page and rely on Step 5 + browser.

- [ ] **Step 5: Serve + curl 200** (restart the gateway so it serves the edited file).

Run: `cd /home/kkaramete/xgraph && ./xgraph restart && sleep 2 && curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8090/`
Expected: `200`.

- [ ] **Step 6: Re-run the frontend unit tests** (no regressions in the pure JS).

Run: `cd frontend && node tests/test_transforms.mjs && node tests/test_client.mjs`
Expected: all `ok` lines print, no throw.

- [ ] **Step 7: Commit — SKIPPED (no commits under xgraph/).**

---

## Final verification (whole feature)

- [ ] Backend: `cd backend && ./.venv/bin/python -m pytest tests/ -q` — all pass/skip.
- [ ] Frontend: `cd frontend && node tests/test_transforms.mjs && node tests/test_client.mjs` — all ok.
- [ ] Gateway restarted; `curl localhost:8090/` → 200; `curl 'localhost:8090/visualize?graph=<falkordb graph>&engine=falkordb&limit=1000'` returns the concise JSON.
- [ ] Browser acceptance (user-driven): FalkorDB Pull + Visualize on `banking_graph` is now fast; capped hint shows "first N of TOTAL"; N/E sliders still gate rendering.
