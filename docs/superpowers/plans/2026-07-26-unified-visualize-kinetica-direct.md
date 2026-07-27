# Unified Visualize Page (Kinetica-Direct) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Merge the `Ontology` and `Visualization` action-bar tabs into one `Visualize` page (ontology + force-graph + label donuts, resizable + maximizable) and make the Kinetica path fetch graph entities directly from Kinetica REST (explorer's fast typed-array path) instead of the slow gateway `/entities` paging.

**Architecture:** All Kinetica-direct fetch/decode logic goes into `frontend/gateway.js` as pure, Node-testable functions (injected `fetch`), matching xgraph's existing "gateway.js holds the client + pure transforms, Node-tested" pattern. `frontend/XGraph.html` gets a thin `engine === 'kinetica'` branch in `handleVisualizeLoad` that calls those helpers; every other engine keeps the existing gateway path. The unified Visualize render reuses the already-carried-over `SplitPane`, `CornerHandle`, `OntologyViewer`, `CanvasGraph`, and `LabelChart` components and the existing `maximizedPanel` state + Escape handler.

**Tech Stack:** React 18 UMD + Babel-standalone (no build step); `frontend/gateway.js` UMD module (browser + Node); Node's built-in test running via `node tests/*.mjs`; esbuild (`frontend/node_modules/esbuild`) for JSX syntax validation.

## Global Constraints

- **No backend changes.** Everything is in `frontend/gateway.js` and `frontend/XGraph.html`.
- **Kinetica-only speedup.** The direct-fetch branch fires only when `graphEngine === 'kinetica'`. FalkorDB and any other gateway engine keep the existing gateway `handleVisualizeLoad` path unchanged.
- **DOT stays gateway-sourced** for every engine (via `gwClient.getSchema()` in `loadSchema`). The new direct browser `show/graph` call is ONLY for `labeljson` donut counts — never for the DOT.
- **Non-geo only.** Decode `entities_int` / `entities_string` (payload_type `int`/`string`). Do NOT decode `entities_double` (WKT/geo). Geo/WMS/deck.gl and batched-for->300k-edges fetch are deferred.
- **Label index is 1-based** against the response's `labels` array; emit the JSON-array-string form (`'["Person"]'`) the carried-over renderers parse.
- **Record shapes must match `graphTableFromGateway`:** nodes `{NODE_NAME, NODE_LABEL}`, edges `{NODE1_NAME, NODE2_NAME, EDGE_LABEL}` — so `CanvasGraph` and the client-side label distributions consume them unchanged.
- **Basic auth** on every direct Kinetica POST: `Authorization: Basic base64(user + ':' + pass)`, sourced from the browser-held Kinetica connection (`graphConn`).
- **Bump `EXPLORER_VERSION`** (`frontend/XGraph.html:50`) as the final step so a stale browser cache is visible.
- **Never regress FalkorDB.** After the change, FalkorDB must still render the same merged page via the gateway path.

---

## File Structure

- `frontend/gateway.js` — MODIFY. Add pure Kinetica-direct helpers (`resolveLabelStr`, `decodeKineticaEntities`, `decodeKineticaConciseEdges`, `kineticaLabelData`, `kineticaFetchGraph`) + small utilities (`kbtoa`, `safeParse`, `kineticaPost`). Export the new public functions on the returned UMD object.
- `frontend/tests/test_kinetica_direct.mjs` — CREATE. Node unit tests for every new gateway.js helper, using an injected fake `fetch` (mirrors `tests/test_client.mjs`).
- `frontend/XGraph.html` — MODIFY. Credentials plumbing for Kinetica; unified donut memo; `handleVisualizeLoad` Kinetica branch; remove the "Max load" input; explorer-style big-graph confirm; `ACTIONS` merge; unified Visualize render (SplitPane + donuts) + maximize overlays; delete the standalone Ontology tab; bump `EXPLORER_VERSION`.

---

## Task 1: gateway.js — pure entity decoders + utilities

**Files:**
- Modify: `frontend/gateway.js` (add functions inside the `factory()` body, before `makeClient`)
- Test: `frontend/tests/test_kinetica_direct.mjs` (create)

**Interfaces:**
- Produces:
  - `resolveLabelStr(labelIdx, labels) → string` — 1-based index → `'["Label"]'` (or `''`).
  - `decodeKineticaEntities(outer, entityType) → Array` — `entityType === 'node'` → `[{NODE_NAME, NODE_LABEL}]` (stride 2 `[id, labelIdx]`); `'edge'` → `[{NODE1_NAME, NODE2_NAME, EDGE_LABEL}]` (stride 4 `[edgeId, n1, n2, labelIdx]`). Reads `outer.entities_int` when `outer.info.payload_type === 'int'`, else `outer.entities_string`.
  - `kbtoa(s) → string` (base64; `btoa` in browser, `Buffer` in Node).
  - `safeParse(s) → object|null`.

- [ ] **Step 1: Write the failing test**

Create `frontend/tests/test_kinetica_direct.mjs`:

```javascript
import assert from "node:assert";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const g = require("../gateway.js");

const run = async () => {
  // resolveLabelStr: 1-based, wraps bare names, passes through array-strings, '' out of range
  assert.equal(g.resolveLabelStr(1, ["Person", "Org"]), '["Person"]');
  assert.equal(g.resolveLabelStr(2, ["Person", "Org"]), '["Org"]');
  assert.equal(g.resolveLabelStr("1", ["Person"]), '["Person"]');
  assert.equal(g.resolveLabelStr(0, ["Person"]), "");
  assert.equal(g.resolveLabelStr(9, ["Person"]), "");
  assert.equal(g.resolveLabelStr(1, ['["Already"]']), '["Already"]');

  // decodeKineticaEntities: string nodes stride 2
  const nodesRes = { labels: ["Person", "Org"], info: { payload_type: "string" },
                     entities_string: ["alice", 1, "acme", 2] };
  assert.deepEqual(g.decodeKineticaEntities(nodesRes, "node"), [
    { NODE_NAME: "alice", NODE_LABEL: '["Person"]' },
    { NODE_NAME: "acme", NODE_LABEL: '["Org"]' },
  ]);

  // decodeKineticaEntities: string edges stride 4 [edgeId, n1, n2, labelIdx]
  const edgesRes = { labels: ["WORKS_AT"], info: { payload_type: "string" },
                     entities_string: [0, "alice", "acme", 1] };
  assert.deepEqual(g.decodeKineticaEntities(edgesRes, "edge"), [
    { NODE1_NAME: "alice", NODE2_NAME: "acme", EDGE_LABEL: '["WORKS_AT"]' },
  ]);

  // decodeKineticaEntities: int payload reads entities_int
  const intNodes = { labels: ["T"], info: { payload_type: "int" }, entities_int: [7, 1] };
  assert.deepEqual(g.decodeKineticaEntities(intNodes, "node"), [
    { NODE_NAME: 7, NODE_LABEL: '["T"]' },
  ]);

  // safeParse
  assert.deepEqual(g.safeParse('{"a":1}'), { a: 1 });
  assert.equal(g.safeParse("not json"), null);
  assert.equal(g.safeParse(null), null);

  // kbtoa round-trips through Node Buffer
  assert.equal(g.kbtoa("admin:pw"), Buffer.from("admin:pw", "utf-8").toString("base64"));

  console.log("test_kinetica_direct: OK");
};
run();
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && node tests/test_kinetica_direct.mjs`
Expected: FAIL — `g.resolveLabelStr is not a function` (helpers not exported yet).

