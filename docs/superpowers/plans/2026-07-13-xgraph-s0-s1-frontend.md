# xgraph S0+S1 Frontend (neutralized explorer) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fork `explorer` into `xgraph/frontend/XGraph.html` and rewire its core data path to talk to the xgraph FastAPI gateway (Plan 1) instead of Kinetica directly, so the same UI renders a FalkorDB graph, Cypher results, an ontology DOT, and a DuckDB hydration pass — with `engine=` switchable (FalkorDB default, Kinetica for validation).

**Architecture:** The gateway returns clean shapes — `run_query → {columns, rows}`, `fetch_entities → {nodes, edges}`, `get_schema → {labels, rel_types, dot}`, `get_record → {id,label,props}`, `hydrate → [row,…]`. We do NOT make the gateway emulate Kinetica's `data_str`/`json_encoded_response`/`gql_result`/`entities_*` wire format (that would require synthesizing `NODE1_HOP_n` hop columns, a pure Kinetica-GQL artifact). Instead we introduce `frontend/gateway.js` — a gateway client + pure transforms that convert the clean shapes into the exact objects explorer's renderers already consume (`{headers,datatypes,rows}` for the Results tab; the `graphTableData` `{edges:{records},nodes:{records}}` shape for `CanvasGraph`; a DOT string for `OntologyViewer`). The renderers stay untouched.

**Tech Stack:** The existing explorer stack (React 18 UMD + Babel-standalone + force-graph + deck.gl + Graphviz-WASM, all via CDN, no build step). `gateway.js` is plain UMD JS (works in the browser via `<script>` and in Node via `require`/`import` for tests). Node ≥18 for the transform tests.

## Global Constraints

