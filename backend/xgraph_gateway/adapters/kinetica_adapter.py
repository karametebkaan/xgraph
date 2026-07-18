from __future__ import annotations
import json
import re
from datetime import datetime, timezone
from gpudb import GPUdb
from xgraph_gateway import config
from .base import GraphEngineAdapter
from graph_loader.kinetica_source import KineticaSource
from graph_loader.mapper import safe_ident, MappingError

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

def _creation_statement_text(resp) -> str | None:
    """Pull the authoritative `create ... graph ...` DDL text out of
    `resp['original_request']` -- a JSON-encoded copy of the original
    statement (`resp['original_request'][0]` is a JSON string with a
    `"statement"` field). Returns `None` if absent/unparseable -- callers
    must treat that as "statement unknown", never raise.
    """
    try:
        raw_list = resp.get("original_request") or []
        if not raw_list:
            return None
        return json.loads(raw_list[0])["statement"]
    except (TypeError, ValueError, KeyError, IndexError):
        return None

def _backing_tables(resp) -> tuple[str | None, str | None]:
    """Discover the vertex/edge backing table names for a graph from the
    `create ... graph ...` DDL text (`_creation_statement_text`). The
    statement shape is:

        create or replace directed graph <name> (
            nodes => INPUT_TABLES((SELECT ... FROM <vtable>)),
            edges => INPUT_TABLES((SELECT ... FROM <etable>)),
            ...
        );

    Returns `(vtable, etable)`, either/both `None` if not found -- callers
    must treat that as "backing tables unknown" and return empty results,
    never raise.
    """
    statement = _creation_statement_text(resp)
    if not statement:
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

# ---------------------------------------------------------------------------
# ingest_elements -- upsert Extract-discovered entities/relations into a pair
# of backing tables (<graph>_nodes/<graph>_edges) and (re)build a Kinetica
# property graph over them. All builders below are PURE (identifiers/rows in,
# SQL/dict out, no I/O) so they're unit-testable without a live Kinetica
# connection. Mirrors kgr's table+graph shape, simplified to a single LABEL
# string column (no ontology/axis tables). Identifiers are validated via
# safe_ident before they're interpolated into any SQL string; all entity data
# (ids, names, attrs) travels only through the insert_records_json JSON
# payload, never string-interpolated.
# ---------------------------------------------------------------------------

def _qualified_table_name(graph: str, suffix: str) -> str:
    # Schema-qualified graph names (e.g. "myschema.mygraph") get their backing
    # table suffixed on the last (table) part only, e.g. "myschema.mygraph_nodes".
    # Each dotted part is validated individually -- safe_ident rejects dots.
    parts = [safe_ident(p) for p in str(graph).split(".")]
    parts[-1] = parts[-1] + suffix
    return ".".join(parts)

def node_table_name(graph: str) -> str:
    return _qualified_table_name(graph, "_nodes")

def edge_table_name(graph: str) -> str:
    return _qualified_table_name(graph, "_edges")

def label_keys_table_name(graph: str) -> str:
    return _qualified_table_name(graph, "_label_keys")

def create_schema_sql(graph: str) -> str | None:
    """`CREATE SCHEMA IF NOT EXISTS <schema>` for a dotted graph name, or None
    if `graph` is unqualified (nothing to create -- the default schema is
    used). Validates the schema part via safe_ident before interpolating it."""
    parts = str(graph).split(".")
    if len(parts) < 2:
        return None
    schema = safe_ident(parts[0])
    return f"CREATE SCHEMA IF NOT EXISTS {schema}"

def create_table_sql(table: str, kind: str) -> str:
    """DDL for the skinny node/edge backing tables Kinetica's graph engine
    reads. `table` must already be a validated identifier (e.g. from
    node_table_name/edge_table_name) -- this builder does not re-validate it,
    since it never accepts raw user input directly. `kind` is 'node' or 'edge'."""
    if kind == "node":
        return (
            f"CREATE TABLE IF NOT EXISTS {table} (\n"
            "    NODE VARCHAR(256, PRIMARY_KEY, SHARD_KEY) NOT NULL,\n"
            "    LABEL VARCHAR[],\n"
            "    label_raw VARCHAR[],\n"
            "    name VARCHAR(1024),\n"
            "    first_seen_ts TIMESTAMP,\n"
            "    last_seen_ts TIMESTAMP\n"
            ")"
        )
    if kind == "edge":
        return (
            f"CREATE TABLE IF NOT EXISTS {table} (\n"
            "    edge_key VARCHAR(64, PRIMARY_KEY) NOT NULL,\n"
            "    NODE1 VARCHAR(256),\n"
            "    NODE2 VARCHAR(256),\n"
            "    LABEL VARCHAR(256)\n"
            ")"
        )
    raise ValueError(f"unknown create_table_sql kind: {kind!r}")

