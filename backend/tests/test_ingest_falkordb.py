import pytest

from xgraph_gateway.adapters.falkordb_adapter import build_ingest_cypher


# ---------------------------------------------------------------------------
# build_ingest_cypher -- pure, no DB. Groups nodes/edges by label, one UNWIND
# MERGE statement per label; all data goes in params, never the Cypher string.
# ---------------------------------------------------------------------------

def _nodes():
    return [
        {"id": "n1", "label": "Person", "name": "Jerome Powell", "attrs": {"role": "chair"}},
        {"id": "n2", "label": "Person", "name": "Janet Yellen", "attrs": {}},
        {"id": "n3", "label": "Organization", "name": "Federal Reserve", "attrs": {}},
    ]

def _edges():
    return [
        {"id": "e1", "src": "n1", "dst": "n3", "label": "WORKS_AT", "attrs": {"since": 2018}},
    ]

def test_build_ingest_cypher_groups_by_label():
    statements = build_ingest_cypher(_nodes(), _edges())
    # 2 node labels (Person, Organization) + 1 edge label (WORKS_AT) = 3 statements.
    assert len(statements) == 3

def test_build_ingest_cypher_node_statement_shape():
    # Node statements are now keyed by their full label vector (Task 7) --
    # single-label nodes fall back to a one-element vector, so there's no
    # scalar "label" param anymore; identify the group via the row payload.
    statements = build_ingest_cypher(_nodes(), [])
    by_labels = {tuple(params["rows"][0]["labels"]): (cypher, params)
                 for cypher, params in statements}
    assert set(by_labels) == {("Person",), ("Organization",)}
    cypher, params = by_labels[("Person",)]
    assert "UNWIND $rows AS r" in cypher
    assert "MERGE (n:Entity {NODE: r.id})" in cypher
    assert "SET n:`Person`" in cypher
    assert "n.LABEL = r.labels" in cypher
    assert "n += r.attrs" in cypher
    ids = {row["id"] for row in params["rows"]}
    assert ids == {"n1", "n2"}

def test_build_ingest_cypher_edge_statement_shape():
    statements = build_ingest_cypher([], _edges())
    assert len(statements) == 1
    cypher, params = statements[0]
    assert params["label"] == "WORKS_AT"
    assert "UNWIND $rows AS e" in cypher
    assert "MATCH (a:Entity {NODE: e.src}), (b:Entity {NODE: e.dst})" in cypher
    assert "MERGE (a)-[x:`WORKS_AT` {ID: e.id}]->(b)" in cypher
    assert "x.LABEL = $label" in cypher
    assert "x += e.attrs" in cypher
    assert params["rows"] == [{"id": "e1", "src": "n1", "dst": "n3", "attrs": {"since": 2018}}]

def test_build_ingest_cypher_labels_are_backtick_quoted():
    statements = build_ingest_cypher(_nodes(), _edges())
    for cypher, params in statements:
        # Labels/types are interpolated as backtick-quoted Cypher identifiers.
        rows = params.get("rows") or [{}]
        if "src" in rows[0]:
            assert "[x:`" in cypher  # edge relationship type is quoted
        else:
            for lbl in params["rows"][0]["labels"]:
                assert "`" + lbl + "`" in cypher  # each node label applied, quoted

def test_build_ingest_cypher_no_entity_data_in_cypher_string():
    statements = build_ingest_cypher(_nodes(), _edges())
    for cypher, params in statements:
        # Data (ids, names, attrs) must be parameters, never string-interpolated.
        assert "n1" not in cypher
        assert "n3" not in cypher
        assert "Jerome Powell" not in cypher
        assert "Federal Reserve" not in cypher
        assert "2018" not in cypher

def test_build_ingest_cypher_skips_rows_with_null_identity():
    nodes = [
        {"id": None, "label": "Person", "name": "Ghost", "attrs": {}},
        {"id": "n1", "label": "Person", "name": "Real", "attrs": {}},
    ]
    edges = [
        {"id": "e1", "src": None, "dst": "n1", "label": "REL", "attrs": {}},
        {"id": None, "src": "n1", "dst": "n1", "label": "REL", "attrs": {}},
        {"id": "e2", "src": "n1", "dst": "n1", "label": "REL", "attrs": {}},
    ]
    statements = build_ingest_cypher(nodes, edges)
    # Node statements no longer carry a scalar "label" param (Task 7 keys
    # them by label vector) -- identify by the absence of edge-only "src".
    node_stmt = next(p for c, p in statements if "src" not in p["rows"][0])
    assert node_stmt["rows"] == [
        {"id": "n1", "name": "Real", "labels": ["Person"], "label_raw": ["Person"], "attrs": {}}
    ]
    edge_stmt = next(p for c, p in statements if p.get("label") == "REL")
    assert edge_stmt["rows"] == [{"id": "e2", "src": "n1", "dst": "n1", "attrs": {}}]

