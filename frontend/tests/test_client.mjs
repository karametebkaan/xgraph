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

  // extract(): text path sends multipart FormData (no JSON content-type), carries session
  let seenExtractUrl, seenExtractOpts;
  const extractClient = g.makeClient("http://gw", async (url, opts) => {
    if (url === "http://gw/connect") {
      return { ok: true, json: async () => ({ session: "s1", graphs: ["banking_graph"] }) };
    }
    seenExtractUrl = url;
    seenExtractOpts = opts;
    return { ok: true, json: async () => ({ graph: "g1", entities: 2, relations: 1,
      labels: { node_labels: ["Person"], edge_labels: ["KNOWS"] }, truncated: false }) };
  });
  await extractClient.connect({ engine: "falkordb", conn: {} }, { engine: "duckdb", conn: {} });
  const extractResult = await extractClient.extract("g1", "some pasted text", "focus hint");
  assert.equal(seenExtractUrl, "http://gw/extract");
  assert.equal(seenExtractOpts.method, "POST");
  assert.ok(seenExtractOpts.body instanceof FormData, "extract() must send FormData");
  assert.equal(seenExtractOpts.headers, undefined, "must not set Content-Type (browser sets multipart boundary)");
  assert.equal(seenExtractOpts.body.get("text"), "some pasted text");
  assert.equal(seenExtractOpts.body.get("graph"), "g1");
  assert.equal(seenExtractOpts.body.get("hint"), "focus hint");
  assert.equal(seenExtractOpts.body.get("session"), "s1");
  assert.equal(seenExtractOpts.body.has("file"), false);
  assert.deepEqual(extractResult, { graph: "g1", entities: 2, relations: 1,
    labels: { node_labels: ["Person"], edge_labels: ["KNOWS"] }, truncated: false });

  // extract(): file path sends a File/Blob under "file", no "text" field
  const fakeFile = new Blob(["hello"], { type: "text/plain" });
  fakeFile.name = "d.txt";
  await extractClient.extract("g1", fakeFile, null);
  assert.ok(seenExtractOpts.body.get("file") instanceof Blob);
  assert.equal(seenExtractOpts.body.has("text"), false);
  assert.equal(seenExtractOpts.body.get("hint"), "");

  // tables()/columns(): GET with engine carried; columns encodes the table param
  const tcUrls = [];
  const tcClient = g.makeClient("http://gw", "duckdb", fakeFetch((url) => { tcUrls.push(url); return []; }));
  assert.deepEqual(await tcClient.tables(), []);
  assert.deepEqual(await tcClient.columns("expero.vertexes"), []);
  assert.equal(tcUrls[0], "http://gw/tables?engine=duckdb");
  assert.equal(tcUrls[1], "http://gw/columns?table=expero.vertexes&engine=duckdb");

  // registerFile(): JSON POST carrying the path + session
  let regUrl, regBody;
  const regClient = g.makeClient("http://gw", async (url, opts) => {
    if (url === "http://gw/connect") return { ok: true, json: async () => ({ session: "s1", graphs: [] }) };
    regUrl = url; regBody = JSON.parse(opts.body);
    return { ok: true, json: async () => ({ name: "v.parquet", type: "file", columns: ["id"] }) };
  });
  await regClient.connect({ engine: "falkordb", conn: {} }, { engine: "duckdb", conn: {} });
  const reg = await regClient.registerFile("v.parquet");
  assert.equal(regUrl, "http://gw/register_file");
  assert.equal(regBody.path, "v.parquet");
  assert.equal(regBody.session, "s1");
  assert.deepEqual(reg.columns, ["id"]);
  console.log("ok: registerFile client method");

  console.log("client OK");
};
run();