- [ ] **Step 3: Write minimal implementation**

In `frontend/gateway.js`, inside the `factory()` body (after `labelToString`, before `graphTableFromGateway`), add:

```javascript
  function kbtoa(s) {
    if (typeof btoa === "function") return btoa(s);
    return Buffer.from(s, "utf-8").toString("base64");
  }

  function safeParse(s) {
    try { return typeof s === "string" ? JSON.parse(s) : null; } catch (e) { return null; }
  }

  // Resolve a 1-based label index against the response's `labels` array, emitting
  // the JSON-array-string form the carried-over renderers parse ('["Person"]').
  function resolveLabelStr(labelIdx, labels) {
    labels = labels || [];
    var idx = typeof labelIdx === "string" ? parseInt(labelIdx, 10) : labelIdx;
    if (idx > 0 && idx <= labels.length) {
      var lbl = labels[idx - 1];
      if (lbl && lbl.charAt(0) === "[") return lbl;
      return lbl ? '["' + lbl + '"]' : "";
    }
    return "";
  }

  // Decode one /get/graph/entities response body into flat records (non-geo).
  //   entityType 'node' → [{NODE_NAME, NODE_LABEL}]            (stride 2: [id, labelIdx])
  //   entityType 'edge' → [{NODE1_NAME, NODE2_NAME, EDGE_LABEL}] (stride 4: [eid, n1, n2, labelIdx])
  // Reads entities_int for payload_type 'int', else entities_string. entities_double
  // (WKT/geo) is intentionally NOT handled — geo is deferred.
  function decodeKineticaEntities(outer, entityType) {
    var labels = (outer && outer.labels) || [];
    var payloadType = (outer && outer.info && outer.info.payload_type) ||
                      (outer && outer.info && outer.info.identifier_type) || "string";
    var arr = payloadType === "int" ? ((outer && outer.entities_int) || [])
                                    : ((outer && outer.entities_string) || []);
    var recs = [];
    if (entityType === "node") {
      for (var i = 0; i + 1 < arr.length; i += 2) {
        recs.push({ NODE_NAME: arr[i], NODE_LABEL: resolveLabelStr(arr[i + 1], labels) });
      }
    } else {
      for (var j = 0; j + 3 < arr.length; j += 4) {
        recs.push({ NODE1_NAME: arr[j + 1], NODE2_NAME: arr[j + 2], EDGE_LABEL: resolveLabelStr(arr[j + 3], labels) });
      }
    }
    return recs;
  }
```

Then add all four to the returned UMD object (bottom `return { ... }`):

```javascript
    kbtoa: kbtoa,
    safeParse: safeParse,
    resolveLabelStr: resolveLabelStr,
    decodeKineticaEntities: decodeKineticaEntities,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && node tests/test_kinetica_direct.mjs`
Expected: `test_kinetica_direct: OK`

- [ ] **Step 5: Run the existing suite to confirm no regression**

Run: `cd frontend && node tests/test_transforms.mjs && node tests/test_client.mjs`
Expected: both print OK.

- [ ] **Step 6: Commit**

```bash
git add frontend/gateway.js frontend/tests/test_kinetica_direct.mjs
git commit -m "feat(viz): gateway.js pure Kinetica entity decoders (non-geo stride 2/4) + tests"
```

---

## Task 2: gateway.js — concise edge decoder

**Files:**
- Modify: `frontend/gateway.js`
- Test: `frontend/tests/test_kinetica_direct.mjs`

**Interfaces:**
- Consumes: `resolveLabelStr` (Task 1).
- Produces: `decodeKineticaConciseEdges(outer, nodeIds) → [{NODE1_NAME, NODE2_NAME, EDGE_LABEL}]`. Concise edges arrive in `entities_int` stride 4 `[edgeId, v0, v1, labelIdx]` where `v0`/`v1` are **integer indices** into `nodeIds` (the node array in fetch order). Resolves each to the node identifier via `nodeIds[v]` (empty string if out of range).

- [ ] **Step 1: Write the failing test**

Append to `frontend/tests/test_kinetica_direct.mjs` (inside `run`, before the final `console.log`):

```javascript
  // decodeKineticaConciseEdges: v0/v1 are indices into the node-id array
  const nodeIds = ["alice", "acme", "bob"];
  const conciseEdges = { labels: ["WORKS_AT", "KNOWS"], info: { payload_type: "int" },
                         entities_int: [0, 0, 1, 1, 1, 0, 2, 2] };
  assert.deepEqual(g.decodeKineticaConciseEdges(conciseEdges, nodeIds), [
    { NODE1_NAME: "alice", NODE2_NAME: "acme", EDGE_LABEL: '["WORKS_AT"]' },
    { NODE1_NAME: "alice", NODE2_NAME: "bob", EDGE_LABEL: '["KNOWS"]' },
  ]);
  // out-of-range index → ''
  assert.deepEqual(
    g.decodeKineticaConciseEdges({ labels: ["R"], info: { payload_type: "int" }, entities_int: [0, 9, 0, 1] }, nodeIds),
    [{ NODE1_NAME: "", NODE2_NAME: "alice", EDGE_LABEL: '["R"]' }]
  );
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && node tests/test_kinetica_direct.mjs`
Expected: FAIL — `g.decodeKineticaConciseEdges is not a function`.

- [ ] **Step 3: Write minimal implementation**

