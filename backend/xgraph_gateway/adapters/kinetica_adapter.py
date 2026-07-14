from __future__ import annotations
import json
import re
from gpudb import GPUdb
from xgraph_gateway import config
from .base import GraphEngineAdapter
from graph_loader.kinetica_source import KineticaSource
from graph_loader.mapper import safe_ident

def _rows_to_result(rows: list[dict]) -> dict:
    cols = list(rows[0].keys()) if rows else []
    return {"columns": cols, "rows": [list(r.values()) for r in rows]}

# Matches explorer's own single-shot "Visualization" tab call (limit: 10000,
# offset: 0) -- see KineticaGraphExplorer.html's executeQuery().
_GQL_GRAPH_LIMIT = 10000

def _is_gql_graph_query(statement: str) -> bool:
    """Kinetica GQL graph traversal queries contain a `GRAPH <graph_name>
    MATCH ...` clause -- either as the whole statement, or nested inside a
    `SELECT ... FROM graph_table(GRAPH ... MATCH ...)` wrapper. Used to gate
    the extra execute_sql_and_decode call in `_query_graph` so a plain SQL
    statement (SELECT/INSERT/DDL/...) is never re-executed -- idempotency of
    a second run can't be assumed for non-GQL SQL."""
    return bool(re.search(r'\bgraph\s+["A-Za-z0-9_.]+\s+match\b', statement, re.IGNORECASE))

def _validate_table_ident(ident: str) -> str:
    # Table names may be schema-qualified (e.g. "expero.vertexes"); safe_ident
    # rejects dots, so validate each dot-separated part individually.
    for part in str(ident).split("."):
        safe_ident(part)
    return ident

def _dot_from_show_graph(resp) -> str:
    """Pull the server-side ontology DOT out of a `show_graph(...,
    options={'export_graph_schema': 'true'})` response.

    Kinetica returns it at `resp['info']['dot']` -- a graphviz `digraph`
    string with one node per label (annotated with its share of the node
    population) and one edge per relationship label. Returns "" if absent
    (e.g. `export_graph_schema` wasn't requested, or the graph has no data).
    """
    info = resp.get("info") or {}
    dot = info.get("dot")
    return dot if isinstance(dot, str) and dot.strip() else ""

def _labels_from_show_graph(resp) -> tuple[list[str], list[str]]:
    """Pull node/edge label names out of `resp['info']['labeljson']`, a JSON
    string shaped like `{"node_labels": [{"labels": [...], "count": n}, ...],
    "edge_labels": [...]}`. Returns `([], [])` if the field is absent or
    unparseable -- callers fall back to an empty list, never raise.
    """
    info = resp.get("info") or {}
    raw = info.get("labeljson")
    if not raw:
        return ([], [])
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return ([], [])
    labels = [l for entry in parsed.get("node_labels", []) for l in entry.get("labels", [])]
    rel_types = [l for entry in parsed.get("edge_labels", []) for l in entry.get("labels", [])]
    return (labels, rel_types)

def _counts_from_show_graph(resp) -> dict:
    """Pull total node/edge counts out of `resp['info']['labeljson']` (see
    `_labels_from_show_graph`) by summing each label group's `count`. Best
    effort -- returns `{"nodes": 0, "edges": 0}` if the field is absent or
    unparseable, never raises.
    """
    info = resp.get("info") or {}
    raw = info.get("labeljson")
    if not raw:
        return {"nodes": 0, "edges": 0}
    try:
        parsed = json.loads(raw)
        nodes = sum(entry.get("count", 0) for entry in parsed.get("node_labels", []))
        edges = sum(entry.get("count", 0) for entry in parsed.get("edge_labels", []))
    except (TypeError, ValueError, AttributeError):
        return {"nodes": 0, "edges": 0}
    return {"nodes": nodes, "edges": edges}

