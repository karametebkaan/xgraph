from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List

from .config import EdgeSpec, NodeSpec

_IDENT_RE = re.compile(r"^[A-Za-z0-9_]+$")


class MappingError(Exception):
    """Raised when a label/type/identifier value is not a safe Cypher token."""


def safe_ident(value) -> str:
    if not isinstance(value, str) or not _IDENT_RE.fullmatch(value):
        raise MappingError(f"unsafe identifier for Cypher: {value!r}")
    return value


@dataclass
class CypherBatch:
    query: str
    params: dict


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _props(row: dict, names: List[str]) -> Dict:
    return {name: row[name] for name in names if row.get(name) is not None}


def _group_by(rows: List[dict], column: str) -> Dict[str, List[dict]]:
    grouped: Dict[str, List[dict]] = {}
    for row in rows:
        key = safe_ident(row[column])
        grouped.setdefault(key, []).append(row)
    return grouped


def node_batches(spec: NodeSpec, rows: List[dict], batch_size: int = 5000) -> List[CypherBatch]:
    idp = safe_ident(spec.id_property)
    lp = safe_ident(spec.label_property)
    # Drop rows with no identity: a null/missing id would create a `NODE: null`
    # node that nothing can match later. Discard up front rather than be
    # surprised by orphan/null-keyed nodes after the load.
    rows = [r for r in rows if r.get(spec.id) is not None]
    batches: List[CypherBatch] = []
    for label, lrows in _group_by(rows, spec.label_column).items():
        query = (
            "UNWIND $rows AS row "
            f"MERGE (n:Entity {{{idp}: row.id}}) "
            f"SET n:{label}, n.{lp} = $label, n += row.props"
        )
        for chunk in _chunks(lrows, batch_size):
            payload = [{"id": r[spec.id], "props": _props(r, spec.properties)} for r in chunk]
            batches.append(CypherBatch(query=query, params={"rows": payload, "label": label}))
    return batches


def entity_index_statement(node_key_property: str) -> str:
    keyprop = safe_ident(node_key_property)
    return f"CREATE INDEX FOR (n:Entity) ON (n.{keyprop})"


def label_index_statements(node_key_property: str, labels: List[str]) -> List[str]:
    keyprop = safe_ident(node_key_property)
    return [f"CREATE INDEX FOR (n:{safe_ident(l)}) ON (n.{keyprop})" for l in labels]


def edge_batches(spec: EdgeSpec, rows: List[dict], node_key_property: str,
                 batch_size: int = 5000) -> List[CypherBatch]:
    keyprop = safe_ident(node_key_property)
    idp = safe_ident(spec.id_property)
    tp = safe_ident(spec.type_property)
    # Drop edges with no identity or a null/missing endpoint id: the endpoint
    # MATCH could never resolve, so they create nothing anyway -- discard them
    # explicitly instead of silently emitting no-op MERGEs.
    rows = [r for r in rows
            if r.get(spec.id) is not None
            and r.get(spec.source_key) is not None
            and r.get(spec.target_key) is not None]
    batches: List[CypherBatch] = []
    for etype, erows in _group_by(rows, spec.type_column).items():
        query = (
            "UNWIND $rows AS row "
            f"MATCH (a:Entity {{{keyprop}: row.n1}}), (b:Entity {{{keyprop}: row.n2}}) "
            f"MERGE (a)-[r:{etype} {{{idp}: row.id}}]->(b) "
            f"SET r.{tp} = $type, r += row.props"
        )
        for chunk in _chunks(erows, batch_size):
            payload = [{"id": r[spec.id], "n1": r[spec.source_key],
                        "n2": r[spec.target_key], "props": _props(r, spec.properties)}
                       for r in chunk]
            batches.append(CypherBatch(query=query, params={"rows": payload, "type": etype}))
    return batches