In `frontend/gateway.js`, after `decodeKineticaEntities`, add:

```javascript
  // Decode concise edges — entities_int stride 4 [edgeId, v0, v1, labelIdx] where
  // v0/v1 are integer indices into `nodeIds` (the node array in fetch order).
  function decodeKineticaConciseEdges(outer, nodeIds) {
    var labels = (outer && outer.labels) || [];
    var arr = (outer && outer.entities_int) || [];
    nodeIds = nodeIds || [];
    var recs = [];
    for (var j = 0; j + 3 < arr.length; j += 4) {
      var v0 = arr[j + 1], v1 = arr[j + 2];
      recs.push({
        NODE1_NAME: nodeIds[v0] != null ? nodeIds[v0] : "",
        NODE2_NAME: nodeIds[v1] != null ? nodeIds[v1] : "",
        EDGE_LABEL: resolveLabelStr(arr[j + 3], labels),
      });
    }
    return recs;
  }
```

Add to the UMD `return { ... }`: `decodeKineticaConciseEdges: decodeKineticaConciseEdges,`

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && node tests/test_kinetica_direct.mjs`
Expected: `test_kinetica_direct: OK`

- [ ] **Step 5: Commit**

```bash
git add frontend/gateway.js frontend/tests/test_kinetica_direct.mjs
git commit -m "feat(viz): gateway.js concise edge decoder (int-indexed connectivity) + tests"
```

---

## Task 3: gateway.js — label distribution from show/graph labeljson

**Files:**
- Modify: `frontend/gateway.js`
- Test: `frontend/tests/test_kinetica_direct.mjs`

**Interfaces:**
- Consumes: `kbtoa`, `safeParse` (Task 1).
- Produces:
  - `kineticaPost(fetchImpl, base, path, body, creds) → Promise<object>` — POSTs JSON with Basic-auth from `creds` (`{user, pass}`); throws on `!res.ok`.
  - `kineticaLabelData(base, graph, creds, opts) → Promise<{node_labels, edge_labels, total_labeled_nodes, total_labeled_edges}>` — POSTs `/show/graph {graph_name, options:{export_graph_schema:'true'}}`, reads `info.labeljson`, returns the explorer `labelData` shape. `opts.fetch` injects fetch for tests. On any failure returns `null` (caller falls back to record-derived donuts).

- [ ] **Step 1: Write the failing test**

Append to `frontend/tests/test_kinetica_direct.mjs` (inside `run`, before the final `console.log`):

```javascript
  // kineticaLabelData: parse labeljson from show/graph into labelData shape
  const labelJson = JSON.stringify({
    node_labels: [{ labels: ["Person"], count: 3 }, { labels: ["Org"], count: 1 }],
    edge_labels: [{ labels: ["WORKS_AT"], count: 2 }],
    total_labeled_nodes: 4, total_labeled_edges: 2,
  });
  let seenShowGraph = null;
  const fakeFetchLabels = async (url, opts) => {
    seenShowGraph = { url, body: JSON.parse(opts.body), headers: opts.headers };
    return { ok: true, json: async () => ({ info: { labeljson: labelJson } }) };
  };
  const ld = await g.kineticaLabelData("http://ki", "expero.banking_graph",
    { user: "admin", pass: "pw" }, { fetch: fakeFetchLabels });
  assert.deepEqual(ld.node_labels, [{ labels: ["Person"], count: 3 }, { labels: ["Org"], count: 1 }]);
  assert.deepEqual(ld.edge_labels, [{ labels: ["WORKS_AT"], count: 2 }]);
  assert.equal(ld.total_labeled_nodes, 4);
  assert.equal(seenShowGraph.url, "http://ki/show/graph");
  assert.equal(seenShowGraph.body.graph_name, "expero.banking_graph");
  assert.equal(seenShowGraph.body.options.export_graph_schema, "true");
  assert.equal(seenShowGraph.headers["Authorization"], "Basic " + g.kbtoa("admin:pw"));

  // kineticaLabelData: failure → null (caller falls back)
  const ldNull = await g.kineticaLabelData("http://ki", "g", {},
    { fetch: async () => ({ ok: false, status: 500, json: async () => ({}) }) });
  assert.equal(ldNull, null);
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && node tests/test_kinetica_direct.mjs`
Expected: FAIL — `g.kineticaLabelData is not a function`.

- [ ] **Step 3: Write minimal implementation**

In `frontend/gateway.js`, after `decodeKineticaConciseEdges`, add:

```javascript
  async function kineticaPost(fetchImpl, base, path, body, creds) {
    var headers = { "Content-Type": "application/json" };
    if (creds && (creds.user || creds.pass)) {
      headers["Authorization"] = "Basic " + kbtoa((creds.user || "") + ":" + (creds.pass || ""));
    }
    var res = await fetchImpl(base + path, { method: "POST", headers: headers, body: JSON.stringify(body) });
    if (!res.ok) throw new Error("HTTP " + res.status);
    return await res.json();
  }

  // Fetch per-label counts directly from Kinetica's show/graph labeljson and return
  // the explorer `labelData` shape. Returns null on any failure so the caller can
  // fall back to record-derived donut counts.
  async function kineticaLabelData(base, graph, creds, opts) {
    opts = opts || {};
    var fetchImpl = opts.fetch || (typeof fetch !== "undefined" ? fetch : null);
    try {
      var res = await kineticaPost(fetchImpl, base, "/show/graph",
        { graph_name: graph, options: { export_graph_schema: "true" } }, creds);
      var outer = safeParse(res.data_str) || res;
      var info = (outer && outer.info) || {};
      var parsed = safeParse(info.labeljson) || {};
      return {
        node_labels: parsed.node_labels || [],
        edge_labels: parsed.edge_labels || [],
        total_labeled_nodes: parsed.total_labeled_nodes || 0,
        total_labeled_edges: parsed.total_labeled_edges || 0,
        total_unlabeled_nodes: parsed.total_unlabeled_nodes || 0,
        total_unlabeled_edges: parsed.total_unlabeled_edges || 0,
      };
    } catch (e) {
      return null;
    }
  }
```

Add to the UMD `return { ... }`: `kineticaPost: kineticaPost,` and `kineticaLabelData: kineticaLabelData,`

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && node tests/test_kinetica_direct.mjs`
Expected: `test_kinetica_direct: OK`

- [ ] **Step 5: Commit**

