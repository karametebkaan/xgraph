import pytest
from xgraph_gateway.adapters.kinetica_adapter import _rows_to_result

def test_rows_to_result_shapes_columns_and_rows():
    rows = [{"NODE": "b1", "risk": 90}, {"NODE": "b2", "risk": 40}]
    assert _rows_to_result(rows) == {"columns": ["NODE", "risk"], "rows": [["b1", 90], ["b2", 40]]}

def test_rows_to_result_empty():
    assert _rows_to_result([]) == {"columns": [], "rows": []}

from xgraph_gateway import config
from xgraph_gateway.adapters.kinetica_adapter import KineticaAdapter

def _adapter_or_skip():
    s = config.load_settings()
    if not s.kinetica_url:
        pytest.skip("KINETICA_URL not set")
    try:
        a = KineticaAdapter(s); a.list_graphs(); return a
    except Exception as e:
        pytest.skip(f"Kinetica unreachable: {e}")

def test_live_kinetica_count_query():
    a = _adapter_or_skip()
    out = a.run_query("", "SELECT COUNT(*) AS c FROM expero.vertexes WHERE label = 'bank'")
    assert out["columns"] == ["c"]
    assert out["rows"][0][0] > 0


# ---------------------------------------------------------------------------
# get_schema display-mode options (Full / NKey / EKey) -- fake db, no live call.
# ---------------------------------------------------------------------------

class _CapturingDb:
    """Fakes show_graph enough for get_schema: captures the options passed in
    and returns a minimal (empty) schema response."""
    def __init__(self):
        self.captured_options = None
    def show_graph(self, graph_name="", options=None):
        self.captured_options = options
        return {"info": {"dot": "digraph {}", "labeljson": ""}}

def _adapter_with_capturing_db():
    adapter = KineticaAdapter.__new__(KineticaAdapter)
    db = _CapturingDb()
    adapter._db = db
    return adapter, db

def test_get_schema_default_options_disable_both_labelkeys():
    adapter, db = _adapter_with_capturing_db()
    adapter.get_schema("expero.banking_graph")
    assert db.captured_options == {
        "export_graph_schema": "true",
        "schema_node_labelkeys": "false",
        "schema_edge_labelkeys": "false",
    }

def test_get_schema_nkey_on_omits_node_labelkeys_false():
    adapter, db = _adapter_with_capturing_db()
    adapter.get_schema("expero.banking_graph", {"nkey": True})
    assert db.captured_options == {
        "export_graph_schema": "true",
        "schema_edge_labelkeys": "false",
    }

def test_get_schema_ekey_on_omits_edge_labelkeys_false():
    adapter, db = _adapter_with_capturing_db()
    adapter.get_schema("expero.banking_graph", {"ekey": True})
    assert db.captured_options == {
        "export_graph_schema": "true",
        "schema_node_labelkeys": "false",
    }

def test_get_schema_full_on_adds_full_search():
    adapter, db = _adapter_with_capturing_db()
    adapter.get_schema("expero.banking_graph", {"full": True})
    assert db.captured_options == {
        "export_graph_schema": "true",
        "schema_node_labelkeys": "false",
        "schema_edge_labelkeys": "false",
        "schema_full_search": "true",
    }

def test_get_schema_all_modes_on():
    adapter, db = _adapter_with_capturing_db()
    adapter.get_schema("expero.banking_graph", {"full": True, "nkey": True, "ekey": True})
    assert db.captured_options == {
        "export_graph_schema": "true",
        "schema_full_search": "true",
    }

def test_live_get_schema_full_mode_returns_nontrivial_dot():
    a = _adapter_or_skip()
    schema = a.get_schema("expero.banking_graph", {"full": True})
    dot = schema["dot"]
    assert isinstance(dot, str) and len(dot) > 20


# ---------------------------------------------------------------------------
# graph_from_gql_result -- hop-column parser (Kinetica Graph Explorer's
# "Visualization" transform). Canned columns shaped exactly like a live
# `resp.info['gql_result']` probe against expero.banking_graph (2-hop
# bank -[performed]-> wire_message -[is_for_transaction]-> banking_transaction).
# ---------------------------------------------------------------------------

from xgraph_gateway.adapters.kinetica_adapter import (
    graph_from_gql_result, _hop_indices, _first_label, _is_gql_graph_query)

def _canned_gql_result():
    return {
        "column_headers": [
            "QUERY_EDGE_ID_HOP_1", "NODE1_HOP_1", "NODE2_HOP_1", "PATH_ID_HOP_1",
            "RING_ID_HOP_1", "NODE1_LABELS_HOP_1", "NODE2_LABELS_HOP_1", "EDGE_LABELS_HOP_1",
            "QUERY_EDGE_ID_HOP_2", "NODE1_HOP_2", "NODE2_HOP_2", "PATH_ID_HOP_2",
            "RING_ID_HOP_2", "NODE1_LABELS_HOP_2", "NODE2_LABELS_HOP_2", "EDGE_LABELS_HOP_2",
        ],
        "column_datatypes": ["long", "char64", "char64", "long", "int", "string", "string", "string",
                              "long", "char64", "char64", "long", "int", "string", "string", "string"],
        "column_1": [-5021232108262253909, 975310795581561515],
        "column_2": ["bank1", "bank1"],
        "column_3": ["wire1", "wire2"],
        "column_4": [1, 2],
        "column_5": [1, 1],
        "column_6": ['["bank"]', '["bank"]'],
        "column_7": ['["wire_message"]', '["wire_message"]'],
        "column_8": ['["performed"]', '["performed"]'],
        "column_9": [-4033536412810602661, 6430577321384744795],
        "column_10": ["wire1", "wire2"],
        "column_11": ["tx1", "tx2"],
        "column_12": [1, 2],
        "column_13": [2, 2],
        "column_14": ['["wire_message"]', '["wire_message"]'],
        "column_15": ['["banking_transaction"]', '["banking_transaction"]'],
        "column_16": ['["is_for_transaction"]', '["is_for_transaction"]'],
    }

