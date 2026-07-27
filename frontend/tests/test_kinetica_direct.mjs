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

  console.log("test_kinetica_direct: OK");
};
run();