```bash
git add frontend/gateway.js frontend/tests/test_kinetica_direct.mjs
git commit -m "feat(viz): gateway.js kineticaLabelData (show/graph labeljson → donut counts) + tests"
```

---

## Task 4: gateway.js — kineticaFetchGraph orchestration

**Files:**
- Modify: `frontend/gateway.js`
- Test: `frontend/tests/test_kinetica_direct.mjs`

**Interfaces:**
- Consumes: `kineticaPost`, `safeParse`, `decodeKineticaEntities`, `decodeKineticaConciseEdges` (Tasks 1-3).
- Produces: `kineticaFetchGraph(base, graph, creds, opts) → Promise<graphTableData>` where `graphTableData` is `{nodes:{records,headers,total}, edges:{records,headers,total}, nodeTable, edgeTable, identifierType}` — the exact shape `graphTableFromGateway` yields and `CanvasGraph` consumes. `opts.fetch` injects fetch; `opts.onProgress(kind, done)` optional (`kind` = `'node'|'edge'`). Fetches nodes then edges via `POST /get/graph/entities {graph_name, offset:0, limit:-1, options:{entity_type}}`; edges request `concise_edge_connectivity:'true'` and, when the response is concise (`info.concise_edge_connectivity === 'true'`), decode via `decodeKineticaConciseEdges` against the node-id array; otherwise decode plain via `decodeKineticaEntities`.

- [ ] **Step 1: Write the failing test**

Append to `frontend/tests/test_kinetica_direct.mjs` (inside `run`, before the final `console.log`):

```javascript
  // kineticaFetchGraph: plain (non-concise) edges
  const routesPlain = (url, body) => {
    if (url === "http://ki/get/graph/entities") {
      if (body.options.entity_type === "node")
        return { labels: ["Person", "Org"], info: { payload_type: "string" }, entities_string: ["alice", 1, "acme", 2] };
      // edges: plain stride 4, not concise
      return { labels: ["WORKS_AT"], info: { payload_type: "string" }, entities_string: [0, "alice", "acme", 1] };
    }
    return {};
  };
  const fetchPlain = async (url, opts) => ({ ok: true, json: async () => routesPlain(url, JSON.parse(opts.body)) });
  const gt = await g.kineticaFetchGraph("http://ki", "expero.banking_graph", { user: "admin", pass: "pw" }, { fetch: fetchPlain });
  assert.deepEqual(gt.nodes.records, [
    { NODE_NAME: "alice", NODE_LABEL: '["Person"]' },
    { NODE_NAME: "acme", NODE_LABEL: '["Org"]' },
  ]);
  assert.deepEqual(gt.edges.records, [
    { NODE1_NAME: "alice", NODE2_NAME: "acme", EDGE_LABEL: '["WORKS_AT"]' },
  ]);
  assert.equal(gt.nodes.total, 2);
  assert.equal(gt.edges.total, 1);

  // kineticaFetchGraph: concise edges (v0/v1 indices) resolve via node order
  const routesConcise = (url, body) => {
    if (url === "http://ki/get/graph/entities") {
      if (body.options.entity_type === "node")
        return { labels: ["Person", "Org"], info: { payload_type: "string" }, entities_string: ["alice", 1, "acme", 2] };
      return { labels: ["WORKS_AT"], info: { payload_type: "int", concise_edge_connectivity: "true" }, entities_int: [0, 0, 1, 1] };
    }
    return {};
  };
  const fetchConcise = async (url, opts) => ({ ok: true, json: async () => routesConcise(url, JSON.parse(opts.body)) });
  const gt2 = await g.kineticaFetchGraph("http://ki", "g", {}, { fetch: fetchConcise });
  assert.deepEqual(gt2.edges.records, [
    { NODE1_NAME: "alice", NODE2_NAME: "acme", EDGE_LABEL: '["WORKS_AT"]' },
  ]);
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && node tests/test_kinetica_direct.mjs`
Expected: FAIL — `g.kineticaFetchGraph is not a function`.

- [ ] **Step 3: Write minimal implementation**

In `frontend/gateway.js`, after `kineticaLabelData`, add:

```javascript
  // Fetch a graph's nodes+edges directly from Kinetica REST and return the
  // graphTableData shape CanvasGraph consumes. Single-request (limit:-1) per
  // entity type — the fast path explorer uses for non-huge graphs. Edges request
  // concise connectivity; when the server answers concise, v0/v1 indices are
  // resolved against the node-id array. Non-geo only.
  async function kineticaFetchGraph(base, graph, creds, opts) {
    opts = opts || {};
    var fetchImpl = opts.fetch || (typeof fetch !== "undefined" ? fetch : null);

    async function getRaw(entityType, extraOpts) {
      var options = { entity_type: entityType };
      if (extraOpts) for (var k in extraOpts) options[k] = extraOpts[k];
      var res = await kineticaPost(fetchImpl, base, "/get/graph/entities",
        { graph_name: graph, offset: 0, limit: -1, options: options }, creds);
      if (res && res.status === "ERROR") throw new Error(res.message || "get/graph/entities failed");
      return safeParse(res.data_str) || res;
    }

    var nodeOuter = await getRaw("node");
    var nodeRecs = decodeKineticaEntities(nodeOuter, "node");
    if (opts.onProgress) opts.onProgress("node", nodeRecs.length);

    var edgeOuter = await getRaw("edge", { concise_edge_connectivity: "true" });
    var isConcise = edgeOuter && edgeOuter.info && edgeOuter.info.concise_edge_connectivity === "true";
    var edgeRecs = isConcise
      ? decodeKineticaConciseEdges(edgeOuter, nodeRecs.map(function (n) { return n.NODE_NAME; }))
      : decodeKineticaEntities(edgeOuter, "edge");
    if (opts.onProgress) opts.onProgress("edge", edgeRecs.length);

    return {
      nodes: { records: nodeRecs, headers: ["NODE_NAME", "NODE_LABEL"], total: nodeRecs.length },
      edges: { records: edgeRecs, headers: ["NODE1_NAME", "NODE2_NAME", "EDGE_LABEL"], total: edgeRecs.length },
      nodeTable: graph + " (entities/nodes)", edgeTable: graph + " (entities)",
      identifierType: "string",
    };
  }
```

