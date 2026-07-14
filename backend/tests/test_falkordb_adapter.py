import pytest
from xgraph_gateway.adapters.falkordb_adapter import _column_names, _dot_from_triples

def test_column_names_decodes_header():
    header = [[1, b"NODE"], [1, "risk"]]
    assert _column_names(header) == ["NODE", "risk"]

def test_dot_from_triples():
    dot = _dot_from_triples([("bank", "performed", "wire_message")])
    assert dot.startswith("digraph")
    assert '"bank" -> "wire_message"' in dot
    assert 'label="performed"' in dot

from xgraph_gateway import config
from xgraph_gateway.adapters.falkordb_adapter import FalkorDBAdapter

def _adapter_or_skip():
    try:
        a = FalkorDBAdapter(config.load_settings())
        a.list_graphs()
        return a
    except Exception as e:
        pytest.skip(f"FalkorDB unreachable: {e}")

def test_live_banking_graph_query():
    a = _adapter_or_skip()
    if "banking_graph" not in a.list_graphs():
        pytest.skip("banking_graph not loaded")
    out = a.run_query("banking_graph", "MATCH (b:bank) RETURN b.NODE AS NODE LIMIT 3")
    assert out["columns"] == ["NODE"]
    assert len(out["rows"]) == 3

def test_live_schema_has_bank_label():
    a = _adapter_or_skip()
    if "banking_graph" not in a.list_graphs():
        pytest.skip("banking_graph not loaded")
    sch = a.get_schema("banking_graph")
    assert "bank" in sch["labels"]
    assert sch["dot"].startswith("digraph")

def test_live_query_returns_graph():
    a = _adapter_or_skip()
    if "banking_graph" not in a.list_graphs():
        pytest.skip("banking_graph not loaded")
    out = a.run_query(
        "banking_graph",
        "MATCH (a:bank)-[r:performed]->(w:wire_message) RETURN a,r,w LIMIT 5")
    assert len(out["graph"]["nodes"]) > 0
    assert len(out["graph"]["edges"]) > 0
    for n in out["graph"]["nodes"]:
        assert set(n.keys()) == {"id", "label", "props"}
    for e in out["graph"]["edges"]:
        assert set(e.keys()) == {"id", "source", "target", "type"}
        assert e["type"] == "performed"


# ---------------------------------------------------------------------------
# extract_graph -- unit tests against the real falkordb Node/Edge/Path
# classes (plain constructible objects, no live connection needed).
# ---------------------------------------------------------------------------

from falkordb import Node as FalkorNode, Edge as FalkorEdge, Path as FalkorPath
from xgraph_gateway.adapters.falkordb_adapter import extract_graph, _serialize_cell, _collect_id_map

def _bank_wire_result_set():
    bank = FalkorNode(node_id=1, labels=["bank"], properties={"NODE": "b1", "bank_name": "Acme"})
    wire = FalkorNode(node_id=2, labels=["wire_message"], properties={"NODE": "w1", "risk": 90})
    edge = FalkorEdge(bank.id, "performed", wire.id, edge_id=10, properties={"ID": "e1"})
    return [[bank, edge, wire]]

def test_extract_graph_from_node_and_edge_cells():
    out = extract_graph(_bank_wire_result_set())
    assert out["nodes"] == [
        {"id": "b1", "label": "bank", "props": {"NODE": "b1", "bank_name": "Acme"}},
        {"id": "w1", "label": "wire_message", "props": {"NODE": "w1", "risk": 90}},
    ]
    assert out["edges"] == [{"id": "e1", "source": "b1", "target": "w1", "type": "performed"}]

def test_extract_graph_from_path_cell():
    bank = FalkorNode(node_id=1, labels=["bank"], properties={"NODE": "b1"})
    wire = FalkorNode(node_id=2, labels=["wire_message"], properties={"NODE": "w1"})
    edge = FalkorEdge(bank.id, "performed", wire.id, edge_id=10, properties={})
    path = FalkorPath([bank, wire], [edge])
    out = extract_graph([[path]])
    assert {n["id"] for n in out["nodes"]} == {"b1", "w1"}
    assert out["edges"] == [{"id": "10", "source": "b1", "target": "w1", "type": "performed"}]

def test_extract_graph_prefers_specific_label_over_shared_entity_label():
    # Every node in this data model carries a shared `Entity` label plus its
    # specific label (e.g. `bank`), and a `LABEL` property mirroring it. The
    # extracted node's "label" must be the specific one, not "Entity".
    bank = FalkorNode(node_id=1, labels=["Entity", "bank"],
                       properties={"LABEL": "bank", "NODE": "b1"})
    out = extract_graph([[bank]])
    assert out["nodes"] == [{"id": "b1", "label": "bank",
                              "props": {"LABEL": "bank", "NODE": "b1"}}]

def test_extract_graph_from_path_cell_prefers_specific_label():
    bank = FalkorNode(node_id=1, labels=["Entity", "bank"],
                       properties={"LABEL": "bank", "NODE": "b1"})
    wire = FalkorNode(node_id=2, labels=["Entity", "wire_message"],
                       properties={"LABEL": "wire_message", "NODE": "w1"})
    edge = FalkorEdge(bank.id, "performed", wire.id, edge_id=10, properties={})
    path = FalkorPath([bank, wire], [edge])
    out = extract_graph([[path]])
    labels = {n["id"]: n["label"] for n in out["nodes"]}
    assert labels == {"b1": "bank", "w1": "wire_message"}

