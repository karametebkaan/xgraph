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