Add to the UMD `return { ... }`: `kineticaFetchGraph: kineticaFetchGraph,`

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && node tests/test_kinetica_direct.mjs`
Expected: `test_kinetica_direct: OK`

- [ ] **Step 5: Run the full frontend suite**

Run: `cd frontend && node tests/test_transforms.mjs && node tests/test_client.mjs && node tests/test_kinetica_direct.mjs`
Expected: all print OK.

- [ ] **Step 6: Commit**

```bash
git add frontend/gateway.js frontend/tests/test_kinetica_direct.mjs
git commit -m "feat(viz): gateway.js kineticaFetchGraph (direct REST → graphTableData, plain+concise) + tests"
```

---

## Task 5: XGraph.html — Kinetica credentials plumbing + unified donut source

**Files:**
- Modify: `frontend/XGraph.html` (credentials set at `:8697`; donut memos at `:9385-9420`; donut render at `:9825-9832`)

**Interfaces:**
- Consumes: `graphConn` (`{url,user,password}`, `:8133`), `graphEngine` (`:8132`), `labelData` (`:8274`), `nodeLabelDistribution`/`edgeLabelDistribution` (`:9385`, `:9405`).
- Produces: `credentials` carries the real Kinetica REST base + auth when `graphEngine === 'kinetica'`; `nodeDonut`/`edgeDonut` memos that prefer `labelData` counts (Kinetica labeljson) and fall back to record-derived distributions (FalkorDB).

**Context:** Today `credentials` (`:8697`) is set to the gateway base with empty user/pass ("kept for prop compatibility"). The direct fetch needs the Kinetica REST URL + basic-auth creds. And the two Visualize donuts currently read `nodeLabelDistribution.items` (record-derived); to show Kinetica's labeljson-weighted counts while keeping ONE render path for all engines, introduce a unified memo that uses `labelData` when it carries real per-label counts, else the record-derived distribution.

- [ ] **Step 1: Plumb Kinetica creds into `credentials`**

Find (`frontend/XGraph.html:8697`):

```javascript
        setCredentials({ url: sel.base, user: '', pass: '', engine: sel.engine });  // kept for prop compatibility
```

Replace with:

```javascript
        // For Kinetica, the Visualize page fetches graph entities directly from
        // the engine's REST endpoint (see handleVisualizeLoad's kinetica branch),
        // so credentials must carry the real Kinetica base + basic-auth creds.
        // Every other engine keeps the gateway-base stub (prop compatibility only).
        if (sel.engine === 'kinetica') {
            setCredentials({ url: graphConn.url || '', user: graphConn.user || '', pass: graphConn.password || '', engine: 'kinetica' });
        } else {
            setCredentials({ url: sel.base, user: '', pass: '', engine: sel.engine });  // kept for prop compatibility
        }
```

- [ ] **Step 2: Add the unified donut-source memos**

After the `edgeLabelDistribution` memo (`frontend/XGraph.html:9420`, immediately after its closing `}, [graphTableData]);`), add:

```javascript
    // Donut source, unified across engines (same render path for all): when
    // labelData carries real per-label counts (Kinetica's show/graph labeljson,
    // fetched in handleVisualizeLoad), use it so the donuts reflect the whole
    // graph; otherwise fall back to the record-derived distribution (FalkorDB,
    // whose gateway schema gives label names with count 0). Explorer's LabelChart
    // reads {labels, count} items, which both sources already emit.
    var nodeDonut = useMemo(function() {
        if (labelData && labelData.node_labels && labelData.node_labels.some(function(i){ return (i.count || 0) > 0; })) {
            var total = labelData.node_labels.reduce(function(s, i){ return s + (i.count || 0); }, 0);
            return { items: labelData.node_labels, total: total };
        }
        return nodeLabelDistribution;
    }, [labelData, nodeLabelDistribution]);
    var edgeDonut = useMemo(function() {
        if (labelData && labelData.edge_labels && labelData.edge_labels.some(function(i){ return (i.count || 0) > 0; })) {
            var total = labelData.edge_labels.reduce(function(s, i){ return s + (i.count || 0); }, 0);
            return { items: labelData.edge_labels, total: total };
        }
        return edgeLabelDistribution;
    }, [labelData, edgeLabelDistribution]);
```

- [ ] **Step 3: Point the two donuts at the unified source**

Find the two `LabelChart` mounts (`frontend/XGraph.html:9825-9832`) and change their `items`/`total` props:

```javascript
                                    <LabelChart
                                        title="Nodes by Label" items={nodeDonut.items} total={nodeDonut.total}
                                        pickingEnabled={true} selectedLabels={selectedNodeLabels} onToggleLabel={toggleSelectedNodeLabel}
                                    />
                                    <LabelChart
                                        title="Edges by Label" items={edgeDonut.items} total={edgeDonut.total}
                                        pickingEnabled={true} selectedLabels={selectedEdgeLabels} onToggleLabel={toggleSelectedEdgeLabel}
                                    />