# ---------------------------------------------------------------------------
# label_keys -- kgr-style LABEL_KEY (axis) grouping table: one row per axis,
# holding the array of node labels that belong to it (e.g. "EntityType" ->
# ["Company", "Person"]). Materialized per-graph, rebuilt before every
# CREATE GRAPH (see KineticaAdapter._materialize_label_keys) and fed in as a
# second NODES input (see create_graph_sql's label_keys_table param) so
# Kinetica's graph schema can group the multi-label vector by axis. The
# adapter has no ontology-store handle here, so every label defaults to the
# single "EntityType" axis (kgr's own default) -- a later task refines this
# once the metadata store is reachable from ingest_elements.
# ---------------------------------------------------------------------------

_DEFAULT_LABEL_AXIS = "EntityType"

def create_label_keys_table_sql(table: str) -> str:
    """DDL for the label_keys grouping table: `label_key` (axis name, PK) ->
    `label` (VARCHAR[] of the node labels on that axis). `table` is expected
    pre-validated (from label_keys_table_name)."""
    return (
        f"CREATE TABLE IF NOT EXISTS {table} (\n"
        "    label_key VARCHAR(64, PRIMARY_KEY, SHARD_KEY) NOT NULL,\n"
        "    label VARCHAR[]\n"
        ")"
    )

def label_keys_rows(nodes: list[dict], default_axis: str = _DEFAULT_LABEL_AXIS) -> list[dict]:
    """Group the distinct labels across `nodes` under `default_axis` -- one
    row `{label_key, label}` (or `[]` if there are no labels at all). Every
    node's full label vector (`_node_label_vector`: `labels` or `[label]`)
    contributes; axis metadata isn't reachable from the adapter here, so
    there is exactly one axis for now (refined once the ontology store is
    consulted, see the module docstring above). `nodes` need not be a real
    ingest payload -- `_materialize_label_keys` calls this with a single
    synthetic `{"labels": [...]}` entry built from the node table's actual
    accumulated label set, so the row reflects every label ever ingested
    into the graph, not just this call's."""
    labels: set[str] = set()
    for n in nodes:
        for lbl in _node_label_vector(n):
            if lbl:
                labels.add(lbl)
    if not labels:
        return []
    return [{"label_key": default_axis, "label": sorted(labels)}]

# Kinetica's CREATE GRAPH DDL grammar special-cases certain result-column
# names inside the NODES INPUT_TABLES select as identity aliases: NODE (also
# ID/WKTPOINT) is the node id, LABEL is the node's type label for `:Label`
# MATCH -- and NAME is an alias for NODE_NAME, a SECOND node-identity column.
# Selecting the backing table's `name` column verbatim (i.e. under that exact
# output name) makes Kinetica silently register every node twice (NUM_NODES
# doubles) and breaks `:Label` matching entirely (confirmed live: with `name`
# selected as-is, `show_graph`'s labeljson reported total_unlabeled_nodes ==
# total_labeled_nodes, and a plain `(p:Person)-[:WORKS_AT]->(o:Organization)`
# MATCH returned zero rows even though the untyped `(p)-[:WORKS_AT]->(o)`
# traversal found all 3 edges). Aliasing the output column to
# `_NAME_PROPERTY` avoids the collision -- get_schema mirrors this alias so
# the properties it reports match what's actually queryable in GQL.
_NAME_PROPERTY = "entity_name"