def test_extract_graph_dedupes_by_id():
    bank = FalkorNode(node_id=1, labels=["bank"], properties={"NODE": "b1"})
    wire = FalkorNode(node_id=2, labels=["wire_message"], properties={"NODE": "w1"})
    edge = FalkorEdge(bank.id, "performed", wire.id, edge_id=10, properties={"ID": "e1"})
    out = extract_graph([[bank, edge, wire], [bank, edge, wire]])
    assert len(out["nodes"]) == 2
    assert len(out["edges"]) == 1

def test_extract_graph_empty_for_scalar_only_result_set():
    assert extract_graph([["b1", 90], ["b2", 40]]) == {"nodes": [], "edges": []}

def test_extract_graph_never_raises_on_malformed_cell():
    # A Node whose `.properties` got corrupted to None -- `.get()` on it
    # would raise AttributeError; extract_graph must swallow that.
    bank = FalkorNode(node_id=1, labels=["bank"], properties={"NODE": "b1"})
    bank.properties = None
    assert extract_graph([[bank]]) == {"nodes": [], "edges": []}

def test_serialize_cell_makes_rows_json_safe():
    bank = FalkorNode(node_id=1, labels=["bank"], properties={"NODE": "b1"})
    id_map = _collect_id_map([[bank]])
    row = [_serialize_cell(bank, id_map), _serialize_cell("scalar", id_map)]
    assert row == [{"id": "b1", "label": "bank", "props": {"NODE": "b1"}}, "scalar"]


# ---------------------------------------------------------------------------
# run_query -- graph-typed columns (Node/Edge/Path) must be excluded from the
# tabular columns/rows, while still feeding the `graph` viz extraction. Uses
# a fake graph/query_result so it needs no live FalkorDB connection.
# ---------------------------------------------------------------------------

def test_run_query_excludes_graph_typed_column_from_table_but_populates_graph():
    bank = FalkorNode(node_id=1, labels=["bank"], properties={"NODE": "b1"})
    wire = FalkorNode(node_id=2, labels=["wire_message"], properties={"NODE": "w1"})
    edge = FalkorEdge(bank.id, "performed", wire.id, edge_id=10, properties={"ID": "e1"})
    path = FalkorPath([bank, wire], [edge])

    class FakeQueryResult:
        header = [[1, b"a_node"], [1, b"b_node"], [1, b"p"]]
        result_set = [["b1", "w1", path]]

    class FakeGraph:
        def query(self, cypher, timeout=60000):
            return FakeQueryResult()

    adapter = object.__new__(FalkorDBAdapter)
    adapter._graph = lambda graph: FakeGraph()

    out = adapter.run_query("banking_graph", "MATCH p=(a:bank)-[ab:performed]->(b:wire_message) RETURN a.NODE as a_node, b.NODE as b_node, p")

    # The `p` (Path) column is dropped from the scalar table...
    assert out["columns"] == ["a_node", "b_node"]
    assert out["rows"] == [["b1", "w1"]]
    # ...but still drives the graph extraction.
    assert {n["id"] for n in out["graph"]["nodes"]} == {"b1", "w1"}
    assert out["graph"]["edges"] == [{"id": "e1", "source": "b1", "target": "w1", "type": "performed"}]

def test_run_query_pure_scalar_result_keeps_all_columns_and_empty_graph():
    class FakeQueryResult:
        header = [[1, b"a_node"], [1, b"risk"]]
        result_set = [["b1", 90], ["b2", 40]]

    class FakeGraph:
        def query(self, cypher, timeout=60000):
            return FakeQueryResult()

    adapter = object.__new__(FalkorDBAdapter)
    adapter._graph = lambda graph: FakeGraph()

    out = adapter.run_query("banking_graph", "MATCH (a:bank) RETURN a.NODE as a_node, a.risk as risk")
    assert out["columns"] == ["a_node", "risk"]
    assert out["rows"] == [["b1", 90], ["b2", 40]]
    assert out["graph"] == {"nodes": [], "edges": []}

def test_run_query_pure_object_result_has_empty_table_and_populated_graph():
    bank = FalkorNode(node_id=1, labels=["bank"], properties={"NODE": "b1"})

    class FakeQueryResult:
        header = [[1, b"a"]]
        result_set = [[bank]]

    class FakeGraph:
        def query(self, cypher, timeout=60000):
            return FakeQueryResult()

    adapter = object.__new__(FalkorDBAdapter)
    adapter._graph = lambda graph: FakeGraph()

    out = adapter.run_query("banking_graph", "MATCH (a:bank) RETURN a")
    assert out["columns"] == []
    assert out["rows"] == [[]]
    assert len(out["graph"]["nodes"]) == 1

def test_graph_typed_columns_guards_ragged_rows():
    from xgraph_gateway.adapters.falkordb_adapter import _graph_typed_columns
    bank = FalkorNode(node_id=1, labels=["bank"], properties={"NODE": "b1"})
    # Second row is short (missing the 2nd column) -- must not raise.
    result_set = [[None, bank], ["scalar"]]
    assert _graph_typed_columns(result_set, 2) == {1}