```

(These two mounts move into the new layout in Task 7; the prop change here is what matters. If Task 7 is done first in a combined edit, apply the `nodeDonut`/`edgeDonut` props there.)

- [ ] **Step 4: Validate JSX transpiles and the app still serves**

Run (JSX syntax gate — extract the `text/babel` block and transform it):

```bash
cd frontend && node -e "const fs=require('fs'),es=require('esbuild');const h=fs.readFileSync('XGraph.html','utf8');const m=h.match(/<script type=\"text\/babel\">([\s\S]*?)<\/script>/);es.transform(m[1],{loader:'jsx'}).then(()=>console.log('JSX OK')).catch(e=>{console.error(e);process.exit(1)})"
```

Expected: `JSX OK`

- [ ] **Step 5: Commit**

```bash
git add frontend/XGraph.html
git commit -m "feat(viz): plumb Kinetica REST creds into credentials + unified donut source (labeljson or record-derived)"
```

---

## Task 6: XGraph.html — handleVisualizeLoad Kinetica branch + remove Max load + big-graph confirm

**Files:**
- Modify: `frontend/XGraph.html` (`handleVisualizeLoad` at `:9439-9477`; control strip at `:9764-9803`; `vizConfirm` state at `:8281`)

**Interfaces:**
- Consumes: `window.xgraphGateway.kineticaFetchGraph`, `window.xgraphGateway.kineticaLabelData` (Tasks 3-4); `graphEngine`, `credentials`, `activeGraph`, `graphCounts`, `bigGraphThreshold`, `vizConfirm`/`setVizConfirm` (`:8281`).
- Produces: a `handleVisualizeLoad` whose `graphEngine === 'kinetica'` path fetches directly from Kinetica and sets both `graphTableData` and `labelData`; all other engines keep the existing gateway paging path. The "Max load" number input is removed; a large graph triggers an explorer-style confirm before fetching.

- [ ] **Step 1: Add the Kinetica branch to `handleVisualizeLoad`**

Find the start of `handleVisualizeLoad` (`frontend/XGraph.html:9439-9445`):

```javascript
    var handleVisualizeLoad = useCallback(async function(loadAll) {
        if (!activeGraph || !gwClient) return;
        setVizLoadError(null);
        var allNodes = [];
        var allEdges = [];
        var target = loadAll === true ? (graphCounts.nodes || bigGraphThreshold) : Math.min(graphCounts.nodes || bigGraphThreshold, bigGraphThreshold);
        setVizProgress({ loaded: 0, total: target, loading: true });
```

Insert the Kinetica branch immediately after `setVizLoadError(null);` (before `var allNodes = [];`):

```javascript
        // Kinetica: fetch entities directly from the engine's REST endpoint
        // (fast typed-array path) instead of the gateway's fat-JSON paging. The
        // ontology DOT still comes from the gateway (loadSchema); only the graph
        // data + donut counts (labeljson) are browser-direct here.
        if (graphEngine === 'kinetica') {
            setVizProgress({ loaded: 0, total: (graphCounts.nodes || 0), loading: true });
            try {
                var kdata = await window.xgraphGateway.kineticaFetchGraph(
                    credentials.url, activeGraph, { user: credentials.user, pass: credentials.pass },
                    { onProgress: function(kind, done) {
                        if (kind === 'node') setVizProgress(function(p){ return Object.assign({}, p, { loaded: done }); });
                    } }
                );
                setGraphTableData(kdata);
                var kld = await window.xgraphGateway.kineticaLabelData(
                    credentials.url, activeGraph, { user: credentials.user, pass: credentials.pass });
                if (kld) setLabelData(kld);
            } catch (err) {
                setVizLoadError(err.message);
            } finally {
                setVizProgress(function(p) { return Object.assign({}, p, { loading: false }); });
            }
            return;
        }
```

Then add `graphEngine` and `credentials` to the `useCallback` dependency array (`:9477`):

```javascript
    }, [activeGraph, gwClient, graphCounts, bigGraphThreshold, graphEngine, credentials]);
```

- [ ] **Step 2: Remove the "Max load" input from the control strip**

Find (`frontend/XGraph.html:9765-9771`):

```javascript
                            <label style={{ fontWeight:600, display:'flex', alignItems:'center', gap:5, flexShrink:0 }}>
                                Max load
                                <input type="number" min="1" value={bigGraphThreshold} onChange={function(e){
                                    var v = parseInt(e.target.value, 10);
                                    setBigGraphThreshold(isNaN(v) ? 0 : v);
                                }} style={{ width:80, fontSize:11, padding:'2px 5px', border:'1px solid #dfe6e9', borderRadius:4, fontFamily:'inherit' }} />
                            </label>
```

Delete it (the Load/Refresh button and progress bar that follow stay).

- [ ] **Step 3: Replace the "Max load cap" warning with an explorer-style big-graph confirm**

Find the warning block (`frontend/XGraph.html:9792-9803`) and replace its inner copy so it no longer references a "max-load cap"; keep the "Continue — load all" button but re-word:

```javascript
                        {(graphCounts.nodes > bigGraphThreshold || graphCounts.edges > bigGraphThreshold) && (
                            <div style={{ flexShrink:0, padding:'4px 10px', background:'#fff3cd', color:'#856404', border:'1px solid #ffeeba', borderRadius:5, fontSize:11, fontWeight:600, display:'flex', alignItems:'center', flexWrap:'wrap', gap:6 }}>
                                <span>
                                    {'⚠ Large graph: ' + graphCounts.nodes.toLocaleString() + ' nodes / ' + graphCounts.edges.toLocaleString() +
                                     ' edges exceed the ' + bigGraphThreshold.toLocaleString() + ' threshold — visualizing may be slow.'}
                                </span>
                                <button onClick={function(){ handleVisualizeLoad(true); }} disabled={!activeGraph || vizProgress.loading} title="Load and visualize the whole graph" style={{
                                    padding:'2px 8px', border:'1px solid #856404', borderRadius:4, cursor: (!activeGraph || vizProgress.loading) ? 'not-allowed' : 'pointer',
                                    fontWeight:700, color:'#856404', background:'#fff', fontSize:10, fontFamily:'inherit', opacity: (!activeGraph || vizProgress.loading) ? 0.5 : 1, flexShrink:0, whiteSpace:'nowrap',
                                }}>Continue — visualize anyway</button>
                            </div>
                        )}
```

(The `bigGraphThreshold` state and its default remain — it is now set only via CanvasGraph's built-in threshold selector, not a panel input. No `VISUALIZE_PAGE_SIZE` change is required; the Kinetica branch bypasses it and the gateway path still uses it.)

- [ ] **Step 4: Validate JSX transpiles**

Run: (same esbuild command as Task 5 Step 4)
Expected: `JSX OK`

- [ ] **Step 5: Restart gateway + curl the app**

```bash
cd /home/kkaramete/xgraph && ./xgraph restart && sleep 2 && curl -s -o /dev/null -w "%{http_code}\n" localhost:8090/
```

Expected: `200`

- [ ] **Step 6: Commit**

```bash
git add frontend/XGraph.html
git commit -m "feat(viz): Kinetica-direct branch in handleVisualizeLoad; drop Max-load field; explorer-style big-graph confirm"
```

---

## Task 7: XGraph.html — merge Ontology into Visualize (SplitPane layout + maximize) and bump version

**Files:**
- Modify: `frontend/XGraph.html` (`ACTIONS` at `:6502-6510`; Visualize render at `:9746-9841`; Ontology render at `:9843-9853`; `EXPLORER_VERSION` at `:50`)

**Interfaces:**
- Consumes: `SplitPane` (`:526`), `CornerHandle` (`:470`), `OntologyViewer` (`:1015`, accepts `maximized`/`onToggleMaximize`), `CanvasGraph` (`:5891`, accepts `maximized`/`onToggleMaximize`), `LabelChart`; `maximizedPanel`/`setMaximizedPanel` (`:8430`) + Escape handler (`:8432-8438`); `hSplit`/`vSplit`/`rightVSplit` (`:8441-8443`); ontology props (`dotString`, `pickingEnabled`, `handleOntologyPick`, `ontologyApiRef`, `schemaFullSearch`/`schemaNodeLabelkeys`/`schemaEdgeLabelkeys` + their setters).
- Produces: one `Visualize` page with ontology + force-graph (left, vertical split) and the two donuts (right), corner-resizable, each of ontology/canvas maximizable to full viewport; the separate `Ontology` action is gone.

**Context:** This is the render-restructuring deliverable. It removes the `ontology` `ACTIONS` entry, deletes the standalone Ontology tab, and rebuilds the Visualize render to mirror explorer's `SplitPane` layout (explorer `KineticaGraphExplorer.html:8752-8829` for the split, `:8869-8907` for the maximize overlays — non-geo only).

- [ ] **Step 1: Remove the `ontology` entry from `ACTIONS`**

Find (`frontend/XGraph.html:6508`):

```javascript
            { key: 'ontology',  label: 'Ontology',  reachable: function(s) { return !!s.activeGraph; } },
