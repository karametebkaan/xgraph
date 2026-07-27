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

  console.log("test_kinetica_direct: OK");
};
run();
