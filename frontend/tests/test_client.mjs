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

  // session-aware client: makeClient(base, fetchImpl) — no legacy engine arg
  const seenUrls = [];
  const seenBodies = [];
  const sessionClient = g.makeClient("http://gw", fakeFetch((url, body) => {
    seenUrls.push(url);
    if (body) seenBodies.push(body);
    if (url === "http://gw/connect" || url.startsWith("http://gw/connect")) {
      // postJSON always hits base + path with no query string appended
    }
    if (body && body.graph && body.compute) return { session: "s1", graphs: ["banking_graph"] };
    if (url.startsWith("http://gw/graphs")) return ["banking_graph"];
    if (url.startsWith("http://gw/query")) return { columns: ["NODE"], rows: [["b1"]] };
    if (url.startsWith("http://gw/hydrate")) return [{ NODE: body.rows[0].NODE, extra: 1 }];
    return {};
  }));

  const connectResult = await sessionClient.connect(
    { engine: "falkordb", conn: { host: "h", port: 6379 } },
    { engine: "duckdb", conn: {} }
  );
  assert.deepEqual(connectResult, { session: "s1", graphs: ["banking_graph"] });

  // /connect body carries {graph, compute} — no session/engine merged in
  const connectBody = seenBodies[seenBodies.length - 1];
  assert.deepEqual(connectBody.graph, { engine: "falkordb", conn: { host: "h", port: 6379 } });
  assert.deepEqual(connectBody.compute, { engine: "duckdb", conn: {} });

  await sessionClient.runQuery("g", "MATCH (n) RETURN n");
  const queryBody = seenBodies[seenBodies.length - 1];
  assert.equal(queryBody.session, "s1");

  await sessionClient.listGraphs();
  assert.equal(seenUrls[seenUrls.length - 1], "http://gw/graphs?session=s1");

  await sessionClient.hydrate([{ NODE: "b1" }], "src", "NODE", "*");
  const hydrateBody = seenBodies[seenBodies.length - 1];
  assert.equal(hydrateBody.session, "s1");

  // legacy path still works: makeClient(base, engine, fetchImpl) hits ?engine=
  const legacyClient = g.makeClient("http://gw", "fake", fakeFetch((url) => {
    seenUrls.push(url);
    return ["banking_graph"];
  }));
  assert.deepEqual(await legacyClient.listGraphs(), ["banking_graph"]);
  assert.equal(seenUrls[seenUrls.length - 1], "http://gw/graphs?engine=fake");

  console.log("client OK");
};
run();