def create_graph_sql(graph: str, node_table: str, edge_table: str,
                      node_attr_cols: list[str] | None = None,
                      edge_attr_cols: list[str] | None = None,
                      label_keys_table: str | None = None) -> str:
    """`CREATE OR REPLACE DIRECTED GRAPH` DDL over the node/edge backing
    tables. `graph` is validated (dot-part-wise) via safe_ident before
    interpolation; `node_table`/`edge_table` are expected pre-validated
    (from node_table_name/edge_table_name). `node_attr_cols`/`edge_attr_cols`
    are the extra evolved attribute columns (see `discover_attr_columns`) to
    add to the respective NODES/EDGES select -- each is re-validated via
    safe_ident here too (defense in depth: this builder never trusts a
    caller-supplied column list blindly), and only appended if present, so
    the base 3-/4-column shape (and the `name AS entity_name` alias -- see
    `_NAME_PROPERTY`) is unchanged when there are no attrs yet.

    `label_keys_table`, if given, is a second NODES input -- the kgr-style
    LABEL_KEY (axis) grouping table (see `_materialize_label_keys`) -- added
    as a SIBLING select inside the same `NODES => INPUT_TABLES(...)` list,
    matching kgr's `graph.sql` verbatim:
        NODES => INPUT_TABLES(
            (SELECT label_key AS LABEL_KEY, label AS LABEL FROM <label_keys_table>),
            (SELECT ... FROM <node_table>)
        )
    (NOT a trailing clause appended after the node select -- Kinetica's CREATE
    GRAPH grammar takes the LABEL_KEY grouping as one more member of the same
    INPUT_TABLES(...) tuple list.) `label_keys_table` is re-validated
    dot-part-wise via safe_ident, same as `graph`.
    """
    graph_ident = ".".join(safe_ident(p) for p in str(graph).split("."))
    node_cols = [safe_ident(c) for c in (node_attr_cols or [])]
    edge_cols = [safe_ident(c) for c in (edge_attr_cols or [])]
    node_select = ", ".join(["NODE", "LABEL", f"name AS {_NAME_PROPERTY}"] + node_cols)
    edge_select = ", ".join(["NODE1", "NODE2", "LABEL"] + edge_cols)
    if label_keys_table:
        lk_ident = ".".join(safe_ident(p) for p in str(label_keys_table).split("."))
        nodes_clause = (
            "INPUT_TABLES(\n"
            f"        (SELECT label_key AS LABEL_KEY, label AS LABEL FROM {lk_ident}),\n"
            f"        (SELECT {node_select} FROM {node_table})\n"
            "    )"
        )
    else:
        nodes_clause = f"INPUT_TABLES((SELECT {node_select} FROM {node_table}))"
    return (
        f"CREATE OR REPLACE DIRECTED GRAPH {graph_ident} (\n"
        f"    NODES => {nodes_clause},\n"
        f"    EDGES => INPUT_TABLES((SELECT {edge_select} FROM {edge_table})),\n"
        "    OPTIONS => KV_PAIRS(save_persist = 'true')\n"
        ")"
    )

