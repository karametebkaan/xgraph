import json
import pytest
from xgraph_gateway.adapters.kinetica_adapter import _rows_to_result
from xgraph_gateway.adapters import kinetica_adapter as ka


# ---------------------------------------------------------------------------
# Task 8: array-LABEL + provenance on the node backing table, kgr-style
# label_keys grouping fed into CREATE GRAPH. Pure builders only -- no live
# Kinetica needed.
# ---------------------------------------------------------------------------

def test_node_table_sql_declares_array_label_and_provenance():
    sql = ka.create_table_sql("s.g_nodes", "node")
    assert "LABEL VARCHAR[]" in sql
    assert "label_raw VARCHAR[]" in sql
    assert "first_seen_ts" in sql and "last_seen_ts" in sql


def test_node_rows_emit_label_vector():
    nodes = [{"id": "n1", "name": "Anthropic", "label": "Company",
              "labels": ["Company", "AI"], "label_raw": ["Firm", "AI"], "attrs": {}}]
    rows = ka.node_rows(nodes)
    assert rows[0]["LABEL"] == ["Company", "AI"]
    assert rows[0]["label_raw"] == ["Firm", "AI"]


def test_node_rows_falls_back_to_single_label_when_no_labels_vector():
    nodes = [{"id": "n1", "name": "Ada", "label": "Person", "attrs": {}}]
    rows = ka.node_rows(nodes)
    assert rows[0]["LABEL"] == ["Person"]
    assert rows[0]["label_raw"] == ["Person"]


def test_node_rows_include_provenance_timestamps():
    nodes = [{"id": "n1", "name": "Ada", "label": "Person", "attrs": {}}]
    rows = ka.node_rows(nodes)
    assert isinstance(rows[0]["first_seen_ts"], str)
    assert rows[0]["first_seen_ts"] == rows[0]["last_seen_ts"]


def test_node_rows_with_attrs_also_emits_label_vector_and_provenance():
    # The ACTUAL insert payload ingest_elements upserts -- must carry the
    # same vector/provenance shape as node_rows, plus evolved attr columns.
    nodes = [{"id": "n1", "name": "Anthropic", "label": "Company",
              "labels": ["Company", "AI"], "attrs": {"founded": 2021}}]
    rows = ka._node_rows_with_attrs(nodes, {"founded": "BIGINT"})
    assert rows[0]["LABEL"] == ["Company", "AI"]
    assert rows[0]["label_raw"] == ["Company", "AI"]
    assert rows[0]["founded"] == 2021
    assert "first_seen_ts" in rows[0] and "last_seen_ts" in rows[0]


def test_node_base_cols_excludes_provenance_and_label_raw_from_attrs():
    # These columns must never be mistaken for discovered attribute columns.
    for col in ("label_raw", "first_seen_ts", "last_seen_ts"):
        assert col in ka._NODE_BASE_COLS


def test_label_keys_rows_groups_distinct_labels_under_default_axis():
    nodes = [
        {"id": "n1", "label": "Company", "labels": ["Company", "AI"], "attrs": {}},
        {"id": "n2", "label": "Person", "labels": ["Person"], "attrs": {}},
    ]
    rows = ka.label_keys_rows(nodes)
    assert rows == [{"label_key": "EntityType", "label": ["AI", "Company", "Person"]}]


def test_label_keys_rows_empty_when_no_labels():
    assert ka.label_keys_rows([{"id": "n1", "attrs": {}}]) == []


def test_create_graph_sql_without_label_keys_table_unchanged():
    sql = ka.create_graph_sql("g", "g_nodes", "g_edges")
    assert "NODES => INPUT_TABLES((SELECT NODE, LABEL" in sql
    # No label_keys_table passed -> no LABEL_KEY axis-grouping select at all.
    assert "LABEL_KEY" not in sql


