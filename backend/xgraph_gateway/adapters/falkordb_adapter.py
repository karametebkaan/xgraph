from __future__ import annotations
from falkordb import FalkorDB
from falkordb import Node as FalkorNode
from falkordb import Edge as FalkorEdge
from falkordb import Path as FalkorPath
from redis.exceptions import ConnectionError as RedisConnectionError, TimeoutError as RedisTimeoutError
from xgraph_gateway import config
from .base import GraphEngineAdapter
from graph_loader.cli import run_build
from graph_loader.config import EdgeSpec, Mapping, NodeSpec
from graph_loader.duckdb_source import DuckDBSource
from graph_loader.falkordb_sink import FalkorDBSink
from graph_loader.mapper import safe_ident

def _mapping_from_spec(spec: dict) -> Mapping:
    """Pure spec (dict, from the /create request body) -> graph_loader Mapping.

    No I/O -- unit-testable without a live DuckDB/FalkorDB connection. Field
    defaults mirror graph_loader.config.load_mapping's YAML defaults.
    """
    nodes = [
        NodeSpec(
            sql=n["sql"],
            id=n["id"],
            id_property=n.get("id_property", "NODE"),
            label_column=n["label_column"],
            label_property=n.get("label_property", "LABEL"),
            properties=list(n.get("properties", [])),
        )
        for n in spec.get("nodes", [])
    ]
    edges = [
        EdgeSpec(
            sql=e["sql"],
            id=e["id"],
            id_property=e.get("id_property", "ID"),
            type_column=e["type_column"],
            type_property=e.get("type_property", "LABEL"),
            source_key=e["source_key"],
            target_key=e["target_key"],
            properties=list(e.get("properties", [])),
        )
        for e in spec.get("edges", [])
    ]
    return Mapping(
        graph=spec["graph"],
        nodes=nodes,
        edges=edges,
        node_key_property=spec.get("node_key_property", "NODE"),
    )

def _column_names(header) -> list[str]:
    names = []
    for col in header:
        name = col[1] if isinstance(col, (list, tuple)) and len(col) > 1 else col
        names.append(name.decode() if isinstance(name, bytes) else name)
    return names

def _as_labels(v):
    """Normalize a raw `n.LABEL` value to a list of strings. FalkorDB nodes
    from `build_ingest_cypher` (Task 7+) store `LABEL` as an array (multi-
    label); pre-existing graphs (e.g. `banking_graph`, loaded via
    graph_loader) store it as a scalar string. Treat both uniformly as a
    list -- a scalar becomes a one-element list, `None`/`""` becomes `[]`."""
    return v if isinstance(v, list) else ([v] if v else [])