- **Do NOT `git commit` anything under `xgraph/`** — develop locally only. Every "checkpoint" step means save/verify locally, do not commit.
- **Do NOT modify `explorer/`** — it remains the Kinetica-only baseline. All work lands under `xgraph/frontend/`.
- The gateway base URL is `GATEWAY_BASE` (default `http://localhost:8088`, matching Plan 1's uvicorn port). The active engine is `engine` (default `falkordb`; `kinetica` selectable for validation).
- The hydration source path is `HYDRATE_SOURCE` (default `../falkor/data/vertexes.parquet`); it is a server-side path passed to the gateway, resolved by DuckDB there.
- Depends on Plan 1 being built and the gateway runnable (`uvicorn xgraph_gateway.app:app --port 8088`).
- Kinetica-specific UI features not needed for S1 (WMS tiles, Create/Solve/Match grammar helpers, geo MapView) are **guarded behind `engine === 'kinetica'`**, not deleted — their neutralization is S4.

---

### Task 1: Fork explorer + add gateway.js scaffold + config

**Files:**
- Create: `xgraph/frontend/XGraph.html` (copy of `explorer/KineticaGraphExplorer.html`)
- Create: `xgraph/frontend/gateway.js`
- Modify: `xgraph/frontend/XGraph.html` (add config constants + `<script src="gateway.js">`)

**Interfaces:**
- Produces: `gateway.js` exposing (browser) `window.xgraphGateway` and (Node) `module.exports` with, initially, `{ GATEWAY_DEFAULT: "http://localhost:8088" }`. Later tasks add transforms and the client.

- [ ] **Step 1: Copy the file**

Run:
```bash
mkdir -p xgraph/frontend/tests
cp explorer/KineticaGraphExplorer.html xgraph/frontend/XGraph.html
```
Expected: `xgraph/frontend/XGraph.html` exists (~605KB). `explorer/` unchanged (`git -C .. status` shows only untracked `xgraph/`).

- [ ] **Step 2: Create gateway.js UMD scaffold**

```javascript
// xgraph/frontend/gateway.js
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;   // Node (tests)
  else root.xgraphGateway = api;                                            // browser
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";
  var GATEWAY_DEFAULT = "http://localhost:8088";
  return { GATEWAY_DEFAULT: GATEWAY_DEFAULT };
});
```

- [ ] **Step 3: Load gateway.js and add config in XGraph.html**

Find the first CDN `<script>` block (around line 10, the React/Babel tags). Immediately AFTER those `<script>` tags and BEFORE the Babel `<script type="text/babel">` app block, add:

```html
<script src="gateway.js"></script>
```

Then find `const DEFAULT_PROFILES = [` (line 58) and immediately ABOVE it add:

```javascript
const GATEWAY_BASE = "http://localhost:8088";   // xgraph FastAPI gateway
const HYDRATE_SOURCE = "../falkor/data/vertexes.parquet";
```

- [ ] **Step 4: Verify it still loads**

Run: `cd xgraph/frontend && python3 -m http.server 8099 &` then open `http://localhost:8099/XGraph.html` (or `curl -s http://localhost:8099/XGraph.html | head -c 200`).
Expected: page HTML served; browser console shows no error from the `gateway.js` load; `window.xgraphGateway.GATEWAY_DEFAULT` is defined. Stop the server afterward.

- [ ] **Step 5: Checkpoint (no commit).**

---

### Task 2: Pure shape transforms (TDD, Node-tested)

**Files:**
- Modify: `xgraph/frontend/gateway.js`
- Test: `xgraph/frontend/tests/test_transforms.mjs`

**Interfaces:**
- Produces on `xgraphGateway`:
  - `tableFromGateway({columns, rows}) -> {headers: string[], datatypes: string[], rows: object[]}` — `rows` are objects keyed by column name (matches explorer's `parseColumnar` output so the Results tab is unchanged).
  - `graphTableFromGateway({nodes, edges}) -> graphTableData` — the exact shape `CanvasGraph` consumes: `{edges:{records,headers,total}, nodes:{records,headers,total}, edgeTable, nodeTable}` with node records `{NODE_NAME, NODE_LABEL}` and edge records `{NODE1_NAME, NODE2_NAME, EDGE_LABEL}`.
  - `recordFromGateway({id,label,props}) -> object` — flat `{NODE, LABEL, ...props}` for the node-detail key/value table.

- [ ] **Step 1: Write the failing test**

```javascript
// xgraph/frontend/tests/test_transforms.mjs
import assert from "node:assert";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const g = require("../gateway.js");

// tableFromGateway
{
  const t = g.tableFromGateway({ columns: ["NODE", "risk"], rows: [["b1", 90], ["b2", 40]] });
  assert.deepEqual(t.headers, ["NODE", "risk"]);
  assert.deepEqual(t.rows, [{ NODE: "b1", risk: 90 }, { NODE: "b2", risk: 40 }]);
  assert.deepEqual(g.tableFromGateway({ columns: [], rows: [] }), { headers: [], datatypes: [], rows: [] });
}
// graphTableFromGateway
{
  const gt = g.graphTableFromGateway({
    nodes: [{ id: "b1", label: "bank", props: {} }, { id: "w1", label: "wire_message", props: {} }],
    edges: [{ id: "e1", source: "b1", target: "w1", type: "performed" }],
  });
  assert.deepEqual(gt.nodes.records, [{ NODE_NAME: "b1", NODE_LABEL: "bank" },
                                      { NODE_NAME: "w1", NODE_LABEL: "wire_message" }]);
  assert.deepEqual(gt.edges.records, [{ NODE1_NAME: "b1", NODE2_NAME: "w1", EDGE_LABEL: "performed" }]);
  assert.deepEqual(gt.nodes.headers, ["NODE_NAME", "NODE_LABEL"]);
  assert.deepEqual(gt.edges.headers, ["NODE1_NAME", "NODE2_NAME", "EDGE_LABEL"]);
  assert.equal(gt.nodes.total, 2);
  assert.equal(gt.edges.total, 1);
}
// recordFromGateway
{
  assert.deepEqual(g.recordFromGateway({ id: "b1", label: "bank", props: { bank_name: "Acme" } }),
                   { NODE: "b1", LABEL: "bank", bank_name: "Acme" });
}
console.log("transforms OK");
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd xgraph/frontend && node tests/test_transforms.mjs`
Expected: FAIL — `TypeError: g.tableFromGateway is not a function`.

- [ ] **Step 3: Implement the transforms in gateway.js**

Replace the `return { GATEWAY_DEFAULT: GATEWAY_DEFAULT };` line with:

```javascript
  function tableFromGateway(res) {
    var columns = (res && res.columns) || [];
    var rows = (res && res.rows) || [];
    return {
      headers: columns,
      datatypes: [],
      rows: rows.map(function (r) {
        var o = {};
        for (var i = 0; i < columns.length; i++) o[columns[i]] = r[i];
        return o;
      }),
    };
  }

  function graphTableFromGateway(res) {
    var nodes = (res && res.nodes) || [];
    var edges = (res && res.edges) || [];
    return {
      nodes: {
        records: nodes.map(function (n) { return { NODE_NAME: n.id, NODE_LABEL: n.label }; }),
        headers: ["NODE_NAME", "NODE_LABEL"], total: nodes.length,
      },
      edges: {
        records: edges.map(function (e) { return { NODE1_NAME: e.source, NODE2_NAME: e.target, EDGE_LABEL: e.type }; }),
        headers: ["NODE1_NAME", "NODE2_NAME", "EDGE_LABEL"], total: edges.length,
      },
      edgeTable: "gateway (entities)", nodeTable: "gateway (entities/nodes)",
    };
  }

  function recordFromGateway(rec) {
    if (!rec || !rec.id) return null;
    var out = { NODE: rec.id, LABEL: rec.label };
    var props = rec.props || {};
    for (var k in props) if (Object.prototype.hasOwnProperty.call(props, k)) out[k] = props[k];
    return out;
  }

  return {
    GATEWAY_DEFAULT: GATEWAY_DEFAULT,
    tableFromGateway: tableFromGateway,
    graphTableFromGateway: graphTableFromGateway,
    recordFromGateway: recordFromGateway,
  };
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd xgraph/frontend && node tests/test_transforms.mjs`
Expected: prints `transforms OK`, exit 0.

- [ ] **Step 5: Checkpoint (no commit).**

---

### Task 3: Gateway client methods (TDD with a mocked fetch)

**Files:**
- Modify: `xgraph/frontend/gateway.js`
- Test: `xgraph/frontend/tests/test_client.mjs`

**Interfaces:**
- Produces `xgraphGateway.makeClient(base, engine)` returning an object with:
  - `listGraphs() -> Promise<string[]>` → `GET {base}/graphs?engine={engine}`
  - `getSchema(graph) -> Promise<{labels,rel_types,dot}>` → `GET /schema?engine=&graph=`
  - `runQuery(graph, cypher) -> Promise<{columns,rows}>` → `POST /query {engine,graph,cypher}`
  - `fetchEntities(graph, limit) -> Promise<{nodes,edges}>` → `GET /entities?engine=&graph=&limit=`
  - `getRecord(graph, id) -> Promise<{id,label,props}>` → `GET /record?engine=&graph=&id=`
  - `hydrate(rows, source, key, columns) -> Promise<object[]>` → `POST /hydrate {rows,source,key,columns}`
  - Each throws `Error(body.error.message)` when the response JSON has an `error` envelope.

- [ ] **Step 1: Write the failing test (inject a fake fetch)**

```javascript
// xgraph/frontend/tests/test_client.mjs
import assert from "node:assert";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const g = require("../gateway.js");

function fakeFetch(routes) {
  return async (url, opts) => ({
    ok: true,
    json: async () => routes(url, opts && opts.body ? JSON.parse(opts.body) : null),
  });
}

const client = g.makeClient("http://gw", "falkordb", fakeFetch((url, body) => {
  if (url === "http://gw/graphs?engine=falkordb") return ["banking_graph"];
  if (url.startsWith("http://gw/query")) return { columns: ["NODE"], rows: [["b1"]] };
  if (url.startsWith("http://gw/hydrate")) return [{ NODE: body.rows[0].NODE, extra: 1 }];
  return {};
}));

const run = async () => {
  assert.deepEqual(await client.listGraphs(), ["banking_graph"]);
  assert.deepEqual((await client.runQuery("banking_graph", "MATCH (n) RETURN n")).rows, [["b1"]]);
  assert.deepEqual(await client.hydrate([{ NODE: "b1" }], "v.parquet", "NODE", "*"),
                   [{ NODE: "b1", extra: 1 }]);

  // error envelope surfaces as a thrown Error
  const errClient = g.makeClient("http://gw", "falkordb",
    async () => ({ ok: true, json: async () => ({ error: { message: "boom" } }) }));
  await assert.rejects(() => errClient.listGraphs(), /boom/);
  console.log("client OK");
};
run();
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd xgraph/frontend && node tests/test_client.mjs`
Expected: FAIL — `g.makeClient is not a function`.

- [ ] **Step 3: Implement makeClient in gateway.js**

Add before the final `return {…}` and include `makeClient` in the returned object. The factory accepts an optional `fetchImpl` (defaults to global `fetch`) so tests can inject one:

```javascript
  function makeClient(base, engine, fetchImpl) {
    var f = fetchImpl || (typeof fetch !== "undefined" ? fetch : null);
    var q = function (path) { return base + path + (path.indexOf("?") >= 0 ? "&" : "?") + "engine=" + encodeURIComponent(engine); };
    async function getJSON(url) {
      var res = await f(url);
      var body = await res.json();
      if (body && body.error) throw new Error(body.error.message || "gateway error");
      return body;
    }
    async function postJSON(path, payload) {
      var res = await f(base + path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      var body = await res.json();
      if (body && body.error) throw new Error(body.error.message || "gateway error");
      return body;
    }
    return {
      listGraphs: function () { return getJSON(q("/graphs")); },
      getSchema: function (graph) { return getJSON(q("/schema?graph=" + encodeURIComponent(graph))); },
      runQuery: function (graph, cypher) { return postJSON("/query", { engine: engine, graph: graph, cypher: cypher }); },
      fetchEntities: function (graph, limit) { return getJSON(q("/entities?graph=" + encodeURIComponent(graph) + "&limit=" + (limit || 1000))); },
      getRecord: function (graph, id) { return getJSON(q("/record?graph=" + encodeURIComponent(graph) + "&id=" + encodeURIComponent(id))); },
      hydrate: function (rows, source, key, columns) { return postJSON("/hydrate", { rows: rows, source: source, key: key || "NODE", columns: columns || "*" }); },
    };
  }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd xgraph/frontend && node tests/test_client.mjs`
Expected: prints `client OK`.

- [ ] **Step 5: Checkpoint (no commit).**

---

### Task 4: Rewire connection (Sidebar → gateway base + engine picker)

**Files:**
- Modify: `xgraph/frontend/XGraph.html`

**Interfaces:**
- Consumes: `GATEWAY_BASE`, `xgraphGateway.makeClient`.
- Produces: App-level `engine` state (default `"falkordb"`), a module-level `gwClient` rebuilt when `engine` changes, and `handleConnect` that lists graphs from the gateway. The Sidebar's URL/user/pass profile inputs are replaced by an **engine dropdown** (`falkordb` / `kinetica`) plus an editable gateway-base field.

- [ ] **Step 1: Replace DEFAULT_PROFILES with engine profiles**

Find (line 58-61):
```javascript
const DEFAULT_PROFILES = [
    { label: "Localhost", url: "http://127.0.0.1:9191", user: "admin", pass: "Kinetica1!" },
    { label: "Demo72",   url: "https://demo72.kinetica.com/_gpudb", user: "", pass: "" },
];
```
Replace with:
```javascript
const ENGINE_PROFILES = [
    { label: "FalkorDB (gateway)", engine: "falkordb", base: GATEWAY_BASE },
    { label: "Kinetica (validation)", engine: "kinetica", base: GATEWAY_BASE },
];
```

- [ ] **Step 2: Add engine state and a gateway client in App**

Find (line 6674): `const [credentials, setCredentials]   = useState({ url:'', user:'', pass:'' });`
Add immediately below:
```javascript
    const [engine, setEngine] = useState("falkordb");
    const [gatewayBase, setGatewayBase] = useState(GATEWAY_BASE);
    const gwClient = useMemo(function(){ return window.xgraphGateway.makeClient(gatewayBase, engine); }, [gatewayBase, engine]);
```

- [ ] **Step 3: Rewire handleConnect to use the gateway**

Find `handleConnect` (lines 7096-7104). Replace its body with:
```javascript
    var handleConnect = async function(sel) {
        setEngine(sel.engine); setGatewayBase(sel.base);
        setActiveGraph(''); setLabelData(null); setDotString(null); setShowOntology(false);
        setMaximizedPanel(null); setGraphTableData(null); setShowForceGraph(false);
        var client = window.xgraphGateway.makeClient(sel.base, sel.engine);
        var names = await client.listGraphs();
        setGraphs(names);
        setCredentials({ url: sel.base, user: '', pass: '', engine: sel.engine });  // kept for prop compatibility
    };
```
Note: `buildGraphSizes`/`graphSizes` (Kinetica `/show/graph` size map) is not available from the gateway in S1 — set graph sizes to empty. Find the `setGraphSizes(buildGraphSizes(outer));` line (7104) and replace with `setGraphSizes({});`.

- [ ] **Step 4: Replace the Sidebar profile UI with an engine picker**

In `Sidebar` (state at lines 460-465, UI at 573-592): replace the `url/user/pass/showPass` state with:
```javascript
    const [profileIdx, setProfileIdx] = useState(0);
    const [base, setBase] = useState(ENGINE_PROFILES[0].base);
    const [status, setStatus] = useState({ text:'', error:false });
```
Replace `handleProfile` (478-486) and `handleConnect` (493-501) with:
```javascript
    var handleProfile = function(e) {
        var idx = parseInt(e.target.value); setProfileIdx(idx);
        setBase(ENGINE_PROFILES[idx].base);
        if (onClearGraphs) onClearGraphs();
    };
    var handleConnect = async function() {
        setStatus({ text:'Connecting…', error:false });
        try {
            await onConnect({ engine: ENGINE_PROFILES[profileIdx].engine, base: base.replace(/\/+$/, '') });
            setStatus({ text:'Connected.', error:false });
        } catch(err) { setStatus({ text: err.message, error:true }); }
    };
```
Replace the profile/URL/user/pass inputs (573-590) with an engine `<select>` (iterating `ENGINE_PROFILES`, `value={profileIdx}`, `onChange={handleProfile}`) and one text input bound to `base`/`setBase` labeled "Gateway URL". Keep the Connect button (592) calling `handleConnect`.

- [ ] **Step 5: Manual verify (needs gateway running)**

Run gateway: `cd ../backend && uvicorn xgraph_gateway.app:app --port 8088` (separate shell).
Serve frontend: `cd xgraph/frontend && python3 -m http.server 8099`; open `XGraph.html`, pick "FalkorDB (gateway)", Connect.
Expected: the graph list populates with `banking_graph` (and `demo`). Switching the dropdown to "Kinetica (validation)" and reconnecting lists Kinetica graphs.

- [ ] **Step 6: Checkpoint (no commit).**

---

### Task 5: Rewire query execution + Results/Visualization tabs

**Files:**
- Modify: `xgraph/frontend/XGraph.html`

**Interfaces:**
- Consumes: `gwClient`, `xgraphGateway.tableFromGateway`, `xgraphGateway.graphTableFromGateway`.
- Produces: `QueryPanel.executeQuery` calls `gwClient.runQuery(graph, cypher)` and stores `{columns, rows}`; `tableData` is computed via `tableFromGateway`; the Visualization tab renders the graph via `fetchEntities` → `graphTableFromGateway` → `CanvasGraph`. `gwClient` and `activeGraph` are passed as props to `QueryPanel`.

- [ ] **Step 1: Pass gwClient + activeGraph into QueryPanel**

At the `QueryPanel` render sites, add props `gwClient={gwClient}` and `graphName={activeGraph}`. (There are QueryPanel instances managed in a panels array; add the props wherever `<QueryPanel ... />` is instantiated in App.)

- [ ] **Step 2: Replace executeQuery to call the gateway**

Find `executeQuery` (lines 2564-2699). Replace its body with:
```javascript
    var executeQuery = async function(statement) {
        if (!props.gwClient || !props.graphName) { setError("Not connected to a graph."); return; }
        setError(null); setResult(null); setLoading(true); setNodeDetail(null); setCreateSuccess(null);
        try {
            var out = await props.gwClient.runQuery(props.graphName, statement);   // {columns, rows}
            setResult(out);
        } catch(err) { setError(err.message); }
        finally { setLoading(false); }
    };
```

- [ ] **Step 3: Replace the result-unwrap memos to consume the clean shape**

Find the unwrap memos (lines 2760-2793: `inner`, `tableData`, `graphParsed`). Replace with:
```javascript
    var tableData = useMemo(function() {
        if (!result) return { headers: [], datatypes: [], rows: [] };
        return window.xgraphGateway.tableFromGateway(result);
    }, [result]);
    // S1: per-query hop-graph viz (Kinetica NODE1_HOP_n artifact) is not produced by the gateway;
    // the Visualization tab renders the graph browse instead (see Step 4). Keep an empty stub so
    // downstream references do not break.
    var graphParsed = { headers: [], rows: [] };
```
Also neutralize `hasHops` (line 2795) → `var hasHops = false;` and `graphData`/`effectiveGraphData` (2927-2996) become unused for S1; leave them defined but guarded by `hasHops` so they produce empty graphs.

- [ ] **Step 4: Point the Visualization tab at the graph browse**

In App, replace `fetchGraphEntities`'s Kinetica `/get/graph/entities` body (rawRequest at 7521-7525 and the normalization) with a gateway call. Find `fetchGraphEntities` (starts ~7510) and replace its network+normalization core with:
```javascript
    var fetchGraphEntities = async function(graphName, opts) {
        var lim = (opts && opts.limit) || 1000;
        var res = await gwClient.fetchEntities(graphName, lim);          // {nodes, edges}
        setGraphTableData(window.xgraphGateway.graphTableFromGateway(res));
        setShowForceGraph(true);
    };
```
This yields the `graphTableData` shape `CanvasGraph` already consumes (verified: node records `{NODE_NAME,NODE_LABEL}`, edge records `{NODE1_NAME,NODE2_NAME,EDGE_LABEL}`).

- [ ] **Step 5: Manual verify (gateway running, live FalkorDB)**

Open `XGraph.html`, connect FalkorDB, select `banking_graph`, run:
`MATCH (b:bank)-[:performed]->(w:wire_message) WHERE w.wire_message_risk_score > 90 RETURN b.NODE AS NODE, b.bank_name AS bank_name, w.wire_message_risk_score AS risk ORDER BY risk DESC LIMIT 25`
Expected: Results tab shows a 25-row table (NODE, bank_name, risk). Trigger the graph browse (the whole-graph / entities view) → `CanvasGraph` renders a FalkorDB node-link graph. Capture a screenshot.

- [ ] **Step 6: Checkpoint (no commit).**

---

### Task 6: Rewire schema/ontology (DOT → OntologyViewer)

**Files:**
- Modify: `xgraph/frontend/XGraph.html`

**Interfaces:**
- Consumes: `gwClient.getSchema(graph)` → `{labels, rel_types, dot}`.
- Produces: selecting a graph sets `dotString` from `schema.dot` and `labelData` from `schema.labels`, so `OntologyViewer` (Graphviz-WASM) and the label list render.

- [ ] **Step 1: Replace the schema/ontology fetch**

Find the `/show/graph` calls that build the ontology DOT and label data (lines 7314 and 7381, and the graph-select handler that sets `dotString`/`labelData`). Replace the network body with:
```javascript
    var loadSchema = async function(graphName) {
        var sch = await gwClient.getSchema(graphName);       // {labels, rel_types, dot}
        setDotString(sch.dot || null);
        setLabelData({ labels: (sch.labels || []).map(function(l){ return { label: l }; }) });
    };
```
Call `loadSchema(graphName)` wherever a graph becomes active (the graph-list click handler that currently triggers the `/show/graph` ontology fetch).

- [ ] **Step 2: Manual verify**

Open `XGraph.html`, connect FalkorDB, click `banking_graph`, open the Ontology view.
Expected: Graphviz renders a DOT diagram with `bank`, `wire_message`, etc. nodes and `performed`/`is_for_transaction`/… edges; the label list shows the FalkorDB labels. Capture a screenshot.

- [ ] **Step 3: Checkpoint (no commit).**

---

### Task 7: Node-detail via get_record + Hydrate affordance

**Files:**
- Modify: `xgraph/frontend/XGraph.html`

**Interfaces:**
- Consumes: `gwClient.getRecord(graph, id)`, `gwClient.hydrate(rows, source, key, columns)`, `xgraphGateway.recordFromGateway`, `HYDRATE_SOURCE`.
- Produces: node click → `getRecord` fills the node-detail key/value table; a **Hydrate** button in the node-detail strip calls `hydrate` for that NODE id and merges the wide columns into the displayed record.

- [ ] **Step 1: Replace fetchNodeDetail (both copies) with getRecord**

Find `fetchNodeDetail` in QueryPanel (2131-2181) and in CanvasGraph (6073-6105). Replace each body with:
```javascript
    var fetchNodeDetail = async function(nodeId) {
        setNodeDetailLoading(true); setNodeDetail(null);
        try {
            var client = props.gwClient || gwClient;
            var rec = await client.getRecord(props.graphName || activeGraph, nodeId);
            setNodeDetail({ record: window.xgraphGateway.recordFromGateway(rec), nodeId: nodeId, table: (props.graphName || activeGraph) });
        } catch(err) { setNodeDetail({ record: null, nodeId: nodeId, error: err.message }); }
        finally { setNodeDetailLoading(false); }
    };
```
(CanvasGraph already receives `gwClient`/`graphName` via the props added in Task 5 Step 1 and Task 4; ensure `CanvasGraph` is passed `gwClient={gwClient}` and `graphName={activeGraph}` at its render sites lines 8780/8899.)

- [ ] **Step 2: Add a Hydrate button to the node-detail panel**

Find the node-detail key/value render (QueryPanel lines 3988-3998). Add above the key/value table:
```javascript
    React.createElement('button', {
        className: 'xg-hydrate-btn',
        disabled: !nodeDetail || !nodeDetail.nodeId,
        onClick: async function() {
            var client = props.gwClient || gwClient;
            var enriched = await client.hydrate([{ NODE: nodeDetail.nodeId }], HYDRATE_SOURCE, 'NODE', '*');
            if (enriched && enriched[0]) {
                setNodeDetail(Object.assign({}, nodeDetail, { record: Object.assign({}, nodeDetail.record, enriched[0]) }));
            }
        }
    }, 'Hydrate (DuckDB)')
```

- [ ] **Step 3: Manual verify (gateway running, Parquet present)**

Ensure `../falkor/data/vertexes.parquet` exists (Plan 1 Task 7). Open `XGraph.html`, connect FalkorDB, browse the graph, click a `bank` node → node-detail shows `NODE`, `LABEL`, `bank_name`, etc. Click **Hydrate (DuckDB)**.
Expected: the record gains columns that were never in the graph — e.g. `bank:bank_number`, `bank:created_date`. Capture a before/after screenshot.

- [ ] **Step 4: Checkpoint (no commit).**

---

### Task 8: Guard Kinetica-only features behind engine check

**Files:**
- Modify: `xgraph/frontend/XGraph.html`

**Interfaces:**
- Produces: the WMS renderer, the Create/Solve/Match grammar helpers, and the geo MapView paths are rendered/enabled only when `engine === 'kinetica'`; in FalkorDB mode they are hidden so their un-neutralized Kinetica `fetch(` calls never fire.

- [ ] **Step 1: Gate the grammar helpers**

Find the `/show/graph/grammar` fetch (line 7216) and the Create/Solve/Match helper UI. Wrap the helper-panel render and the grammar fetch in `if (engine === 'kinetica') { … }` (fetch) and `{engine === 'kinetica' && <HelperPanel .../>}` (render). In FalkorDB mode these panels are absent.

- [ ] **Step 2: Gate the geo/WMS renderer**

Find the renderer toggle (Auto/Canvas/Deck.gl/WMS) and the `MapView`/WMS component render (lines 5739/5758 fetches; render sites for `DeckMapView`/`MapView`). Force renderer to `Canvas` and hide the Deck.gl/WMS options when `engine !== 'kinetica'`. `CanvasGraph` (force-graph) is engine-neutral and remains the default.

- [ ] **Step 3: Manual verify**

Open `XGraph.html` in FalkorDB mode.
Expected: no Create/Solve/Match helper panels, no WMS/Deck.gl renderer options, no console errors from Kinetica-only fetches. Switch to Kinetica mode → those panels reappear (they still use the un-neutralized Kinetica paths; acceptable, S4 will route them through the gateway).

- [ ] **Step 4: Checkpoint (no commit).**

---

### Task 9: End-to-end S1 acceptance verification

**Files:**
- Create: `xgraph/frontend/tests/VERIFY.md` (manual verification script + captured evidence notes)

**Interfaces:**
- Consumes: running gateway (`:8088`), live FalkorDB with `banking_graph`, `../falkor/data/vertexes.parquet`.

- [ ] **Step 1: Write the verification checklist**

Create `xgraph/frontend/tests/VERIFY.md` listing the S1 acceptance criteria (spec §11) as manual steps: connect FalkorDB → graph list shows `banking_graph`; ontology DOT renders; run the bank→wire Cypher → table renders; graph browse → CanvasGraph node-link renders; click a bank node → Hydrate → `bank:bank_number` appears; switch to Kinetica engine → `banking_graph`/Kinetica graphs list (validation switch works). Each step records the observed result + a screenshot path.

- [ ] **Step 2: Run the full manual verification**

Start gateway + `python3 -m http.server` for the frontend; drive the UI through every step in VERIFY.md (use the `verify` skill's discipline: observe and capture, don't assume). Capture screenshots for the graph render, the ontology, and the before/after hydrate.
Expected: all S1 acceptance criteria pass; `explorer/` remains untouched (`git status` shows only untracked `xgraph/`).

- [ ] **Step 3: Run the Node transform/client tests once more**

Run: `cd xgraph/frontend && node tests/test_transforms.mjs && node tests/test_client.mjs`
Expected: `transforms OK` and `client OK`.

- [ ] **Step 4: Checkpoint (no commit).** Record evidence in VERIFY.md.

---

## Self-Review

**Spec coverage (S1 frontend, spec §11 criteria):**
- #1 list graphs → Task 4. ✓
- #2 schema + DOT in OntologyViewer → Task 6. ✓
- #3 run Cypher → table + force-graph node-link → Task 5 (table via `tableFromGateway`; node-link via `fetchEntities`→`CanvasGraph`). ✓ (Interpretation made explicit: the Visualization tab renders the graph browse, not a per-query hop-graph, because hop columns are a Kinetica-GQL artifact the gateway does not synthesize; per-query subgraph rendering is a documented fast-follow.)
- #4 hydrate surfaces `bank:bank_number` in node-detail → Task 7. ✓
- #5 carried-over UI on FalkorDB, no Kinetica → Tasks 1,4–8. ✓
- #7 explorer/ untouched → Global Constraints + Task 1/9 checks. ✓
- Kinetica validation switch preserved → Task 4 (engine picker) + Task 8 (Kinetica-only features gated, not deleted). ✓

**Placeholder scan:** transforms/client have complete code + tests; UI edits give the replacement code and the verbatim anchor (function name + line range) to find. UI edits are anchored by search, not frozen line numbers, because earlier edits shift lines — this is called out where it matters.

**Type consistency:** `tableFromGateway`/`graphTableFromGateway`/`recordFromGateway`/`makeClient` signatures and return shapes are identical across Tasks 2, 3, 5, 6, 7; the `graphTableData` shape produced in Task 5 matches what the Task-4 CanvasGraph props feed and what the extraction confirmed CanvasGraph consumes (`NODE_NAME`/`NODE_LABEL`, `NODE1_NAME`/`NODE2_NAME`/`EDGE_LABEL`).

**Known deferrals (not S1 gaps):** per-query hop-subgraph rendering; Kinetica-mode neutralization of WMS/grammar/geo (S4); graph-size map in the sidebar (Kinetica `/show/graph` only).