def test_create_graph_sql_with_label_keys_table_adds_sibling_grouping_select():
    sql = ka.create_graph_sql("g", "g_nodes", "g_edges", label_keys_table="g_label_keys")
    # kgr/graph.sql form: the LABEL_KEY grouping is a SIBLING select inside
    # the same NODES => INPUT_TABLES(...) list, not appended after it.
    assert "NODES => INPUT_TABLES(" in sql
    assert "(SELECT label_key AS LABEL_KEY, label AS LABEL FROM g_label_keys)" in sql
    assert "(SELECT NODE, LABEL, name AS entity_name FROM g_nodes)" in sql
    # Both selects must be inside ONE INPUT_TABLES(...) grouping for NODES.
    nodes_clause_start = sql.index("NODES =>")
    edges_clause_start = sql.index("EDGES =>")
    nodes_clause = sql[nodes_clause_start:edges_clause_start]
    assert "g_label_keys" in nodes_clause and "g_nodes" in nodes_clause


def test_create_graph_sql_rejects_bad_label_keys_table():
    with pytest.raises(Exception):
        ka.create_graph_sql("g", "g_nodes", "g_edges", label_keys_table="a; DROP TABLE x")

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


def test_get_schema_includes_properties_key_best_effort():
    # Kinetica's show_graph labeljson carries label names + counts only, not
    # column names, so `properties` is populated best-effort ({} for now) --
    # this must not break existing get_schema callers/tests.
    adapter, _db = _adapter_with_capturing_db()
    schema = adapter.get_schema("expero.banking_graph")
    assert schema["properties"] == {}


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


# ---------------------------------------------------------------------------
# Task 8 live round-trip: ingest_elements stores a multi-label node's LABEL
# as an array (structural + facet label) and materializes a label_keys
# table, both fed into a live CREATE GRAPH. THROWAWAY graph name, dropped
# (graph + node/edge/label_keys backing tables) in a finally block -- never
# touches kgr.*/ki_home.* or any pre-existing graph. SKIPs cleanly if
# Kinetica is unreachable.
# ---------------------------------------------------------------------------

from xgraph_gateway.adapters.kinetica_adapter import (
    KineticaAdapter, edge_table_name, label_keys_table_name, node_table_name,
)

_TASK8_TEST_GRAPH = "xgraph_task8_probe"

def _task8_adapter_or_skip():
    from xgraph_gateway import config
    s = config.load_settings()
    if not s.kinetica_url:
        pytest.skip("KINETICA_URL not set")
    try:
        a = KineticaAdapter(s)
        a.list_graphs()
        return a
    except Exception as e:
        pytest.skip(f"Kinetica unreachable: {e}")

def _task8_cleanup(adapter, graph_name=_TASK8_TEST_GRAPH):
    try:
        adapter._db.delete_graph(graph_name=graph_name)
    except Exception:
        pass
    for table in (node_table_name(graph_name), edge_table_name(graph_name),
                  label_keys_table_name(graph_name)):
        try:
            adapter._db.execute_sql(f"DROP TABLE IF EXISTS {table}")
        except Exception:
            pass