```

Delete that line. (Keep the `visualize` entry above it.)

- [ ] **Step 2: Rebuild the Visualize render with the SplitPane layout**

Replace the `graphTableData ?` branch of the Visualize render (`frontend/XGraph.html:9804-9840`, from `{graphTableData ? (` through its matching `)}`) with the two-column split layout. The control strip (`:9764-9791`) and big-graph warning (`:9792-9803`, edited in Task 6) stay above it:

```javascript
                        {graphTableData ? (
                            <div style={{ flex:1, minHeight:0, position:'relative' }}>
                                <SplitPane direction="horizontal" split={hSplit} onSplitChange={setHSplit} minA={300} minB={250}>
                                    {/* LEFT: Ontology (top) over force-graph (bottom) */}
                                    <SplitPane direction="vertical" split={vSplit} onSplitChange={setVSplit} minA={80} minB={150}>
                                        <OntologyViewer
                                            dotString={dotString} pickingEnabled={pickingEnabled}
                                            onPickLabel={handleOntologyPick} apiRef={ontologyApiRef}
                                            schemaFullSearch={schemaFullSearch} onToggleSchemaFullSearch={function(){setSchemaFullSearch(function(p){return !p;});}}
                                            schemaNodeLabelkeys={schemaNodeLabelkeys} onToggleSchemaNodeLabelkeys={function(){setSchemaNodeLabelkeys(function(p){return !p;});}}
                                            schemaEdgeLabelkeys={schemaEdgeLabelkeys} onToggleSchemaEdgeLabelkeys={function(){setSchemaEdgeLabelkeys(function(p){return !p;});}}
                                            onToggleMaximize={function(){ setMaximizedPanel('ontology'); }}
                                        />
                                        <div style={{ minWidth:0, minHeight:0, display:'flex', height:'100%' }}>
                                            <CanvasGraph
                                                data={graphTableData} graphName={activeGraph} gwClient={gwClient}
                                                credentials={credentials} nodeSourceTable={nodeSourceTable} labelData={labelData}
                                                nodeLabelDist={nodeDonut.items} edgeLabelDist={edgeDonut.items}
                                                selectedNodeLabels={selectedNodeLabels} selectedEdgeLabels={selectedEdgeLabels}
                                                nodeLimit={canvasNodeLimit} onNodeLimitChange={setCanvasNodeLimit}
                                                edgeLimit={canvasEdgeLimit} onEdgeLimitChange={setCanvasEdgeLimit}
                                                bigGraphThreshold={bigGraphThreshold} onChangeBigGraphThreshold={setBigGraphThreshold}
                                                onVisualize={handleVisualizeLoad} vizLoading={vizProgress.loading}
                                                onToggleMaximize={function(){ setMaximizedPanel('canvas'); }}
                                            />
                                        </div>
                                    </SplitPane>
                                    {/* RIGHT: label donuts */}
                                    <div style={{ display:'flex', flexDirection:'column', gap:16, height:'100%', overflow:'auto', paddingLeft:4 }}>
                                        <SplitPane direction="vertical" split={rightVSplit} onSplitChange={setRightVSplit} minA={150} minB={150}>
                                            <LabelChart
                                                title="Nodes by Label" items={nodeDonut.items} total={nodeDonut.total}
                                                pickingEnabled={true} selectedLabels={selectedNodeLabels} onToggleLabel={toggleSelectedNodeLabel}
                                            />
                                            <LabelChart
                                                title="Edges by Label" items={edgeDonut.items} total={edgeDonut.total}
                                                pickingEnabled={true} selectedLabels={selectedEdgeLabels} onToggleLabel={toggleSelectedEdgeLabel}
                                            />
                                        </SplitPane>
                                    </div>
                                </SplitPane>
                                <CornerHandle hSplit={hSplit} vSplit={vSplit}
                                    onDrag={function(dx, dy){
                                        setHSplit(function(p){ return Math.max(0.15, Math.min(0.85, p + dx)); });
                                        setVSplit(function(p){ return Math.max(0.15, Math.min(0.85, p + dy)); });
                                    }} />
                            </div>
                        ) : (
                            <div style={{ textAlign:'center', color:'#636e72', margin:'auto' }}>
                                <h2 style={{ fontWeight:700, fontSize:20, margin:'0 0 8px' }}>Visualize</h2>
                                <p style={{ fontSize:14, color:'#b2bec3' }}>Use the Load button above to fetch this graph's nodes/edges and render it.</p>
                            </div>
                        )}
```

Note: verify `CornerHandle`'s `onDrag` signature against its definition (`frontend/XGraph.html:470`) and match it (explorer uses `onDrag(dx, dy)` as fractional deltas at `:8830`). If the carried-over `CornerHandle` expects a different callback shape, adapt this call to match — do NOT change `CornerHandle` itself.

- [ ] **Step 3: Delete the standalone Ontology render block**

Find and delete the entire `activeAction === 'ontology'` block (`frontend/XGraph.html:9843-9853`):

```javascript
                {activeAction === 'ontology' && (
                    <div style={{ width:'100%', height:'100%', alignSelf:'stretch', display:'flex', minHeight:0 }}>
                        <OntologyViewer
                            dotString={dotString} pickingEnabled={pickingEnabled}
                            onPickLabel={handleOntologyPick} apiRef={ontologyApiRef}
                            schemaFullSearch={schemaFullSearch} onToggleSchemaFullSearch={function(){setSchemaFullSearch(function(p){return !p;});}}
                            schemaNodeLabelkeys={schemaNodeLabelkeys} onToggleSchemaNodeLabelkeys={function(){setSchemaNodeLabelkeys(function(p){return !p;});}}
                            schemaEdgeLabelkeys={schemaEdgeLabelkeys} onToggleSchemaEdgeLabelkeys={function(){setSchemaEdgeLabelkeys(function(p){return !p;});}}
                        />
                    </div>
                )}