def test_hop_indices_detects_both_hops():
    assert _hop_indices(_canned_gql_result()["column_headers"]) == [1, 2]

def test_first_label_parses_json_array():
    assert _first_label('["bank"]') == "bank"
    assert _first_label("bank") == "bank"
    assert _first_label(None) is None
    assert _first_label("") is None

def test_graph_from_gql_result_builds_nodes_and_edges():
    out = graph_from_gql_result(_canned_gql_result())
    ids = {n["id"] for n in out["nodes"]}
    assert ids == {"bank1", "wire1", "wire2", "tx1", "tx2"}
    labels = {n["id"]: n["label"] for n in out["nodes"]}
    assert labels["bank1"] == "bank"
    assert labels["wire1"] == "wire_message"
    assert labels["tx1"] == "banking_transaction"
    edges = {(e["source"], e["target"], e["type"]) for e in out["edges"]}
    assert edges == {
        ("bank1", "wire1", "performed"), ("bank1", "wire2", "performed"),
        ("wire1", "tx1", "is_for_transaction"), ("wire2", "tx2", "is_for_transaction"),
    }

def test_graph_from_gql_result_dedupes_shared_node():
    # bank1 appears as NODE1 in every HOP_1 row -- de-duped to a single node.
    out = graph_from_gql_result(_canned_gql_result())
    assert sum(1 for n in out["nodes"] if n["id"] == "bank1") == 1

def test_graph_from_gql_result_empty_for_plain_sql_response():
    # A plain SELECT's info has no HOP columns at all.
    assert graph_from_gql_result({"column_headers": []}) == {"nodes": [], "edges": []}

def test_graph_from_gql_result_never_raises_on_malformed_columns():
    # NODE1_HOP_1 present in headers but its column data is missing/None.
    assert graph_from_gql_result({"column_headers": ["NODE1_HOP_1"], "column_1": None}) == \
        {"nodes": [], "edges": []}

def test_graph_from_gql_result_never_raises_on_none_input():
    # A caller passing a non-dict (e.g. a parse upstream returned None) must
    # get an empty graph back, not an exception.
    assert graph_from_gql_result(None) == {"nodes": [], "edges": []}

def test_graph_from_gql_result_empty_dict():
    assert graph_from_gql_result({}) == {"nodes": [], "edges": []}


# ---------------------------------------------------------------------------
# _is_gql_graph_query -- gates the extra execute_sql_and_decode call so a
# plain (potentially mutating) SQL statement is never re-executed.
# ---------------------------------------------------------------------------

def test_is_gql_graph_query_true_for_graph_match():
    assert _is_gql_graph_query(
        "GRAPH expero.banking_graph MATCH (a:bank) -[ab:performed]-> (b:wire_message) RETURN a,b")

def test_is_gql_graph_query_true_for_graph_table_wrapper():
    assert _is_gql_graph_query("SELECT * FROM graph_table(GRAPH expero.banking_graph MATCH ...)")

def test_is_gql_graph_query_false_for_plain_select():
    assert not _is_gql_graph_query("SELECT COUNT(*) FROM expero.vertexes")

def test_is_gql_graph_query_false_for_dml():
    assert not _is_gql_graph_query("INSERT INTO expero.vertexes VALUES (1)")


# ---------------------------------------------------------------------------
# run_query graph wiring -- live GQL query against expero.banking_graph.
# ---------------------------------------------------------------------------

def test_live_kinetica_gql_query_returns_graph():
    a = _adapter_or_skip()
    bank_id_result = a.run_query(
        "", "SELECT id AS n FROM expero.vertexes WHERE label = 'bank' LIMIT 1")
    if not bank_id_result["rows"]:
        pytest.skip("no bank vertex found to probe")
    bank_id = bank_id_result["rows"][0][0]
    out = a.run_query(
        "expero.banking_graph",
        "GRAPH expero.banking_graph "
        f"MATCH (a:bank WHERE (a.NODE = '{bank_id}')) "
        "-[ab:performed]-> (b:wire_message) "
        "RETURN a.NODE as bank, b.NODE as wire, ab.LABEL as ablabel")
    assert out["graph"]["nodes"], "expected at least one node from a GQL GRAPH MATCH query"
    assert out["graph"]["edges"], "expected at least one edge from a GQL GRAPH MATCH query"
    assert all(e["type"] == "performed" for e in out["graph"]["edges"])
