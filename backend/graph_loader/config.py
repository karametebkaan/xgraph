from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

import yaml


class ConfigError(Exception):
    """Raised when the mapping YAML is missing keys or malformed."""


@dataclass
class NodeSpec:
    sql: str
    id: str
    id_property: str
    label_column: str
    label_property: str
    properties: List[str]


@dataclass
class EdgeSpec:
    sql: str
    id: str
    id_property: str
    type_column: str
    type_property: str
    source_key: str
    target_key: str
    properties: List[str]


@dataclass
class DuckDBSpec:
    # Maps a table name used in the mapping SQL (e.g. "expero.vertexes") to a
    # Parquet/CSV path, glob, or object-store URL. Enables the Kinetica-free
    # build route: `build-graph.py --source duckdb`.
    tables: Dict[str, str]


@dataclass
class HydrateSpec:
    # Wide-attribute source for post-traversal hydration (see graph_loader.hydrate).
    source: str
    key: str


@dataclass
class Mapping:
    graph: str
    nodes: List[NodeSpec]
    edges: List[EdgeSpec]
    node_key_property: str
    duckdb: Optional[DuckDBSpec] = None
    hydrate: Optional[HydrateSpec] = None


def _require(d: dict, key: str, ctx: str):
    if not isinstance(d, dict) or key not in d:
        raise ConfigError(f"{ctx}: missing required key '{key}'")
    return d[key]


def load_mapping(path: str) -> Mapping:
    with open(path) as f:
        try:
            raw = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("mapping file must be a YAML mapping/object")

    graph = _require(raw, "graph", "top level")
    nodes_raw = _require(raw, "nodes", "top level")
    if not nodes_raw:
        raise ConfigError("at least one node spec is required")

    nodes = []
    for i, n in enumerate(nodes_raw):
        ctx = f"nodes[{i}]"
        nodes.append(NodeSpec(
            sql=_require(n, "sql", ctx),
            id=_require(n, "id", ctx),
            id_property=n.get("id_property", "NODE"),
            label_column=_require(n, "label_column", ctx),
            label_property=n.get("label_property", "LABEL"),
            properties=list(n.get("properties", [])),
        ))

    key_props = {n.id_property for n in nodes}
    if len(key_props) != 1:
        raise ConfigError(
            f"all node specs must share one id_property; found {sorted(key_props)}"
        )
    node_key_property = next(iter(key_props))

    edges = []
    for i, e in enumerate(raw.get("edges", []) or []):
        ctx = f"edges[{i}]"
        edges.append(EdgeSpec(
            sql=_require(e, "sql", ctx),
            id=_require(e, "id", ctx),
            id_property=e.get("id_property", "ID"),
            type_column=_require(e, "type_column", ctx),
            type_property=e.get("type_property", "LABEL"),
            source_key=_require(e, "source_key", ctx),
            target_key=_require(e, "target_key", ctx),
            properties=list(e.get("properties", [])),
        ))

    dd = raw.get("duckdb")
    duckdb_spec = None
    if dd is not None:
        tables = _require(dd, "tables", "duckdb")
        if not isinstance(tables, dict) or not tables:
            raise ConfigError(
                "duckdb.tables must be a non-empty mapping of table name -> file path")
        duckdb_spec = DuckDBSpec(tables=dict(tables))

    h = raw.get("hydrate")
    hydrate_spec = None
    if h is not None:
        hydrate_spec = HydrateSpec(
            source=_require(h, "source", "hydrate"),
            key=h.get("key", node_key_property),
        )

    return Mapping(graph=graph, nodes=nodes, edges=edges,
                   node_key_property=node_key_property,
                   duckdb=duckdb_spec, hydrate=hydrate_spec)