def _now_ts_str() -> str:
    """Current time as a naive-UTC 'YYYY-MM-DD HH:MM:SS.mmm' string -- the
    same naive-UTC convention as compute/duckdb_engine.py's record_document
    (`datetime.now(timezone.utc).replace(tzinfo=None)`), formatted to
    millisecond precision as a string because insert_records_json's payload
    is JSON (a raw `datetime` object isn't JSON-serializable) and Kinetica's
    TIMESTAMP JSON-insert format is millisecond-precision, not microsecond
    (confirmed live: a 3-digit-ms string round-trips through insert_records_json
    -> show_table as the expected epoch-ms TIMESTAMP)."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

def _node_label_vector(n: dict) -> list[str]:
    """A node's multi-label vector: `labels` if present (Task 6/7's
    facet-carrying entities), else a one-element fallback `[label]` (older
    single-label callers), else `[]` if neither is present."""
    return n.get("labels") or ([n["label"]] if n.get("label") else [])

def node_rows(nodes: list[dict]) -> list[dict]:
    """[{id,label,labels?,label_raw?,name,attrs}] -> insert_records_json
    payload dicts ({NODE,LABEL,label_raw,name,first_seen_ts,last_seen_ts}),
    dropping rows with no identity (mirrors graph_loader.mapper's null-id row
    discard). `LABEL` is the multi-label vector (`_node_label_vector`);
    `label_raw` falls back to that same vector when the caller didn't supply
    pre-fold labels. Provenance timestamps are naive UTC, one shared value for
    the whole batch (see `_now_ts_str`)."""
    now = _now_ts_str()
    out = []
    for n in nodes:
        if n.get("id") is None:
            continue
        labels = _node_label_vector(n)
        out.append({"NODE": n["id"], "LABEL": labels,
                     "label_raw": n.get("label_raw") or labels,
                     "name": n.get("name"),
                     "first_seen_ts": now, "last_seen_ts": now})
    return out

def edge_rows(edges: list[dict]) -> list[dict]:
    """[{id,src,dst,label,attrs}] -> insert_records_json payload dicts
    ({edge_key,NODE1,NODE2,LABEL}), dropping rows with a null id/src/dst
    (an edge with a missing endpoint can never resolve)."""
    return [{"edge_key": e["id"], "NODE1": e["src"], "NODE2": e["dst"], "LABEL": e.get("label")}
            for e in edges
            if e.get("id") is not None and e.get("src") is not None and e.get("dst") is not None]

# ---------------------------------------------------------------------------
# Attribute-column evolution -- extracted `attrs` become real, typed columns
# on the node/edge backing tables (kgr-style ALTER TABLE ADD COLUMN), so
# they're queryable in GQL rather than only visible via hydration. All
# builders here are PURE; the live ALTER/read-columns work happens in
# KineticaAdapter (below), which calls these.
# ---------------------------------------------------------------------------

_NODE_BASE_COLS = {"NODE", "LABEL", "label_raw", "name", _NAME_PROPERTY,
                   "first_seen_ts", "last_seen_ts"}
_EDGE_BASE_COLS = {"edge_key", "NODE1", "NODE2", "LABEL"}

def _infer_col_type(value) -> str:
    """First-non-null-value type inference for an evolved attr column.
    `bool` is checked before `int` -- `bool` is an `int` subclass in Python,
    so `isinstance(True, int)` is also True."""
    if isinstance(value, bool):
        return "BOOLEAN"
    if isinstance(value, int):
        return "BIGINT"
    if isinstance(value, float):
        return "DOUBLE"
    return "VARCHAR(1024)"

def discover_attr_columns(elements: list[dict], base_cols: set[str]) -> dict[str, str]:
    """Union the `attrs` keys across `elements` ([{...,"attrs":{...}}, ...] --
    nodes and edges are discovered separately, in separate calls) into
    {col_name: sql_type}, insertion-ordered by first appearance.

    - A key colliding with `base_cols` (already a real column: NODE/LABEL/
      name/entity_name for nodes, edge_key/NODE1/NODE2/LABEL for edges) is
      skipped -- it's not a new attribute.
    - A key that isn't a safe SQL identifier (`safe_ident`) is skipped too --
      `attrs` are untrusted extraction data, so a stray key name (spaces,
      punctuation) must never crash ingest; it's silently dropped rather than
      raised, mirroring how `mapper`/`node_rows`/`edge_rows` drop bad rows
      instead of raising.
    - The type is inferred (`_infer_col_type`) from the first NON-NULL value
      seen for that key across every element; a key seen only with None
      values so far defaults to VARCHAR(1024) until/unless a later ingest
      call sees a real value (kgr rule: a column's type, once declared by
      ALTER TABLE, never changes -- so once the live column exists this
      discovery step doesn't matter for it, see `KineticaAdapter._evolve_columns`).
    """
    cols: dict[str, str | None] = {}
    for el in elements:
        attrs = el.get("attrs") or {}
        for key, value in attrs.items():
            if key in base_cols:
                continue
            try:
                safe_ident(key)
            except MappingError:
                continue
            if key not in cols:
                cols[key] = _infer_col_type(value) if value is not None else None
            elif cols[key] is None and value is not None:
                cols[key] = _infer_col_type(value)
    return {k: (v if v is not None else "VARCHAR(1024)") for k, v in cols.items()}

def add_column_sql(table: str, column: str, col_type: str) -> str:
    """`ALTER TABLE <table> ADD COLUMN <column> <col_type>` for evolving a new
    attr column onto a backing table. `column` is validated via safe_ident
    before interpolation (defense in depth -- `discover_attr_columns` already
    filters bad keys, but this builder doesn't trust that blindly either).
    `table` is expected pre-validated (from node_table_name/edge_table_name);
    `col_type` is one of `_infer_col_type`'s fixed set, never user data."""
    safe_ident(column)
    return f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"

def _coerce_attr_value(value, col_type: str):
    """Best-effort coercion of an extracted attr value to its evolved
    column's declared SQL type (int()/float()/bool()/str()) -- an
    unconvertible value becomes None (null) rather than raising, since a
    column's type never changes once declared (kgr rule) and a single bad
    value must not fail the whole upsert."""
    if value is None:
        return None
    try:
        if col_type == "BOOLEAN":
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in ("true", "t", "1", "yes")
            return bool(value)
        if col_type == "BIGINT":
            return int(value)
        if col_type == "DOUBLE":
            return float(value)
        return str(value)
    except (TypeError, ValueError):
        return None

def _node_rows_with_attrs(nodes: list[dict], attr_cols: dict[str, str]) -> list[dict]:
    """Like `node_rows`, but each row also carries one field per evolved attr
    column (`attr_cols`: {col_name: sql_type}), coerced via
    `_coerce_attr_value`. This is the payload `KineticaAdapter.ingest_elements`
    actually upserts, so it shares `node_rows`' multi-label vector + label_raw
    + provenance-timestamp base shape (one shared batch timestamp)."""
    now = _now_ts_str()
    rows = []
    for n in nodes:
        if n.get("id") is None:
            continue
        labels = _node_label_vector(n)
        row = {"NODE": n["id"], "LABEL": labels,
               "label_raw": n.get("label_raw") or labels,
               "name": n.get("name"),
               "first_seen_ts": now, "last_seen_ts": now}
        attrs = n.get("attrs") or {}
        for col, col_type in attr_cols.items():
            row[col] = _coerce_attr_value(attrs.get(col), col_type)
        rows.append(row)
    return rows

def _edge_rows_with_attrs(edges: list[dict], attr_cols: dict[str, str]) -> list[dict]:
    """Edge counterpart of `_node_rows_with_attrs` -- see its docstring."""
    rows = []
    for e in edges:
        if e.get("id") is None or e.get("src") is None or e.get("dst") is None:
            continue
        row = {"edge_key": e["id"], "NODE1": e["src"], "NODE2": e["dst"], "LABEL": e.get("label")}
        attrs = e.get("attrs") or {}
        for col, col_type in attr_cols.items():
            row[col] = _coerce_attr_value(attrs.get(col), col_type)
        rows.append(row)
    return rows

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
        properties = self._extract_node_properties(graph, labels)
        return {"labels": labels, "rel_types": rel_types, "dot": dot,
                "properties": properties, "counts": counts}

    def _extract_node_properties(self, graph, labels: list[str]) -> dict:
        """Per-label property keys (for NL->Cypher grounding, see
        falkordb_adapter's equivalent), for EXTRACT graphs only.

        show_graph's labeljson carries only label names + counts, not column
        names, so there is no cheap per-label property list in general.
        However, an EXTRACT graph's backing node table (this adapter's own
        `<graph>_nodes`, created by `ingest_elements`/`create_table_sql`) has a
        known, uniform column set (`NODE`, `LABEL`, `name`) shared by every
        label -- so if that table exists, every label gets the same property
        list. Graphs built some other way (e.g. the banking graph, whose
        backing vertex table is unrelated to `node_table_name`) simply won't
        have this table, so `show_table` reports it missing and this returns
        `{}` unchanged -- never an expensive per-graph query, never raises.

        The table's own `name` column is reported as `_NAME_PROPERTY`
        (`entity_name`), not literally `name` -- that's the alias
        `create_graph_sql` gives it in the graph itself (see `_NAME_PROPERTY`'s
        docstring), so it's the property actually filterable via GQL.
        """
        try:
            table = node_table_name(graph)
            cols = self._current_columns(table)
            if not cols:
                return {}
            cols = [_NAME_PROPERTY if c == "name" else c for c in cols]
            return {label: cols for label in labels}
        except Exception:
            return {}

    def fetch_entities(self, graph, limit, offset=0):
        # Primary: Kinetica's native /get/graph/entities (what the Kinetica Graph
        # Explorer uses) — the engine's own view of the graph, works for ANY graph
        # (banking, extract, computed) regardless of backing-table shape. Fall back
        # to reading the backing tables only if the graph API is unavailable.
        try:
            return self._entities_via_graph_api(graph, limit, offset)
        except Exception:
            pass
        try:
            return self._entities_via_tables(graph, limit, offset)
        except Exception:
            # Load must still succeed — ontology renders from get_schema(); browse
            # is simply empty if neither path works.
            return {"nodes": [], "edges": []}

    def _entities_via_graph_api(self, graph, limit, offset):
        """Fetch nodes+edges from `/get/graph/entities` (GPUdb.get_graph_entities).

        Response packs entities into a flat `entities_string`/`entities_int` list:
        nodes as [id, labelIdx, ...] (stride 2), edges as [edgeId, src, dst,
        labelIdx, ...] (stride 4). `labelIdx` is 1-based into `labels`, each a JSON
        array string like '["Organization"]'. (payload_type 'double' = WKT/geo — not
        decoded here; that's the geo/DeckGL path.)
        """
        nresp = self._db.get_graph_entities(
            graph_name=graph, offset=int(offset), limit=int(limit),
            options={"entity_type": "node"})
        eresp = self._db.get_graph_entities(
            graph_name=graph, offset=int(offset), limit=int(limit),
            options={"entity_type": "edge"})

        def _arr(resp):
            return resp.get("entities_string") or resp.get("entities_int") or []

        def _label(labels, idx1):
            try:
                raw = labels[int(idx1) - 1]
            except (IndexError, ValueError, TypeError):
                return None
            try:
                v = json.loads(raw)
                if isinstance(v, list):
                    return "|".join(str(x) for x in v) if v else None
                return str(v)
            except (ValueError, TypeError):
                return str(raw).strip('[]"')

        nlabels = nresp.get("labels") or []
        narr = _arr(nresp)
        nodes = [{"id": str(narr[i]), "label": _label(nlabels, narr[i + 1]), "props": {}}
                 for i in range(0, len(narr) - 1, 2)]

        elabels = eresp.get("labels") or []
        earr = _arr(eresp)
        edges = [{"id": str(earr[i]), "source": str(earr[i + 1]), "target": str(earr[i + 2]),
                  "type": _label(elabels, earr[i + 3])}
                 for i in range(0, len(earr) - 3, 4)]
        return {"nodes": nodes, "edges": edges}

    def _entities_via_tables(self, graph, limit, offset):
        """Fallback: read the graph's backing node/edge tables directly, mapping
        columns flexibly (extract: NODE/LABEL/name, NODE1/NODE2; banking:
        id/label, source_name/target_name)."""
        resp = self._db.show_graph(graph_name=graph)
        vtable, etable = _backing_tables(resp)
        if not vtable or not etable:
            return {"nodes": [], "edges": []}
        _validate_table_ident(vtable)
        _validate_table_ident(etable)
        ncols = {c.lower(): c for c in self._current_columns(vtable)}
        ecols = {c.lower(): c for c in self._current_columns(etable)}

        def pick(cols, *cands):
            for c in cands:
                if c.lower() in cols:
                    return cols[c.lower()]
            return cands[-1] if not cols else None

        # Read by the resolved column names directly (no SQL alias) so the row
        # dicts are keyed by the real column, regardless of engine aliasing.
        n_id, n_lbl = pick(ncols, "NODE", "id"), pick(ncols, "LABEL", "label")
        e_id = pick(ecols, "edge_key", "id")
        e_src, e_tgt = pick(ecols, "NODE1", "source_name"), pick(ecols, "NODE2", "target_name")
        e_lbl = pick(ecols, "LABEL", "label")
        nodes = []
        if n_id:
            cols = [n_id] + ([n_lbl] if n_lbl else [])
            for r in self._src.rows(f"SELECT {', '.join(cols)} FROM {vtable} "
                                    f"LIMIT {int(limit)} OFFSET {int(offset)}"):
                nodes.append({"id": r.get(n_id), "label": r.get(n_lbl) if n_lbl else None, "props": {}})
        edges = []
        if e_src and e_tgt:
            cols = [e_src, e_tgt] + ([e_id] if e_id else []) + ([e_lbl] if e_lbl else [])
            for r in self._src.rows(f"SELECT {', '.join(cols)} FROM {etable} "
                                    f"LIMIT {int(limit)} OFFSET {int(offset)}"):
                edges.append({"id": r.get(e_id) if e_id else None, "source": r.get(e_src),
                              "target": r.get(e_tgt), "type": r.get(e_lbl) if e_lbl else None})
        return {"nodes": nodes, "edges": edges}

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

    def _execute_ddl(self, statement: str) -> None:
        # Mirrors load_graph's execute_sql/is_ok() error-surfacing pattern.
        resp = self._db.execute_sql(statement)
        if not resp.is_ok():
            info = resp.get("status_info", {}) or {}
            raise RuntimeError(info.get("message") or "execute_sql failed")

    def _insert_and_count(self, table: str, rows: list[dict]) -> int:
        """Insert `rows` into `table` via insert_records_json (upserting on the
        existing primary key), returning how many were newly *created* (as
        opposed to matched-and-updated). insert_records_json returns a JSON
        response string shaped like `{"data": {"count_inserted": ...,
        "count_updated": ...}, ...}` (see the GPUdb.insert_records_json
        docstring's own example: `response_object['data']['count_inserted']`).
        Falls back to `len(rows)` if that shape isn't present for any reason
        (never raises just to compute this count -- the ingest itself already
        succeeded by the time this runs)."""
        resp = self._db.insert_records_json(
            json.dumps(rows), table, options={"update_on_existing_pk": "true"})
        try:
            return int(json.loads(resp)["data"]["count_inserted"])
        except (TypeError, ValueError, KeyError):
            return len(rows)

    def _current_columns(self, table: str) -> list[str]:
        """Current column names of `table` via `show_table`'s column-info
        response (same shape `_extract_node_properties` reads). Returns []
        if the table doesn't exist yet or the response is unreadable for any
        reason -- never raises (callers treat that as "no columns yet")."""
        try:
            resp = self._db.show_table(
                table_name=table,
                options={"get_column_info": "true", "no_error_if_not_exists": "true"})
            if not resp.get("table_names"):
                return []
            schemas = resp.get("type_schemas") or []
            if not schemas:
                return []
            return [f["name"] for f in json.loads(schemas[0]).get("fields", [])]
        except Exception:
            return []

    def _evolve_columns(self, table: str, attr_cols: dict[str, str]) -> None:
        """ALTER TABLE ADD COLUMN for each key in `attr_cols` not already
        present on `table`. Column types never change once declared (kgr
        rule) -- a key already present is left untouched even if this call's
        inferred type would differ."""
        if not attr_cols:
            return
        existing = set(self._current_columns(table))
        for col, col_type in attr_cols.items():
            if col not in existing:
                self._execute_ddl(add_column_sql(table, col, col_type))

    def _all_attr_columns(self, table: str, base_cols: set[str]) -> list[str]:
        """Every non-base column currently on `table` -- used to rebuild
        CREATE GRAPH's select list from the table's actual, accumulated
        state (not just this call's discovered attrs), so a prior ingest's
        attr columns (e.g. `population` added last run) stay queryable even
        on a run that only adds a different new attr (e.g. `country`)."""
        return [c for c in self._current_columns(table) if c not in base_cols]

    def _distinct_node_labels(self, node_table: str) -> set[str]:
        """Every distinct node label currently on `node_table`, read back
        from the table itself -- mirrors `_all_attr_columns`'s "read the
        table's actual accumulated state" approach, so a label from an
        earlier ingest call (e.g. a prior `/extract` into the same graph)
        stays in the axis grouping even on a later call whose payload
        doesn't re-mention it. `LABEL` is declared `VARCHAR[]`; over the
        plain-SQL read path each row's value round-trips as a JSON-array
        string (confirmed live, see the task-8 live test), so this flattens
        every row's array in Python rather than depending on Kinetica
        array-unnest SQL. Returns `set()` (never raises) if the table
        doesn't exist yet or the read fails for any reason.
        """
        labels: set[str] = set()
        try:
            for r in self._src.rows(f"SELECT DISTINCT LABEL FROM {node_table}"):
                val = r.get("LABEL")
                if isinstance(val, str):
                    try:
                        val = json.loads(val)
                    except (TypeError, ValueError):
                        val = None
                if isinstance(val, list):
                    labels.update(lbl for lbl in val if lbl)
        except Exception:
            return set()
        return labels

    def _materialize_label_keys(self, graph, node_table) -> str | None:
        """Best-effort, idempotent per-graph label_keys table (kgr's
        LABEL_KEY grouping shape, see `label_keys_rows`) built from
        `node_table`'s FULL ACCUMULATED label set, not just this call's
        passed-in nodes. `ingest_elements` is invoked once per extracted
        document with only that document's entities, so grouping from just
        this call's nodes would silently drop an earlier document's label
        from the axis the moment a later `/extract` into the same graph
        doesn't re-mention it -- breaking incremental "append to an existing
        graph" behavior. Mirrors `_all_attr_columns`: re-read the table's
        actual state via `_distinct_node_labels` (by the time this runs,
        `ingest_elements` has already upserted this call's rows into
        `node_table`, so its labels are included too). Idempotent via
        drop-and-recreate (not `CREATE TABLE IF NOT EXISTS` + upsert) so a
        label no longer present anywhere in the node table doesn't linger as
        a stale row -- this table holds only a rebuildable *derived*
        grouping, never the entity data itself, so replacing it outright
        each call is safe. Returns the table name to feed into
        `create_graph_sql`'s `label_keys_table=`, or `None` if there's
        nothing to materialize (no labels at all) or the write failed for
        any reason -- never blocks the node/edge ingest that matters more.
        """
        try:
            labels = self._distinct_node_labels(node_table)
            if not labels:
                return None
            rows = label_keys_rows([{"labels": sorted(labels)}])
            table = label_keys_table_name(graph)
            self._execute_ddl(f"DROP TABLE IF EXISTS {table}")
            self._execute_ddl(create_label_keys_table_sql(table))
            self._insert_and_count(table, rows)
            return table
        except Exception:
            return None

    def ingest_elements(self, graph, nodes, edges):
        n_rows = node_rows(nodes)
        e_rows = edge_rows(edges)
        if not n_rows and not e_rows:
            # Never touch Kinetica for an empty ingest -- nothing to create.
            return {"nodes": 0, "edges": 0, "nodes_created": 0, "edges_created": 0,
                    "labels": {"node_labels": [], "edge_labels": []}}

        node_table = node_table_name(graph)
        edge_table = edge_table_name(graph)

        schema_ddl = create_schema_sql(graph)
        if schema_ddl:
            self._execute_ddl(schema_ddl)
        # `CREATE TABLE IF NOT EXISTS` is NOT a no-op once a prior ingest has
        # evolved extra columns onto the table: Kinetica errors ("already
        # exists with type id X not type id Y") if the existing table's type
        # doesn't match the statement's declared columns, even under
        # IF NOT EXISTS. So only run it when the table doesn't exist yet --
        # `_current_columns` returns [] for both "table missing" and "read
        # failed", either of which means "safe to (re-)issue CREATE TABLE".
        if not self._current_columns(node_table):
            self._execute_ddl(create_table_sql(node_table, "node"))
        if not self._current_columns(edge_table):
            self._execute_ddl(create_table_sql(edge_table, "edge"))

        # Evolve: discover this call's new attr columns and ALTER them in
        # before upserting so the payload's extra fields have somewhere to
        # land.
        node_attr_cols = discover_attr_columns(nodes, _NODE_BASE_COLS)
        edge_attr_cols = discover_attr_columns(edges, _EDGE_BASE_COLS)
        self._evolve_columns(node_table, node_attr_cols)
        self._evolve_columns(edge_table, edge_attr_cols)

        n_payload = _node_rows_with_attrs(nodes, node_attr_cols)
        e_payload = _edge_rows_with_attrs(edges, edge_attr_cols)

        nodes_created = self._insert_and_count(node_table, n_payload) if n_payload else 0
        edges_created = self._insert_and_count(edge_table, e_payload) if e_payload else 0

        # Rebuild CREATE GRAPH from the table's actual current columns (not
        # just this call's discovered attrs) so previously-evolved columns
        # from an earlier ingest stay in the graph even if this call's rows
        # don't mention them.
        all_node_attr_cols = self._all_attr_columns(node_table, _NODE_BASE_COLS)
        all_edge_attr_cols = self._all_attr_columns(edge_table, _EDGE_BASE_COLS)
        label_keys_table = self._materialize_label_keys(graph, node_table)
        self._execute_ddl(create_graph_sql(
            graph, node_table, edge_table, all_node_attr_cols, all_edge_attr_cols,
            label_keys_table=label_keys_table))

        # LABEL is now a multi-label vector (list), not a scalar -- flatten
        # before deduping (a set of lists would be unhashable).
        node_labels = sorted({lbl for r in n_rows for lbl in (r.get("LABEL") or [])})
        edge_labels = sorted({r["LABEL"] for r in e_rows if r.get("LABEL")})
        # "nodes"/"edges" = total ensured present this call; "nodes_created"/
        # "edges_created" = newly created (vs. matched-and-updated) -- lets a
        # repeat/overlapping Extract report elements as present even when
        # nothing new was created.
        return {"nodes": len(n_rows), "edges": len(e_rows),
                "nodes_created": nodes_created, "edges_created": edges_created,
                "labels": {"node_labels": node_labels, "edge_labels": edge_labels}}

    def storage(self, graph):
        """Storage viewer for an Extract graph: columns + up to 25 sample rows
        from each backing table (`node_table_name`/`edge_table_name`) that
        actually exists. A graph built some other way (e.g. the banking
        graph, whose backing vertex table is unrelated to `node_table_name`)
        has neither table, so this returns an empty `tables` list with a
        note -- never raises, per table or overall (a bad `graph` name that
        `safe_ident` would reject, an unreachable table, a read error -- all
        degrade to "skip this table", mirroring `_extract_node_properties`'s
        best-effort shape).
        """
        try:
            candidate_tables = [node_table_name(graph), edge_table_name(graph)]
        except Exception:
            candidate_tables = []
        tables = []
        for table in candidate_tables:
            try:
                cols = self._current_columns(table)
                if not cols:
                    continue
                rows = [[r.get(c) for c in cols]
                        for r in self._src.rows(f"SELECT * FROM {table} LIMIT 25")]
                tables.append({"name": table, "columns": cols, "rows": rows})
            except Exception:
                continue
        if not tables:
            return {"kind": "kinetica", "tables": [],
                    "note": "No extract backing tables for this graph."}
        return {"kind": "kinetica", "tables": tables}

    def creation_statement(self, graph):
        """The authoritative CREATE GRAPH DDL for `graph`, straight from
        `show_graph` (see `_creation_statement_text`) -- Kinetica keeps the
        original creation statement server-side, unlike FalkorDB (built
        incrementally, no stored recipe). Never raises -- any failure
        (unreachable Kinetica, missing/malformed original_request) yields
        `{"statement": None, ...}`.
        """
        try:
            resp = self._db.show_graph(graph_name=graph, options={})
            return {"statement": _creation_statement_text(resp),
                    "source": "kinetica:show_graph"}
        except Exception:
            return {"statement": None, "source": "kinetica:show_graph"}

    def delete_graph(self, graph):
        # Best-effort, never raises: dropping a graph that doesn't exist (or a
        # backing table an EXTRACT never created) is still a successful delete
        # from the caller's point of view.
        try:
            self._db.delete_graph(graph_name=graph)
        except Exception:
            try:
                graph_ident = ".".join(safe_ident(p) for p in str(graph).split("."))
                self._src.rows(f'DROP GRAPH "{graph_ident}"')
            except Exception:
                pass
        for table in (node_table_name(graph), edge_table_name(graph)):
            try:
                self._db.clear_table(table_name=table, options={})
            except Exception:
                pass
        return {"deleted": graph}
