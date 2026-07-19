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

  // A node/edge LABEL may arrive as an array (multi-label / faceted extraction,
  // e.g. ["Person","Engineer"]). The carried-over explorer renderers expect a
  // STRING and normalize with `v.charAt(0) === '[' ? JSON.parse(v) : v.split(', ')`
  // — passing a raw array makes them call `.split`/`.charAt` on an array and blow
  // up the whole view. Encode arrays as the JSON-array-STRING form the renderers
  // already parse; leave scalars untouched.
  function labelToString(v) {
    return Array.isArray(v) ? JSON.stringify(v) : v;
  }

  function graphTableFromGateway(res) {
    var nodes = (res && res.nodes) || [];
    var edges = (res && res.edges) || [];
    return {
      nodes: {
        records: nodes.map(function (n) { return { NODE_NAME: n.id, NODE_LABEL: labelToString(n.label) }; }),
        headers: ["NODE_NAME", "NODE_LABEL"], total: nodes.length,
      },
      edges: {
        records: edges.map(function (e) { return { NODE1_NAME: e.source, NODE2_NAME: e.target, EDGE_LABEL: labelToString(e.type) }; }),
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
    // props may carry an array LABEL (it overwrites the one above); coerce the
    // final value so the detail view never sees a raw array.
    out.LABEL = labelToString(out.LABEL);
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

    // Send BOTH session (backend-preferred) and engine (fallback if the session
    // is stale, e.g. after a gateway restart) so calls degrade gracefully.
    var q = function (path) {
      var sep = path.indexOf("?") >= 0 ? "&" : "?";
      var parts = [];
      if (session) parts.push("session=" + encodeURIComponent(session));
      if (engine) parts.push("engine=" + encodeURIComponent(engine));
      return parts.length ? base + path + sep + parts.join("&") : base + path;
    };

    // Merge session (preferred) AND engine (fallback) into a POST body, without mutating the caller's object.
    function withSessionOrEngine(payload) {
      var out = {};
      for (var k in payload) if (Object.prototype.hasOwnProperty.call(payload, k)) out[k] = payload[k];
      if (session) out.session = session;
      if (engine) out.engine = engine;
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
    async function postFormWithAuth(path, formData) {
      // Multipart: do NOT set Content-Type — the browser/runtime must set the
      // boundary itself. Session (preferred) AND engine (fallback), like postJSONWithAuth.
      if (session) formData.append("session", session);
      if (engine) formData.append("engine", engine);
      var res = await f(base + path, { method: "POST", body: formData });
      var body = await res.json();
      if (body && body.error) throw new Error(body.error.message || "gateway error");
      return body;
    }

    return {
      connect: async function (graph, compute, llm) {
        var res = await postJSON("/connect", { graph: graph, compute: compute, llm: llm });
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
      deleteGraph: function (graph) { return postJSONWithAuth("/delete_graph", { graph: graph }); },
      storage: function (graph) { return getJSON(q("/storage?graph=" + encodeURIComponent(graph))); },
      documents: function (graph) { return getJSON(q("/documents?graph=" + encodeURIComponent(graph))); },
      graphDdl: function (graph) { return getJSON(q("/graph_ddl?graph=" + encodeURIComponent(graph))); },
      sourcePreview: function (source) { return getJSON(q("/source_preview?source=" + encodeURIComponent(source))); },
      tables: function () { return getJSON(q("/tables")); },
      columns: function (table) { return getJSON(q("/columns?table=" + encodeURIComponent(table))); },
      grammar: function () { return getJSON(q("/grammar")); },
      ask: function (graph, question) { return postJSONWithAuth("/ask", { graph: graph, question: question }); },
      nl2cypher: function (graph, question) { return postJSONWithAuth("/nl2cypher", { graph: graph, question: question }); },
      synthesize: function (question, columns, rows, cypher) { return postJSONWithAuth("/synthesize", { question: question, columns: columns, rows: rows, cypher: cypher }); },
      explain: function (question, columns, rows, cypher, source, graph) { return postJSONWithAuth("/explain", { question: question, columns: columns, rows: rows, cypher: cypher, source: source, graph: graph }); },
      extract: function (graph, fileOrText, hint) {
        var formData = new FormData();
        var isFileLike = typeof Blob !== "undefined" && fileOrText instanceof Blob;
        if (isFileLike) formData.append("file", fileOrText, fileOrText.name || "document");
        else formData.append("text", fileOrText || "");
        formData.append("graph", graph);
        formData.append("hint", hint || "");
        return postFormWithAuth("/extract", formData);
      },
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