```

- [ ] **Step 4: Add the maximize overlays**

Immediately after the closing `</div>` of the action-content container (just before the two lines `</div>\n        </div>\n    );` at `:9854-9856`), add the two full-viewport overlays (mirroring explorer `:8869-8907`, non-geo only):

```javascript
                {maximizedPanel === 'ontology' && (
                    <div style={{ position:'fixed', top:0, left:0, width:'100vw', height:'100vh', zIndex:999, display:'flex', flexDirection:'column', background:'#fff' }}>
                        <OntologyViewer
                            dotString={dotString} pickingEnabled={pickingEnabled}
                            onPickLabel={handleOntologyPick} apiRef={ontologyApiRef}
                            schemaFullSearch={schemaFullSearch} onToggleSchemaFullSearch={function(){setSchemaFullSearch(function(p){return !p;});}}
                            schemaNodeLabelkeys={schemaNodeLabelkeys} onToggleSchemaNodeLabelkeys={function(){setSchemaNodeLabelkeys(function(p){return !p;});}}
                            schemaEdgeLabelkeys={schemaEdgeLabelkeys} onToggleSchemaEdgeLabelkeys={function(){setSchemaEdgeLabelkeys(function(p){return !p;});}}
                            maximized={true} onToggleMaximize={function(){ setMaximizedPanel(null); }}
                        />
                    </div>
                )}
                {maximizedPanel === 'canvas' && graphTableData && (
                    <div style={{ position:'fixed', top:0, left:0, width:'100vw', height:'100vh', zIndex:999, display:'flex', flexDirection:'column', background:'#fff' }}>
                        <CanvasGraph
                            data={graphTableData} graphName={activeGraph} gwClient={gwClient}
                            credentials={credentials} nodeSourceTable={nodeSourceTable} labelData={labelData}
                            nodeLabelDist={nodeDonut.items} edgeLabelDist={edgeDonut.items}
                            selectedNodeLabels={selectedNodeLabels} selectedEdgeLabels={selectedEdgeLabels}
                            nodeLimit={canvasNodeLimit} onNodeLimitChange={setCanvasNodeLimit}
                            edgeLimit={canvasEdgeLimit} onEdgeLimitChange={setCanvasEdgeLimit}
                            bigGraphThreshold={bigGraphThreshold} onChangeBigGraphThreshold={setBigGraphThreshold}
                            onVisualize={handleVisualizeLoad} vizLoading={vizProgress.loading}
                            maximized={true} onToggleMaximize={function(){ setMaximizedPanel(null); }}
                        />
                    </div>
                )}
```

Confirm placement is inside the App's returned root `<div>` (same level as the other overlays/panels) so `maximizedPanel` state is in scope — match how the surrounding blocks are nested.

- [ ] **Step 5: Bump `EXPLORER_VERSION`**

Find (`frontend/XGraph.html:50`):

```javascript
const EXPLORER_VERSION = "0.18.9";  // xGraph version — bump on frontend changes so a stale browser cache is visible
```

Change to `"0.19.0"`.

- [ ] **Step 6: Validate — JSX transpile, full node suite, gateway 200**

```bash
cd frontend && node -e "const fs=require('fs'),es=require('esbuild');const h=fs.readFileSync('XGraph.html','utf8');const m=h.match(/<script type=\"text\/babel\">([\s\S]*?)<\/script>/);es.transform(m[1],{loader:'jsx'}).then(()=>console.log('JSX OK')).catch(e=>{console.error(e);process.exit(1)})"
cd /home/kkaramete/xgraph && node frontend/tests/test_transforms.mjs && node frontend/tests/test_client.mjs && node frontend/tests/test_kinetica_direct.mjs
./xgraph restart && sleep 2 && curl -s -o /dev/null -w "%{http_code}\n" localhost:8090/
```

Expected: `JSX OK`, three test-suite OK lines, `200`.

- [ ] **Step 7: Commit**

```bash
git add frontend/XGraph.html
git commit -m "feat(viz): merge Ontology into Visualize (SplitPane ontology+graph+donuts, maximize/restore); v0.19.0"
```

---

## Manual Acceptance (user-driven, after all tasks)

The React app cannot be runtime-verified headlessly. After Task 7, the user opens `http://localhost:8090/`, connects Kinetica, and checks:

1. There is a single **Visualize** action (no separate Ontology tab).
2. On `expero.banking_graph` and `extracted_graph_kinetica1`: ontology + force-graph + donuts render on one page; the force-graph load is fast (direct Kinetica fetch).
3. The horizontal/vertical splits drag-resize; the corner handle works; ontology and the force-graph each maximize to full window and restore (button or Esc).
4. Donuts show weighted per-label counts (from labeljson).
5. Switching to FalkorDB still renders the same page via the gateway path (no regression).

---

## Self-Review Notes

- **Spec coverage:** Section 1 (unified page + resize/maximize) → Tasks 5-7. Section 2 (Kinetica-direct fetch, plain + concise, labeljson donuts, creds) → Tasks 1-6. Section 3 (no Max-load, big-graph confirm, geo deferred, validation) → Task 6 + validation steps. Section 4 (engine matrix: DOT gateway-sourced, donuts browser-direct for Kinetica / record-derived for FalkorDB) → Task 5 unified donut memo + Task 6 branch (DOT untouched via loadSchema).
- **Deferred (out of scope, matching spec):** geo/WMS/deck.gl + `entities_double` decode; batched paging for >300k-edge graphs (single `limit:-1` request is used — Kinetica serves it, and explorer only batches to drive progressive geo drawing, which we don't do). If a future graph is large enough that one request is impractical, add a batched path reusing `decodeKineticaConciseEdges`.
- **Type consistency:** helper names (`kineticaFetchGraph`, `kineticaLabelData`, `decodeKineticaEntities`, `decodeKineticaConciseEdges`, `resolveLabelStr`) are used identically in tests and in the XGraph.html branch; record shapes (`NODE_NAME`/`NODE_LABEL`, `NODE1_NAME`/`NODE2_NAME`/`EDGE_LABEL`) match `graphTableFromGateway` and the `nodeDonut`/`edgeDonut` memos.