def _dot_esc(s) -> str:
    """Escape a value for a double-quoted Graphviz DOT string: backslash then
    double-quote (standard DOT escaping). Labels now come from arbitrary LLM
    output (e.g. `Government Agency`, or a name with an embedded quote), so the
    schema DOT must escape them or a stray `"` breaks the whole ontology render."""
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def _dot_from_triples(triples, pct=None, edge_pct=None) -> str:
    lines = ["digraph {"]
    if pct:
        # Emit a node declaration per label with its share of nodes in the
        # display label (e.g. `Location\n(31%)`) so the ontology graph carries
        # the same distribution as the label donut. The node id stays the bare
        # label so edges still match; only the rendered label carries the %.
        seen: list = []
        for src, _rel, dst in triples:
            for n in (src, dst):
                if n not in seen:
                    seen.append(n)
        for n in seen:
            p = pct.get(n)
            disp = _dot_esc(n) + (f"\\n({p:.1f}%)" if p is not None else "")
            lines.append(f'  "{_dot_esc(n)}" [label="{disp}"];')
    for src, rel, dst in triples:
        # Edge label carries its relationship-type's share of all edges.
        elabel = _dot_esc(rel) + (f"\\n({edge_pct[rel]:.1f}%)"
                                  if edge_pct and rel in edge_pct else "")
        lines.append(f'  "{_dot_esc(src)}" -> "{_dot_esc(dst)}" [label="{elabel}"];')
    lines.append("}")
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Graph extraction for the QueryPanel viz (path/graph rendering of raw Cypher
# results). A `falkordb` QueryResult.result_set cell can be a scalar, a Node,
# an Edge, a Path, or a list/map nesting any of those (e.g. `RETURN
# collect(n)`). Edge.src_node/dest_node come back from the wire as bare
# internal integer ids (see query_result.py's __parse_edge) -- NOT Node
# objects -- so resolving them to the graph's own `NODE` identity property
# requires a first pass over every Node seen anywhere in the result_set.
# ---------------------------------------------------------------------------

def _node_dict(node: FalkorNode) -> dict:
    # Every node carries a shared `Entity` label plus its specific label
    # (e.g. `bank`) and a `LABEL` property mirroring the specific label.
    # Prefer the specific label so query-viz nodes read `bank`/`wire_message`/
    # etc. instead of the generic `Entity`.
    label = (node.properties.get("LABEL")
             or next((l for l in (node.labels or []) if l != "Entity"), None)
             or (node.labels or [None])[0])
    return {"id": node.properties.get("NODE") or str(node.id),
            "label": label,
            "props": node.properties}

def _edge_dict(edge: FalkorEdge, id_map: dict) -> dict:
    return {"id": edge.properties.get("ID") or str(edge.id),
            "source": id_map.get(edge.src_node, str(edge.src_node)),
            "target": id_map.get(edge.dest_node, str(edge.dest_node)),
            "type": edge.relation}

def _walk_cells(result_set, visit) -> None:
    """Call `visit(cell)` for every scalar cell in `result_set`, recursing into
    lists/dicts (Cypher collections/maps can nest Node/Edge/Path values)."""
    def _walk(cell):
        if isinstance(cell, list):
            for c in cell:
                _walk(c)
        elif isinstance(cell, dict):
            for v in cell.values():
                _walk(v)
        else:
            visit(cell)
    for row in result_set:
        for cell in row:
            _walk(cell)

def _collect_id_map(result_set) -> dict:
    id_map: dict = {}
    def _visit(cell):
        if isinstance(cell, FalkorNode):
            id_map[cell.id] = cell.properties.get("NODE") or str(cell.id)
        elif isinstance(cell, FalkorPath):
            for n in cell.nodes():
                id_map[n.id] = n.properties.get("NODE") or str(n.id)
    _walk_cells(result_set, _visit)
    return id_map

def extract_graph(result_set) -> dict:
    """Best-effort: walk every Node/Edge/Path cell in a FalkorDB result_set and
    return de-duped {"nodes": [...], "edges": [...]}. Never raises -- any
    parse error yields an empty graph so `rows`/`columns` are unaffected."""
    try:
        id_map = _collect_id_map(result_set)
        nodes: dict = {}
        edges: dict = {}
        def _visit(cell):
            if isinstance(cell, FalkorNode):
                nd = _node_dict(cell)
                nodes[nd["id"]] = nd
            elif isinstance(cell, FalkorEdge):
                ed = _edge_dict(cell, id_map)
                edges[ed["id"]] = ed
            elif isinstance(cell, FalkorPath):
                for n in cell.nodes():
                    nd = _node_dict(n)
                    nodes[nd["id"]] = nd
                for e in cell.edges():
                    ed = _edge_dict(e, id_map)
                    edges[ed["id"]] = ed
        _walk_cells(result_set, _visit)
        return {"nodes": list(nodes.values()), "edges": list(edges.values())}
    except Exception:
        return {"nodes": [], "edges": []}

def _serialize_cell(cell, id_map: dict):
    """Make a result_set cell JSON-safe: Node/Edge/Path objects become compact
    dicts (reusing the same shapes as `extract_graph`); everything else
    (scalars, lists, maps) passes through, recursing where needed."""
    if isinstance(cell, FalkorNode):
        return _node_dict(cell)
    if isinstance(cell, FalkorEdge):
        return _edge_dict(cell, id_map)
    if isinstance(cell, FalkorPath):
        return {"nodes": [_node_dict(n) for n in cell.nodes()],
                "edges": [_edge_dict(e, id_map) for e in cell.edges()]}
    if isinstance(cell, list):
        return [_serialize_cell(c, id_map) for c in cell]
    if isinstance(cell, dict):
        return {k: _serialize_cell(v, id_map) for k, v in cell.items()}
    return cell

def _graph_typed_columns(result_set, num_columns: int) -> set:
    """Return the set of column indices whose cell value is a Node/Edge/Path
    (checked via each column's first non-null row -- ragged/short rows are
    guarded, never raise). These columns are excluded from the tabular
    `columns`/`rows` output (they're rendered via `graph` instead) while a
    pure-scalar RETURN leaves this set empty."""
    graph_cols: set = set()
    try:
        for col_idx in range(num_columns):
            for row in result_set:
                if col_idx >= len(row):
                    continue
                cell = row[col_idx]
                if cell is None:
                    continue
                if isinstance(cell, (FalkorNode, FalkorEdge, FalkorPath)):
                    graph_cols.add(col_idx)
                break  # only the first non-null cell per column matters
    except Exception:
        return set()
    return graph_cols

# ---------------------------------------------------------------------------
# ingest_elements -- MERGE Extract-discovered entities/relations into a named
# graph. build_ingest_cypher is PURE (rows in, Cypher+params out, no I/O) so
# it's unit-testable without a live FalkorDB connection, mirroring
# graph_loader.mapper's node_batches/edge_batches shape: same `:Entity(NODE)`
# + `LABEL` conventions, labels/types validated via safe_ident before they're
# interpolated (Cypher can't parameterize a label/type), all data (ids,
# names, attrs) passed as query params.
# ---------------------------------------------------------------------------

def _valid_nodes(nodes: list[dict]) -> list[dict]:
    # A null/missing id can't be MERGEd on, so drop it up front (mirrors
    # graph_loader.mapper.node_batches).
    return [n for n in nodes if n.get("id") is not None]

def _valid_edges(edges: list[dict]) -> list[dict]:
    # A null id/src/dst edge could never resolve its endpoints -- discard
    # rather than emit a no-op MERGE (mirrors graph_loader.mapper.edge_batches).
    return [e for e in edges
            if e.get("id") is not None and e.get("src") is not None and e.get("dst") is not None]

def _cypher_ident(name) -> str:
    """Backtick-quote an arbitrary label / relationship type for use as a Cypher
    identifier. Labels can't be parameterized, so they're interpolated into the
    query string; backtick-quoting (embedded backticks doubled) lets multi-word
    or punctuated LLM labels like `Government Agency` be used safely without the
    identifier-only restriction of `safe_ident`, while neutralizing injection —
    a `` a`)-[:X]-() `` payload stays inside the quoted identifier as
    `` `a``)-[:X]-()` ``."""
    return "`" + str(name).replace("`", "``") + "`"


def build_ingest_cypher(nodes: list[dict], edges: list[dict]) -> list[tuple[str, dict]]:
    """Group `nodes` by their full label vector and `edges` by label into one
    UNWIND/MERGE Cypher statement each (labels/type backtick-quoted); returns
    [(cypher, params), ...]. All entity data (ids, names, attrs) travels in
    `params["rows"]`, never interpolated into the Cypher string.

    Nodes carry a canonical `labels: list[str]` + `label_raw: list[str]`
    (Task 6 provenance); older callers that only send a single `label` still
    work -- `labels`/`label_raw` fall back to `[label]`. Every node/edge also
    gets `last_seen_ts` set on every MERGE and `first_seen_ts` set only
    `ON CREATE` (first ingest)."""
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()

    statements: list[tuple[str, dict]] = []

    node_groups: dict[tuple, list[dict]] = {}
    for n in _valid_nodes(nodes):
        labels = n.get("labels") or [n.get("label")]
        labels = tuple(l for l in labels if l)  # original strings; quoted below
        node_groups.setdefault(labels, []).append(n)
    for labels, rows in node_groups.items():
        label_clause = "".join(f":{_cypher_ident(l)}" for l in labels)
        # A node with no usable label yields an empty clause -- keep the SET
        # valid by dropping the bare `n` token in that case.
        label_set = f"n{label_clause}, " if label_clause else ""
        # FalkorDB requires ON CREATE SET to appear directly after the MERGE
        # it qualifies, before any other SET clause on the same variable --
        # so first_seen_ts is set there, and the label/vector/name/attrs/
        # last_seen_ts SET is a second, unconditional clause right after.
        query = (
            "UNWIND $rows AS r "
            f"MERGE (n:Entity {{NODE: r.id}}) "
            "ON CREATE SET n.first_seen_ts = $now "
            f"SET {label_set}n.LABEL = r.labels, n.label_raw = r.label_raw, "
            "n += r.attrs, n.last_seen_ts = $now"
        )
        payload = [{"id": r["id"], "name": r.get("name"),
                    "labels": list(labels),
                    "label_raw": r.get("label_raw") or list(labels),
                    "attrs": r.get("attrs") or {}} for r in rows]
        statements.append((query, {"rows": payload, "now": now_iso}))

    edge_groups: dict[str, list[dict]] = {}
    for e in _valid_edges(edges):
        label = e.get("label")  # original string; quoted below
        edge_groups.setdefault(label, []).append(e)
    for label, rows in edge_groups.items():
        query = (
            "UNWIND $rows AS e "
            "MATCH (a:Entity {NODE: e.src}), (b:Entity {NODE: e.dst}) "
            f"MERGE (a)-[x:{_cypher_ident(label)} {{ID: e.id}}]->(b) "
            "ON CREATE SET x.first_seen_ts = $now "
            f"SET x.LABEL = $label, x += e.attrs, x.last_seen_ts = $now"
        )
        payload = [{"id": r["id"], "src": r["src"], "dst": r["dst"], "attrs": r.get("attrs") or {}}
                   for r in rows]
        statements.append((query, {"rows": payload, "label": label, "now": now_iso}))

    return statements

class FalkorDBAdapter(GraphEngineAdapter):
    def __init__(self, settings=None, conn=None):
        if conn is not None:
            host = conn["host"]
            port = conn["port"]
            password = conn.get("password")
        else:
            host = settings.falkordb_host
            port = settings.falkordb_port
            password = settings.falkordb_password
        self._host = host
        self._port = port
        self._password = password
        self._db = FalkorDB(host=host, port=port, password=password)

    def _graph(self, graph):
        return self._db.select_graph(graph)

    def _counts(self, g):
        nodes = g.query("MATCH (n) RETURN count(n)", timeout=60000).result_set[0][0]
        edges = g.query("MATCH ()-[r]->() RETURN count(r)", timeout=60000).result_set[0][0]
        return {"nodes": nodes, "edges": edges}

    def list_graphs(self):
        return list(self._db.list_graphs())

    def run_query(self, graph, cypher, timeout=60000):
        qr = self._graph(graph).query(cypher, timeout=timeout)
        graph_data = extract_graph(qr.result_set)
        id_map = _collect_id_map(qr.result_set)
        all_columns = _column_names(qr.header)
        graph_col_idx = _graph_typed_columns(qr.result_set, len(all_columns))
        columns = [c for i, c in enumerate(all_columns) if i not in graph_col_idx]
        rows = [
            [_serialize_cell(cell, id_map) for i, cell in enumerate(row) if i not in graph_col_idx]
            for row in qr.result_set
        ]
        return {"columns": columns, "rows": rows, "graph": graph_data}

    def _label_properties(self, g, labels: list[str]) -> dict:
        """Best-effort per-label property keys: sample ONE node per label via
        `keys(n) LIMIT 1` (small graphs, so per-label LIMIT 1 is cheap). Feeds
        the NL->Cypher prompt so the LLM learns e.g. `name` exists and doesn't
        default to filtering on the opaque `NODE` id. A label that isn't a
        safe Cypher identifier, or whose sample query errors (e.g. no nodes
        left with that label), is simply skipped -- never raises."""
        properties: dict = {}
        for label in labels:
            try:
                ident = safe_ident(label)
            except Exception:
                continue
            try:
                rs = g.query(f"MATCH (n:{ident}) RETURN keys(n) LIMIT 1", timeout=60000).result_set
            except Exception:
                continue
            if rs and rs[0]:
                properties[label] = sorted(rs[0][0])
        return properties

    def get_schema(self, graph, options=None):
        # `options` (Full/NKey/EKey display modes) is Kinetica-only -- FalkorDB
        # always derives the DOT from actual triples, so it's accepted and ignored.
        #
        # `n.LABEL` is ALWAYS treated as a list of strings (Task 9): a scalar
        # (pre-existing graphs like `banking_graph`) is normalized to a
        # one-element list via `_as_labels`, same as an array `LABEL`
        # (extracted graphs, Task 7+) -- one code path, no scalar/array branch.
        g = self._graph(graph)
        label_rows = [r[0] for r in
                      g.query("MATCH (n) RETURN DISTINCT n.LABEL", timeout=60000).result_set]
        labels = sorted({label for row in label_rows for label in _as_labels(row)})
        # Per-label node counts via server-side aggregation -- one small row per
        # distinct LABEL value, so it stays cheap even on large graphs (unlike a
        # full node scan). Feeds the ontology DOT's percentages; Kinetica's
        # show/graph DOT computes the same distribution.
        from collections import Counter
        struct_counts: Counter = Counter()
        _total = 0
        for lv, c in g.query("MATCH (n) RETURN n.LABEL AS l, count(*) AS c",
                             timeout=60000).result_set:
            lbls = _as_labels(lv)
            _total += c
            if lbls:
                struct_counts[lbls[0]] += c  # structural (first) label, matches the DOT
        label_pct = {l: 100.0 * c / _total for l, c in struct_counts.items()} if _total else {}
        rel_counts: Counter = Counter()
        _etotal = 0
        for t, c in g.query("MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS c",
                            timeout=60000).result_set:
            if t:
                rel_counts[t] += c
                _etotal += c
        rels = sorted(rel_counts)
        edge_pct = {t: 100.0 * c / _etotal for t, c in rel_counts.items()} if _etotal else {}
        raw_triples = [(r[0], r[1], r[2]) for r in g.query(
            "MATCH (a)-[r]->(b) RETURN DISTINCT a.LABEL, type(r), b.LABEL", timeout=60000).result_set]
        triples = []
        seen_triples: set = set()
        for a_label, rel, b_label in raw_triples:
            a_labels, b_labels = _as_labels(a_label), _as_labels(b_label)
            if a_labels and b_labels:
                # DOT is a structural label -> label graph; the first element
                # of each side's label vector is its structural label. Two
                # distinct multi-label arrays can collapse to the same
                # structural triple -- dedup (stable order) before DOT.
                triple = (a_labels[0], rel, b_labels[0])
                if triple not in seen_triples:
                    seen_triples.add(triple)
                    triples.append(triple)
        return {"labels": labels, "rel_types": rels,
                "dot": _dot_from_triples(triples, label_pct, edge_pct),
                "label_counts": dict(struct_counts),
                "rel_counts": dict(rel_counts),
                "properties": self._label_properties(g, labels),
                "counts": self._counts(g),
                # No ontology-store handle here, so every label defaults to
                # the single EntityType axis; a later task can enrich this
                # from the metadata store.
                "axes": {"EntityType": labels}}

    def fetch_entities(self, graph, limit, offset=0):
        g = self._graph(graph)
        nodes = [{"id": r[0], "label": r[1], "props": r[2]} for r in g.query(
            "MATCH (n) RETURN n.NODE, n.LABEL, properties(n) SKIP $off LIMIT $l",
            {"l": limit, "off": offset}, timeout=60000).result_set]
        edges = [{"id": r[0], "source": r[1], "target": r[2], "type": r[3]} for r in g.query(
            "MATCH (a)-[r]->(b) RETURN r.ID, a.NODE, b.NODE, type(r) SKIP $off LIMIT $l",
            {"l": limit, "off": offset}, timeout=60000).result_set]
        return {"nodes": nodes, "edges": edges}

    def get_record(self, graph, node_id):
        rs = self._graph(graph).query(
            "MATCH (n {NODE:$id}) RETURN n.NODE, n.LABEL, properties(n)",
            {"id": node_id}, timeout=60000).result_set
        if not rs:
            return {}
        return {"id": rs[0][0], "label": rs[0][1], "props": rs[0][2]}

    def load_graph(self, spec):
        mapping = _mapping_from_spec(spec)
        # Resolve bare/relative table paths against the repo data dir so the
        # Create panel can send `vertexes.parquet` without an absolute host path.
        tables = {name: config.resolve_data_path(path)
                  for name, path in spec["tables"].items()}
        source = DuckDBSource.connect(tables)
        sink = FalkorDBSink.connect(
            spec["graph"], host=self._host, port=self._port, password=self._password)
        return run_build(mapping, source, sink)

    def graph_sizes(self):
        return {name: self._counts(self._graph(name)) for name in self.list_graphs()}

    def ingest_elements(self, graph, nodes, edges):
        g = self._graph(graph)
        created_nodes = 0
        created_edges = 0
        for query, params in build_ingest_cypher(nodes, edges):
            qr = g.query(query, params, timeout=60000)
            created_nodes += qr.nodes_created
            created_edges += qr.relationships_created
        valid_nodes = _valid_nodes(nodes)
        valid_edges = _valid_edges(edges)
        node_labels = sorted({n["label"] for n in valid_nodes})
        edge_labels = sorted({e["label"] for e in valid_edges})
        # "nodes"/"edges" = total ensured present (inputs are already unique by
        # id); "nodes_created"/"edges_created" = newly created this call (MERGE
        # deltas) -- a repeat/overlapping Extract still reports the elements as
        # present even though nothing new was created.
        return {"nodes": len(valid_nodes), "edges": len(valid_edges),
                "nodes_created": created_nodes, "edges_created": created_edges,
                "labels": {"node_labels": node_labels, "edge_labels": edge_labels}}

    def delete_graph(self, graph):
        # Best-effort on a missing graph (delete is idempotent from the
        # caller's point of view) -- but a real connection/timeout error
        # against FalkorDB itself still propagates so it surfaces as a 502/504,
        # not a false "deleted" success.
        try:
            self._graph(graph).delete()
        except (RedisConnectionError, RedisTimeoutError):
            raise
        except Exception:
            pass
        return {"deleted": graph}
