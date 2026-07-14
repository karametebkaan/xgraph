import json
import pytest

from xgraph_gateway import config
from xgraph_gateway.adapters.kinetica_adapter import (
    KineticaAdapter,
    _backing_tables,
    _dot_from_show_graph,
    _labels_from_show_graph,
)

# ---------------------------------------------------------------------------
# Canned show_graph responses, shaped after a live probe of
# `expero.banking_graph` (see docs/superpowers/specs for the probe notes).
# ---------------------------------------------------------------------------

_DOT_SAMPLE = (
    'digraph G { concentrate=true; \n'
    '"bank (0.342 %)" -> "wire_message (9.13 %)" [label=" performed (13.4%)"];\n'
    '}'
)

_LABELJSON_SAMPLE = json.dumps({
    "node_labels": [{"labels": ["bank"], "count": 2130}, {"labels": ["wire_message"], "count": 56800}],
    "edge_labels": [{"labels": ["performed"], "count": 3000}],
})

_SCHEMA_RESP = {
    "info": {"dot": _DOT_SAMPLE, "labeljson": _LABELJSON_SAMPLE, "UERR": ""},
}

_ORIGINAL_REQUEST_STATEMENT = (
    "create or replace directed graph expero.banking_graph (\n"
    "    nodes => INPUT_TABLES(\n"
    "        (SELECT\n"
    "            id as NODE,\n"
    "            label as LABEL\n"
    "        FROM expero.vertexes)\n"
    "    ),\n"
    "    edges => INPUT_TABLES((\n"
    "        SELECT\n"
    "            id as ID,\n"
    "            source_name as NODE1,\n"
    "            target_name as NODE2,\n"
    "            label as LABEL\n"
    "        FROM expero.edges\n"
    "    )),\n"
    "    OPTIONS => KV_PAIRS(is_partitioned = 'false')\n"
    ");"
)

_DDL_RESP = {
    "original_request": [json.dumps({"statement": _ORIGINAL_REQUEST_STATEMENT})],
}


# ---------------------------------------------------------------------------
# Unit tests -- pure helpers, no live DB.
# ---------------------------------------------------------------------------

def test_dot_from_show_graph_extracts_dot_string():
    assert _dot_from_show_graph(_SCHEMA_RESP) == _DOT_SAMPLE

def test_dot_from_show_graph_missing_info_returns_empty():
    assert _dot_from_show_graph({}) == ""
    assert _dot_from_show_graph({"info": {}}) == ""
    assert _dot_from_show_graph({"info": {"dot": ""}}) == ""

def test_labels_from_show_graph_extracts_node_and_edge_labels():
    labels, rel_types = _labels_from_show_graph(_SCHEMA_RESP)
    assert labels == ["bank", "wire_message"]
    assert rel_types == ["performed"]

def test_labels_from_show_graph_missing_labeljson_returns_empty_lists():
    assert _labels_from_show_graph({}) == ([], [])
    assert _labels_from_show_graph({"info": {}}) == ([], [])

def test_labels_from_show_graph_unparseable_labeljson_returns_empty_lists():
    assert _labels_from_show_graph({"info": {"labeljson": "not json"}}) == ([], [])

def test_backing_tables_extracts_vtable_and_etable():
    vtable, etable = _backing_tables(_DDL_RESP)
    assert vtable == "expero.vertexes"
    assert etable == "expero.edges"

def test_backing_tables_missing_original_request_returns_none_none():
    assert _backing_tables({}) == (None, None)
    assert _backing_tables({"original_request": []}) == (None, None)

def test_backing_tables_malformed_json_returns_none_none():
    assert _backing_tables({"original_request": ["not json"]}) == (None, None)


# ---------------------------------------------------------------------------
# fetch_entities shaping / never-raises behavior, via a fake KineticaAdapter
# built without touching gpudb's constructor network calls.
# ---------------------------------------------------------------------------

class _FakeSrc:
    def __init__(self, table_rows):
        self._table_rows = table_rows

    def rows(self, sql):
        for table, rows in self._table_rows.items():
            if table in sql:
                yield from rows
                return
        return iter(())

class _FakeDb:
    def __init__(self, show_graph_resp):
        self._resp = show_graph_resp

    def show_graph(self, graph_name="", options=None):
        return self._resp

def _bare_adapter(show_graph_resp, table_rows=None):
    adapter = KineticaAdapter.__new__(KineticaAdapter)
    adapter._db = _FakeDb(show_graph_resp)
    adapter._src = _FakeSrc(table_rows or {})
    return adapter

def test_fetch_entities_shapes_nodes_and_edges_from_backing_tables():
    adapter = _bare_adapter(_DDL_RESP, table_rows={
        "expero.vertexes": [{"id": "b1", "label": "bank"}],
        "expero.edges": [{"id": "e1", "source_name": "b1", "target_name": "w1", "label": "performed"}],
    })
    out = adapter.fetch_entities("expero.banking_graph", 10)
    assert out == {
        "nodes": [{"id": "b1", "label": "bank", "props": {}}],
        "edges": [{"id": "e1", "source": "b1", "target": "w1", "type": "performed"}],
    }

def test_fetch_entities_returns_empty_when_backing_tables_not_discoverable():
    adapter = _bare_adapter({"original_request": []})
    assert adapter.fetch_entities("expero.banking_graph", 10) == {"nodes": [], "edges": []}

def test_fetch_entities_never_raises_when_rows_call_blows_up():
    class _ExplodingSrc:
        def rows(self, sql):
            raise RuntimeError("boom")
    adapter = _bare_adapter(_DDL_RESP)
    adapter._src = _ExplodingSrc()
    assert adapter.fetch_entities("expero.banking_graph", 10) == {"nodes": [], "edges": []}

def test_fetch_entities_never_raises_when_show_graph_blows_up():
    class _ExplodingDb:
        def show_graph(self, graph_name="", options=None):
            raise ConnectionError("network error")
    adapter = _bare_adapter({})
    adapter._db = _ExplodingDb()
    assert adapter.fetch_entities("expero.banking_graph", 10) == {"nodes": [], "edges": []}


# ---------------------------------------------------------------------------
# Live integration tests -- skip if Kinetica is unreachable.
# ---------------------------------------------------------------------------

def _adapter_or_skip():
    s = config.load_settings()
    if not s.kinetica_url:
        pytest.skip("KINETICA_URL not set")
    try:
        a = KineticaAdapter(s)
        a.list_graphs()
        return a
    except Exception as e:
        pytest.skip(f"Kinetica unreachable: {e}")

def test_live_get_schema_returns_nontrivial_dot():
    a = _adapter_or_skip()
    schema = a.get_schema("expero.banking_graph")
    dot = schema["dot"]
    assert isinstance(dot, str) and len(dot) > 20
    low = dot.lower()
    assert "graph" in low
    assert isinstance(schema["labels"], list)
    assert isinstance(schema["rel_types"], list)

def test_live_fetch_entities_returns_bounded_shaped_rows():
    a = _adapter_or_skip()
    out = a.fetch_entities("expero.banking_graph", 25)
    assert isinstance(out, dict) and "nodes" in out and "edges" in out
    assert len(out["nodes"]) <= 25
    assert len(out["edges"]) <= 25
    for n in out["nodes"]:
        assert "id" in n and "label" in n
    for e in out["edges"]:
        assert "id" in e and "source" in e and "target" in e and "type" in e
