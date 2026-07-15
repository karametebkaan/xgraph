import pytest

from graph_loader.mapper import MappingError
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
    statements = build_ingest_cypher(_nodes(), [])
    by_label = {params["label"]: (cypher, params) for cypher, params in statements}
    assert set(by_label) == {"Person", "Organization"}
    cypher, params = by_label["Person"]
    assert "UNWIND $rows AS r" in cypher
    assert "MERGE (n:Entity {NODE: r.id})" in cypher
    assert "SET n:Person" in cypher
    assert "n.LABEL = $label" in cypher
    assert "n.name = r.name" in cypher
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
    assert "MERGE (a)-[x:WORKS_AT {ID: e.id}]->(b)" in cypher
    assert "x.LABEL = $label" in cypher
    assert "x += e.attrs" in cypher
    assert params["rows"] == [{"id": "e1", "src": "n1", "dst": "n3", "attrs": {"since": 2018}}]

def test_build_ingest_cypher_labels_pass_through_safe_ident():
    statements = build_ingest_cypher(_nodes(), _edges())
    for cypher, params in statements:
        # Every interpolated label is a bare safe_ident token (alnum/underscore).
        assert params["label"].replace("_", "").isalnum()

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
    node_stmt = next(p for c, p in statements if p["label"] == "Person")
    assert node_stmt["rows"] == [{"id": "n1", "name": "Real", "attrs": {}}]
    edge_stmt = next(p for c, p in statements if p["label"] == "REL")
    assert edge_stmt["rows"] == [{"id": "e2", "src": "n1", "dst": "n1", "attrs": {}}]

def test_build_ingest_cypher_empty_inputs_yield_no_statements():
    assert build_ingest_cypher([], []) == []

def test_build_ingest_cypher_malicious_node_label_raises():
    nodes = [{"id": "n1", "label": "a` b", "name": "x", "attrs": {}}]
    with pytest.raises(MappingError):
        build_ingest_cypher(nodes, [])

def test_build_ingest_cypher_malicious_edge_label_raises():
    edges = [{"id": "e1", "src": "n1", "dst": "n2", "label": "a; DROP", "attrs": {}}]
    with pytest.raises(MappingError):
        build_ingest_cypher([], edges)


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
    assert set(out["labels"]["node_labels"]) == {"Person", "Organization"}
    assert out["labels"]["edge_labels"] == ["WORKS_AT"]

    result = live_adapter.run_query(
        _TEST_GRAPH, "MATCH (n:Entity {NODE: 'ing-n1'}) RETURN n.NODE, n.LABEL, n.name")
    assert result["rows"] == [["ing-n1", "Person", "Ada Lovelace"]]

def test_ingest_elements_merge_does_not_double_on_rerun(live_adapter):
    nodes = [{"id": "ing-n1", "label": "Person", "name": "Ada Lovelace", "attrs": {}}]
    edges = []
    live_adapter.ingest_elements(_TEST_GRAPH, nodes, edges)
    counts_before = live_adapter._counts(live_adapter._graph(_TEST_GRAPH))
    out = live_adapter.ingest_elements(_TEST_GRAPH, nodes, edges)
    counts_after = live_adapter._counts(live_adapter._graph(_TEST_GRAPH))
    # Re-running with the same id MERGEs the existing node -- no new node created.
    assert out["nodes"] == 0
    assert counts_after == counts_before