def _backing_tables(resp) -> tuple[str | None, str | None]:
    """Discover the vertex/edge backing table names for a graph from
    `resp['original_request']` -- a JSON-encoded copy of the original
    `create ... graph ...` DDL statement (`resp['original_request'][0]` is a
    JSON string with a `"statement"` field). The statement shape is:

        create or replace directed graph <name> (
            nodes => INPUT_TABLES((SELECT ... FROM <vtable>)),
            edges => INPUT_TABLES((SELECT ... FROM <etable>)),
            ...
        );

    Returns `(vtable, etable)`, either/both `None` if not found -- callers
    must treat that as "backing tables unknown" and return empty results,
    never raise.
    """
    try:
        raw_list = resp.get("original_request") or []
        if not raw_list:
            return (None, None)
        statement = json.loads(raw_list[0])["statement"]
    except (TypeError, ValueError, KeyError, IndexError):
        return (None, None)

    def _first_from(section_keyword: str) -> str | None:
        m = re.search(rf"{section_keyword}\s*=>.*?\bFROM\b\s+([A-Za-z0-9_.]+)",
                      statement, re.IGNORECASE | re.DOTALL)
        return m.group(1) if m else None

    return (_first_from("nodes"), _first_from("edges"))

def _escape_sql_literal(value) -> str:
    """Escape a value for interpolation as a single-quoted SQL string literal
    (double the single quotes -- the SQL-standard escape). Kinetica SQL has no
    parameterized-query path through KineticaSource, so string literals must
    be escaped, never raw-interpolated.
    """
    return str(value).replace("'", "''")

def _hop_indices(headers: list[str]) -> list[int]:
    """Column headers like NODE1_HOP_1, EDGE_LABELS_HOP_2, ... -> sorted [1, 2, ...]
    (the number of graph-traversal hops present in a gql_result)."""
    hops = set()
    for h in headers:
        if "_HOP_" in h:
            try:
                hops.add(int(h.rsplit("_HOP_", 1)[1]))
            except ValueError:
                continue
    return sorted(hops)

def _first_label(raw) -> str | None:
    """A LABELS column value is either a JSON array string (`'["bank"]'`) or a
    bare label string; return the first label, or None if empty/unparseable."""
    if not raw:
        return None
    if isinstance(raw, str) and raw.startswith("["):
        try:
            labels = json.loads(raw)
        except (TypeError, ValueError):
            return raw
        return labels[0] if labels else None
    return raw

def graph_from_gql_result(gql_result: dict) -> dict:
    """Port of the Kinetica Graph Explorer's "Visualization" transform
    (KineticaGraphExplorer.html, the gql_result hop-based path columns) to
    Python. `gql_result` is the parsed `resp.info['gql_result']` dict --
    `{"column_headers": [...], "column_datatypes": [...], "column_1": [...],
    ...}` -- with columns named `NODE1_HOP_n`/`NODE2_HOP_n` (+ their
    `_LABELS_HOP_n` siblings) and `EDGE_LABELS_HOP_n` for each hop n of the
    traversal. Builds one node per distinct NODE1/NODE2 id and one edge per
    (src, dst, edge_label) triple across every hop and every row, de-duped by
    id. Never raises -- any parse error yields {"nodes": [], "edges": []}.
    """
    try:
        headers = gql_result.get("column_headers") or []
        columns = {h: (gql_result.get(f"column_{i}") or [])
                   for i, h in enumerate(headers, start=1)}
        nodes: dict = {}
        edges: dict = {}
        for hop in _hop_indices(headers):
            n1 = columns.get(f"NODE1_HOP_{hop}")
            n2 = columns.get(f"NODE2_HOP_{hop}")
            if not n1 or not n2:
                continue
            n1_labels = columns.get(f"NODE1_LABELS_HOP_{hop}") or []
            n2_labels = columns.get(f"NODE2_LABELS_HOP_{hop}") or []
            e_labels = columns.get(f"EDGE_LABELS_HOP_{hop}") or []
            for i in range(min(len(n1), len(n2))):
                src, dst = n1[i], n2[i]
                src_label = _first_label(n1_labels[i]) if i < len(n1_labels) else None
                dst_label = _first_label(n2_labels[i]) if i < len(n2_labels) else None
                edge_label = _first_label(e_labels[i]) if i < len(e_labels) else None
                if src not in nodes:
                    nodes[src] = {"id": src, "label": src_label, "props": {}}
                if dst not in nodes:
                    nodes[dst] = {"id": dst, "label": dst_label, "props": {}}
                edge_id = f"{src}->{dst}|{edge_label}"
                edges[edge_id] = {"id": edge_id, "source": src, "target": dst, "type": edge_label}
        return {"nodes": list(nodes.values()), "edges": list(edges.values())}
    except Exception:
        return {"nodes": [], "edges": []}

