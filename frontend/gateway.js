// xgraph/frontend/gateway.js
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;   // Node (tests)
  else root.xgraphGateway = api;                                            // browser
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";
  var GATEWAY_DEFAULT = "http://localhost:8090";

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

  function makeClient(base, arg2, arg3) {
    // Backward-compatible signature: makeClient(base, engine, fetchImpl) [legacy]
    // or makeClient(base, fetchImpl) [session-aware]. Detect by type of arg2.
    var engine, fetchImpl;
    if (typeof arg2 === "string") {
      engine = arg2;
      fetchImpl = arg3;
    } else {
      engine = undefined;
      fetchImpl = arg2;
    }
    var f = fetchImpl || (typeof fetch !== "undefined" ? fetch : null);
    var session = null; // set once connect() resolves; overrides legacy `engine` when present

    var q = function (path) {
      var sep = path.indexOf("?") >= 0 ? "&" : "?";
      if (session) return base + path + sep + "session=" + encodeURIComponent(session);
      if (engine) return base + path + sep + "engine=" + encodeURIComponent(engine);
      return base + path;
    };

    // Merge session (preferred) or legacy engine into a POST body, without mutating the caller's object.
    function withSessionOrEngine(payload) {
      var out = {};
      for (var k in payload) if (Object.prototype.hasOwnProperty.call(payload, k)) out[k] = payload[k];
      if (session) out.session = session;
      else if (engine) out.engine = engine;
      return out;
    }

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
    async function postJSONWithAuth(path, payload) {
      return postJSON(path, withSessionOrEngine(payload));
    }

    return {
      connect: async function (graph, compute) {
        var res = await postJSON("/connect", { graph: graph, compute: compute });
        if (res && res.session) session = res.session;
        return res;
      },
      listGraphs: function () { return getJSON(q("/graphs")); },
      graphSizes: function () { return getJSON(q("/graph_sizes")); },
      getSchema: function (graph, opts) {
        var url = "/schema?graph=" + encodeURIComponent(graph);
        if (opts) {
          url += "&full=" + !!opts.full + "&nkey=" + !!opts.nkey + "&ekey=" + !!opts.ekey;
        }
        return getJSON(q(url));
      },
      runQuery: function (graph, cypher) { return postJSONWithAuth("/query", { graph: graph, cypher: cypher }); },
      fetchEntities: function (graph, limit, offset) { return getJSON(q("/entities?graph=" + encodeURIComponent(graph) + "&limit=" + (limit || 1000) + "&offset=" + (offset || 0))); },
      getRecord: function (graph, id) { return getJSON(q("/record?graph=" + encodeURIComponent(graph) + "&id=" + encodeURIComponent(id))); },
      hydrate: function (rows, source, key, columns) { return postJSONWithAuth("/hydrate", { rows: rows, source: source, key: key || "NODE", columns: columns || "*" }); },
      create: function (spec) { return postJSONWithAuth("/create", { spec: spec }); },
      ask: function (graph, question) { return postJSONWithAuth("/ask", { graph: graph, question: question }); },
      nl2cypher: function (graph, question) { return postJSONWithAuth("/nl2cypher", { graph: graph, question: question }); },
      synthesize: function (question, columns, rows, cypher) { return postJSONWithAuth("/synthesize", { question: question, columns: columns, rows: rows, cypher: cypher }); },
      explain: function (question, columns, rows, cypher, source) { return postJSONWithAuth("/explain", { question: question, columns: columns, rows: rows, cypher: cypher, source: source }); },
    };
  }

  return {
    GATEWAY_DEFAULT: GATEWAY_DEFAULT,
    tableFromGateway: tableFromGateway,
    graphTableFromGateway: graphTableFromGateway,
    recordFromGateway: recordFromGateway,
    makeClient: makeClient,
  };
});