def test_live_ingest_elements_stores_multilabel_node_array_and_label_keys():
    a = _task8_adapter_or_skip()
    _task8_cleanup(a)
    try:
        nodes = [
            {"id": "t8-n1", "label": "Company", "labels": ["Company", "AI"],
             "label_raw": ["Firm", "AI"], "name": "Anthropic", "attrs": {}},
            {"id": "t8-n2", "label": "Person", "labels": ["Person"],
             "name": "Dario Amodei", "attrs": {}},
        ]
        edges = [{"id": "t8-e1", "src": "t8-n2", "dst": "t8-n1",
                  "label": "WORKS_AT", "attrs": {}}]
        out = a.ingest_elements(_TASK8_TEST_GRAPH, nodes, edges)
        assert out["nodes"] == 2
        assert out["edges"] == 1

        # Read the backing node table directly (LABEL/label_raw round-trip
        # as JSON-array strings through the plain-SQL read path, per a live
        # probe of insert_records_json -> execute_sql_and_decode).
        node_table = node_table_name(_TASK8_TEST_GRAPH)
        rows = list(a._src.rows(
            f"SELECT NODE, LABEL, label_raw, first_seen_ts, last_seen_ts "
            f"FROM {node_table} WHERE NODE = 't8-n1'"))
        assert len(rows) == 1
        row = rows[0]

        def _as_list(v):
            if isinstance(v, list):
                return v
            return json.loads(v) if v else []

        labels = _as_list(row["LABEL"])
        label_raw = _as_list(row["label_raw"])
        assert set(labels) == {"Company", "AI"}
        assert set(label_raw) == {"Firm", "AI"}
        assert row["first_seen_ts"] is not None
        assert row["last_seen_ts"] is not None

        # label_keys materialized: one EntityType row grouping every distinct
        # label seen this call (structural + facet, undifferentiated for now).
        lk_table = label_keys_table_name(_TASK8_TEST_GRAPH)
        lk_rows = list(a._src.rows(f"SELECT label_key, label FROM {lk_table}"))
        assert len(lk_rows) == 1
        assert lk_rows[0]["label_key"] == "EntityType"
        assert set(_as_list(lk_rows[0]["label"])) == {"Company", "AI", "Person"}

        # The graph itself was (re)created without error -- CREATE GRAPH's
        # NODES clause included the label_keys grouping select successfully.
        graphs = a.list_graphs()
        assert any(_TASK8_TEST_GRAPH in g for g in graphs)
    finally:
        _task8_cleanup(a)


# ---------------------------------------------------------------------------
# Task 8 fix: label_keys must accumulate across ingest_elements calls, not
# just reflect the last call's payload. ingest_elements is invoked once per
# extracted document -- a second /extract into the same graph that doesn't
# re-mention an earlier document's label must not silently drop that label
# from the CREATE GRAPH axis grouping (the nodes carrying it are still in
# the node table). THROWAWAY graph, dropped (graph + backing tables) in a
# finally block -- never touches kgr.*/ki_home.*/expero.* or any
# pre-existing graph. SKIPs cleanly if Kinetica is unreachable.
# ---------------------------------------------------------------------------

_TASK8_CUMULATIVE_TEST_GRAPH = "xgraph_task8_cumulative_probe"

def test_live_label_keys_accumulate_across_ingest_calls():
    a = _task8_adapter_or_skip()
    graph = _TASK8_CUMULATIVE_TEST_GRAPH
    _task8_cleanup(a, graph)
    try:
        # First call: only "Alpha" exists in the node table.
        out1 = a.ingest_elements(
            graph,
            [{"id": "c8-n1", "label": "Alpha", "labels": ["Alpha"],
              "name": "one", "attrs": {}}],
            [])
        assert out1["nodes"] == 1

        # Second call: only "Beta" this time -- "Alpha" is NOT re-mentioned,
        # but its node still lives in the node table from the first call.
        out2 = a.ingest_elements(
            graph,
            [{"id": "c8-n2", "label": "Beta", "labels": ["Beta"],
              "name": "two", "attrs": {}}],
            [])
        assert out2["nodes"] == 1

        lk_table = label_keys_table_name(graph)
        lk_rows = list(a._src.rows(f"SELECT label_key, label FROM {lk_table}"))
        assert len(lk_rows) == 1
        assert lk_rows[0]["label_key"] == "EntityType"

        def _as_list(v):
            if isinstance(v, list):
                return v
            return json.loads(v) if v else []

        labels = set(_as_list(lk_rows[0]["label"]))
        # Both labels must be present -- proof that label_keys is rebuilt
        # from the node table's accumulated state, not just this call's rows.
        assert {"Alpha", "Beta"} <= labels
    finally:
        _task8_cleanup(a, graph)