def _row_to_record(row: dict, node_id) -> dict:
    """Shape one backing-table row into the {"id","label","props"} contract
    shared with FalkorDBAdapter.get_record. `props` is the *entire* row --
    picking a node needs every attribute column, not just id/label. Falls
    back to the caller's `node_id` for "id" and None for "label" if either
    column is absent from the row (schema drift), never raises.
    """
    return {
        "id": row.get("id", node_id),
        "label": row.get("label"),
        "props": dict(row),
    }

class KineticaAdapter(GraphEngineAdapter):
    def __init__(self, settings=None, conn=None):
        if conn is not None:
            url = conn["url"]
            user = conn.get("user")
            password = conn.get("password")
        else:
            url = settings.kinetica_url
            user = settings.kinetica_user
            password = settings.kinetica_pass
        self._db = GPUdb(host=url, username=user, password=password)
        self._src = KineticaSource(self._db)

    def list_graphs(self):
        resp = self._db.show_graph(graph_name="")
        return list(resp.get("graph_names", []))

    def run_query(self, graph, cypher, timeout=60000):
        # `cypher` here is Kinetica SQL/GQL (engine-appropriate validation query).
        result = _rows_to_result(list(self._src.rows(cypher)))
        result["graph"] = self._query_graph(cypher)
        return result

    def _query_graph(self, statement: str) -> dict:
        """Best-effort path/graph extraction for the QueryPanel viz. Only a
        GQL `GRAPH ... MATCH ...` query populates `resp.info['gql_result']`
        (the Explorer's hop-path columns -- see graph_from_gql_result); a
        plain SQL statement leaves it empty/absent. One extra bounded
        (offset=0, limit=_GQL_GRAPH_LIMIT) execute_sql_and_decode call --
        mirrors the single-shot `limit: 10000` call the Kinetica Graph
        Explorer itself makes to render its "Visualization" tab. Never
        raises -- any failure (including re-running a DDL/DML statement)
        yields an empty graph; `columns`/`rows` from the primary paginated
        read above are unaffected either way.
        """
        if not _is_gql_graph_query(statement):
            return {"nodes": [], "edges": []}
        try:
            resp = self._db.execute_sql_and_decode(
                statement, offset=0, limit=_GQL_GRAPH_LIMIT, get_column_major=False)
            info = resp.info if hasattr(resp, "info") else resp.get("info") or {}
            raw = info.get("gql_result") if info else None
            if not raw:
                return {"nodes": [], "edges": []}
            gql_result = json.loads(raw) if isinstance(raw, str) else raw
            return graph_from_gql_result(gql_result)
        except Exception:
            return {"nodes": [], "edges": []}

    def get_schema(self, graph, options=None):
        # Mirrors the Kinetica explorer's ontology display-mode toggles (Full /
        # NKey / EKey) onto show_graph options. NKey/EKey ON is the default --
        # omitting the corresponding *_labelkeys option groups by schema type;
        # OFF sets it to 'false' to disable label-key grouping. Full is OFF by
        # default; ON adds schema_full_search='true'.
        opts = {"export_graph_schema": "true"}
        o = options or {}
        if not o.get("nkey"):
            opts["schema_node_labelkeys"] = "false"
        if not o.get("ekey"):
            opts["schema_edge_labelkeys"] = "false"
        if o.get("full"):
            opts["schema_full_search"] = "true"
        resp = self._db.show_graph(graph_name=graph, options=opts)
        dot = _dot_from_show_graph(resp) or "digraph {}"
        labels, rel_types = _labels_from_show_graph(resp)
        counts = _counts_from_show_graph(resp)
        return {"labels": labels, "rel_types": rel_types, "dot": dot, "counts": counts}

    def fetch_entities(self, graph, limit, offset=0):
        try:
            resp = self._db.show_graph(graph_name=graph)
            vtable, etable = _backing_tables(resp)
            if not vtable or not etable:
                return {"nodes": [], "edges": []}
            _validate_table_ident(vtable)
            _validate_table_ident(etable)
            nodes = [{"id": r["id"], "label": r["label"], "props": {}}
                     for r in self._src.rows(
                         f"SELECT id, label FROM {vtable} LIMIT {int(limit)} OFFSET {int(offset)}")]
            edges = [{"id": r["id"], "source": r["source_name"], "target": r["target_name"], "type": r["label"]}
                     for r in self._src.rows(
                         f"SELECT id, source_name, target_name, label FROM {etable} "
                         f"LIMIT {int(limit)} OFFSET {int(offset)}")]
            return {"nodes": nodes, "edges": edges}
        except Exception:
            # Load must succeed even if show_graph fails (network, auth), backing
            # tables aren't discoverable, or discovered tables are unreadable
            # (permissions, schema drift, etc.) -- ontology still renders from
            # get_schema(); browse is simply empty.
            return {"nodes": [], "edges": []}

    def get_record(self, graph, node_id):
        # The "post-join": picking a node pulls its full record from the
        # backing vertex table (explorer's `/get/records`). Never raises --
        # a bad id / unreachable Kinetica should not crash picking, it should
        # just show nothing.
        try:
            resp = self._db.show_graph(graph_name=graph)
            vtable, _etable = _backing_tables(resp)
            if not vtable:
                return {}
            _validate_table_ident(vtable)
            escaped_id = _escape_sql_literal(node_id)
            rows = list(self._src.rows(
                f"SELECT * FROM {vtable} WHERE id = '{escaped_id}' LIMIT 1"))
            if not rows:
                return {}
            return _row_to_record(rows[0], node_id)
        except Exception:
            return {}

    def load_graph(self, spec):
        ddl = spec.get("ddl")
        if not ddl:
            return {"status": "error", "message": "Kinetica Create requires a 'ddl' statement"}
        resp = self._db.execute_sql(ddl)
        if not resp.is_ok():
            info = resp.get("status_info", {}) or {}
            raise RuntimeError(info.get("message") or "execute_sql failed")
        return {"status": "ok", "graph": spec.get("graph")}

    def graph_sizes(self):
        # `show_graph(graph_name='')` returns parallel lists -- graph_names[i]
        # pairs with num_nodes[i]/num_edges[i] for every graph on the server
        # (probed live: 'expero.banking_graph' -> num_nodes=622032,
        # num_edges=845752). Best effort: zero-fill names if the size lists
        # are missing/misaligned, empty dict if show_graph itself fails.
        try:
            resp = self._db.show_graph(graph_name="")
        except Exception:
            return {}
        names = resp.get("graph_names") or []
        num_nodes = resp.get("num_nodes") or []
        num_edges = resp.get("num_edges") or []
        if len(num_nodes) == len(names) and len(num_edges) == len(names):
            return {name: {"nodes": int(n), "edges": int(e)}
                    for name, n, e in zip(names, num_nodes, num_edges)}
        return {name: {"nodes": 0, "edges": 0} for name in names}
