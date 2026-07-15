"""Cross-engine LIVE integration test: Extract -> Ask, on both FalkorDB and
Kinetica, over a FIXED (deterministic, no-LLM) graph built from three
canonical "who works where" facts.

Real Kinetica/FalkorDB (via config.load_settings()) AND a real LLM (the
`claude` CLI) are required -- this is not a fake-adapter unit test. Each
engine param SKIPs independently if its adapter can't construct/connect, or
if `claude` isn't on PATH; it never fails just because a dependency is
missing.

Background (see xgraph_gateway/adapters/kinetica_adapter.py): Extract builds
a graph where a node's `NODE` is an opaque canonical id and `name` is the
human-readable value. FalkorDB's Ask path already grounds the NL->Cypher
prompt with `name` (get_schema's per-label `properties`). This test is the
cross-engine proof that Kinetica's Ask path does the same, now that
`create_graph_sql` exposes `name` as a queryable node property and
`get_schema` grounds the LLM with it for EXTRACT graphs.
"""
from __future__ import annotations

import shutil

import pytest

from xgraph_gateway import config, nlcypher
from xgraph_gateway.adapters.falkordb_adapter import FalkorDBAdapter
from xgraph_gateway.adapters.kinetica_adapter import (
    KineticaAdapter,
    edge_table_name,
    node_table_name,
)

NODES = [
    {"id": "kaan", "label": "Person", "name": "Kaan", "attrs": {}},
    {"id": "tan", "label": "Person", "name": "Tan", "attrs": {}},
    {"id": "shouvik", "label": "Person", "name": "Shouvik", "attrs": {}},
    {"id": "kinetica", "label": "Organization", "name": "Kinetica", "attrs": {}},
    {"id": "bloomberg", "label": "Organization", "name": "Bloomberg", "attrs": {}},
]

EDGES = [
    {"id": "e1", "src": "kaan", "dst": "kinetica", "label": "WORKS_AT", "attrs": {}},
    {"id": "e2", "src": "tan", "dst": "bloomberg", "label": "WORKS_AT", "attrs": {}},
    {"id": "e3", "src": "shouvik", "dst": "kinetica", "label": "WORKS_AT", "attrs": {}},
]

_GRAPH_NAMES = {"falkordb": "xgraph_ask_it_fk", "kinetica": "xgraph_ask_it_kin"}


def _require_claude():
    if shutil.which("claude") is None:
        pytest.skip("claude CLI not on PATH -- Ask needs the real LLM")


def _falkor_adapter_or_skip():
    s = config.load_settings()
    try:
        a = FalkorDBAdapter(s)
        a.list_graphs()
        return a
    except Exception as e:
        pytest.skip(f"FalkorDB unreachable: {e}")


def _kinetica_adapter_or_skip():
    s = config.load_settings()
    if not s.kinetica_url:
        pytest.skip("KINETICA_URL not set")
    try:
        a = KineticaAdapter(s)
        a.list_graphs()
        return a
    except Exception as e:
        pytest.skip(f"Kinetica unreachable: {e}")


def _cleanup_falkordb(adapter, graph):
    try:
        adapter._graph(graph).delete()
    except Exception:
        pass


def _cleanup_kinetica(adapter, graph):
    try:
        adapter._db.delete_graph(graph_name=graph)
    except Exception:
        pass
    for table in (node_table_name(graph), edge_table_name(graph)):
        try:
            adapter._db.execute_sql(f"DROP TABLE IF EXISTS {table}")
        except Exception:
            pass


def _flatten_lower(*sources) -> list[str]:
    """Lowercase every scalar value found anywhere in `sources` (rows lists,
    the `graph` {"nodes": [...], "edges": [...]} dict, or any nesting of
    list/dict). Both engines' `run_query` can put the actual answer in either
    place: FalkorDB's dialect prompt encourages a bound path variable
    (`MATCH p=(...) RETURN a, p`), and a RETURN of bare Node/Path cells is
    stripped out of the tabular `columns`/`rows` by the adapter (they only
    surface under `graph`) -- so a robust assertion has to look at both,
    regardless of which shape the (non-deterministic) LLM's RETURN clause
    happens to pick."""
    cells: list[str] = []

    def _visit(v):
        if isinstance(v, (list, tuple)):
            for x in v:
                _visit(x)
        elif isinstance(v, dict):
            for x in v.values():
                _visit(x)
        elif v is not None:
            cells.append(str(v).lower())

    for source in sources:
        _visit(source)
    return cells


@pytest.mark.parametrize("engine", ["falkordb", "kinetica"])
def test_extract_ask_who_works_at_kinetica(engine):
    _require_claude()
    graph = _GRAPH_NAMES[engine]

    if engine == "falkordb":
        adapter = _falkor_adapter_or_skip()
        cleanup = lambda: _cleanup_falkordb(adapter, graph)
    else:
        adapter = _kinetica_adapter_or_skip()
        cleanup = lambda: _cleanup_kinetica(adapter, graph)

    cleanup()
    try:
        adapter.ingest_elements(graph, NODES, EDGES)

        schema = adapter.get_schema(graph)
        cypher = nlcypher.generate_cypher(schema, engine, "Who works at Kinetica?", graph=graph)
        print(f"\n[{engine}] generated query:\n{cypher}\n")
        ok, reason = nlcypher.validate_cypher(cypher, schema)
        assert ok, f"[{engine}] generated query failed read-only validation: {reason}\n{cypher}"

        res = adapter.run_query(graph, cypher)
        cells = _flatten_lower(res.get("rows"), res.get("graph"))
        print(f"[{engine}] rows: {res['rows']}")
        print(f"[{engine}] graph: {res.get('graph')}")

        assert any("kaan" in c for c in cells), (
            f"[{engine}] expected 'kaan' among result cells; got {cells}\ncypher:\n{cypher}")
        assert any("shouvik" in c for c in cells), (
            f"[{engine}] expected 'shouvik' among result cells; got {cells}\ncypher:\n{cypher}")
        assert not any("tan" in c for c in cells), (
            f"[{engine}] 'tan' (works at Bloomberg, not Kinetica) leaked into results; "
            f"got {cells}\ncypher:\n{cypher}")
    finally:
        cleanup()