def test_build_ingest_cypher_empty_inputs_yield_no_statements():
    assert build_ingest_cypher([], []) == []

def test_build_ingest_cypher_multiword_label_is_escaped_not_rejected():
    # Multi-word LLM labels (e.g. "Government Agency") are common and must NOT
    # fail extraction -- they're backtick-quoted, and the original string is
    # kept in the LABEL property vector.
    nodes = [{"id": "n1", "label": "Government Agency",
              "labels": ["Government Agency"], "label_raw": ["Government Agency"],
              "name": "DHS", "attrs": {}}]
    cypher, params = build_ingest_cypher(nodes, [])[0]
    assert ":`Government Agency`" in cypher
    assert params["rows"][0]["labels"] == ["Government Agency"]

def test_build_ingest_cypher_node_label_injection_is_neutralized():
    # A backtick in the label is doubled so it can't close the quoted identifier
    # and break out into executable Cypher (no longer raises).
    nodes = [{"id": "n1", "label": "a`b", "name": "x", "attrs": {}}]
    cypher, _ = build_ingest_cypher(nodes, [])[0]
    assert ":`a``b`" in cypher

def test_build_ingest_cypher_edge_label_injection_is_neutralized():
    edges = [{"id": "e1", "src": "n1", "dst": "n2", "label": "a; DROP", "attrs": {}}]
    cypher, _ = build_ingest_cypher([], edges)[0]
    assert "[x:`a; DROP`" in cypher  # quoted -> the ';' is a literal identifier char


# ---------------------------------------------------------------------------
# FalkorDBAdapter.ingest_elements -- live (SKIP if FalkorDB unreachable).
# ---------------------------------------------------------------------------

from xgraph_gateway import config
from xgraph_gateway.adapters.falkordb_adapter import FalkorDBAdapter

_TEST_GRAPH = "extract_ingest_test_graph"

def _adapter_or_skip():
    try:
        a = FalkorDBAdapter(config.load_settings())
        a.list_graphs()
        return a
    except Exception as e:
        pytest.skip(f"FalkorDB unreachable: {e}")

@pytest.fixture
def live_adapter():
    a = _adapter_or_skip()
    yield a
    try:
        a._graph(_TEST_GRAPH).delete()
    except Exception:
        pass

def test_ingest_elements_creates_nodes_and_edges(live_adapter):
    nodes = [
        {"id": "ing-n1", "label": "Person", "name": "Ada Lovelace", "attrs": {"born": 1815}},
        {"id": "ing-n2", "label": "Organization", "name": "Analytical Engine Co", "attrs": {}},
    ]
    edges = [
        {"id": "ing-e1", "src": "ing-n1", "dst": "ing-n2", "label": "WORKS_AT", "attrs": {}},
    ]
    out = live_adapter.ingest_elements(_TEST_GRAPH, nodes, edges)
    assert out["nodes"] >= 2
    assert out["edges"] >= 1
    assert out["nodes_created"] >= 2
    assert out["edges_created"] >= 1
    assert set(out["labels"]["node_labels"]) == {"Person", "Organization"}
    assert out["labels"]["edge_labels"] == ["WORKS_AT"]

    result = live_adapter.run_query(
        _TEST_GRAPH, "MATCH (n:Entity {NODE: 'ing-n1'}) RETURN n.NODE, n.LABEL")
    # NODE is the readable identity; the redundant `name` property was dropped.
    # n.LABEL is the full label vector (array), not a scalar (Task 7).
    assert result["rows"] == [["ing-n1", ["Person"]]]

def test_ingest_elements_merge_does_not_double_on_rerun(live_adapter):
    nodes = [{"id": "ing-n1", "label": "Person", "name": "Ada Lovelace", "attrs": {}}]
    edges = []
    live_adapter.ingest_elements(_TEST_GRAPH, nodes, edges)
    counts_before = live_adapter._counts(live_adapter._graph(_TEST_GRAPH))
    out = live_adapter.ingest_elements(_TEST_GRAPH, nodes, edges)
    counts_after = live_adapter._counts(live_adapter._graph(_TEST_GRAPH))
    # Re-running with the same id MERGEs the existing node -- it's still
    # reported as present ("nodes"), but MERGE matched rather than created
    # (proves accumulate/idempotent), and the graph itself doesn't double.
    assert out["nodes"] >= 1
    assert out["nodes_created"] == 0
    assert counts_after == counts_before
