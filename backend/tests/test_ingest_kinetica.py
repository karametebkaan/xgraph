import json
import pytest

from graph_loader.mapper import MappingError
from xgraph_gateway.adapters.kinetica_adapter import (
    create_graph_sql,
    create_schema_sql,
    create_table_sql,
    edge_rows,
    edge_table_name,
    node_rows,
    node_table_name,
)


# ---------------------------------------------------------------------------
# node_table_name / edge_table_name -- pure, no DB. Schema-qualify by
# validating each dotted part via safe_ident, suffixing the last part.
# ---------------------------------------------------------------------------

def test_node_table_name_unqualified():
    assert node_table_name("extracted_graph") == "extracted_graph_nodes"

def test_edge_table_name_unqualified():
    assert edge_table_name("extracted_graph") == "extracted_graph_edges"

def test_node_table_name_schema_qualified():
    assert node_table_name("myschema.mygraph") == "myschema.mygraph_nodes"

def test_edge_table_name_schema_qualified():
    assert edge_table_name("myschema.mygraph") == "myschema.mygraph_edges"

def test_node_table_name_rejects_bad_identifier():
    with pytest.raises(MappingError):
        node_table_name("a` b")

def test_edge_table_name_rejects_bad_schema_part():
    with pytest.raises(MappingError):
        edge_table_name("bad schema.mygraph")


# ---------------------------------------------------------------------------
# create_schema_sql -- pure. None for unqualified graph names (no schema to
# create); a CREATE SCHEMA IF NOT EXISTS statement for dotted names.
# ---------------------------------------------------------------------------

def test_create_schema_sql_none_for_unqualified_graph():
    assert create_schema_sql("extracted_graph") is None

def test_create_schema_sql_for_qualified_graph():
    sql = create_schema_sql("myschema.mygraph")
    assert "CREATE SCHEMA IF NOT EXISTS" in sql
    assert "myschema" in sql

def test_create_schema_sql_rejects_bad_schema_part():
    with pytest.raises(MappingError):
        create_schema_sql("bad schema.mygraph")


# ---------------------------------------------------------------------------
# create_table_sql -- pure DDL builders for the node/edge backing tables.
# ---------------------------------------------------------------------------

def test_create_table_sql_node_shape():
    sql = create_table_sql("extracted_graph_nodes", "node")
    assert "CREATE TABLE IF NOT EXISTS extracted_graph_nodes" in sql
    assert "NODE" in sql
    assert "PRIMARY_KEY" in sql
    assert "SHARD_KEY" in sql
    assert "LABEL" in sql
    assert "name" in sql

def test_create_table_sql_edge_shape():
    sql = create_table_sql("extracted_graph_edges", "edge")
    assert "CREATE TABLE IF NOT EXISTS extracted_graph_edges" in sql
    assert "edge_key" in sql
    assert "PRIMARY_KEY" in sql
    assert "NODE1" in sql
    assert "NODE2" in sql
    assert "LABEL" in sql

def test_create_table_sql_rejects_unknown_kind():
    with pytest.raises(ValueError):
        create_table_sql("t", "bogus")


# ---------------------------------------------------------------------------
# create_graph_sql -- pure. Must reference both backing tables and the
# NODE1/NODE2 columns Kinetica's graph engine expects.
# ---------------------------------------------------------------------------

def test_create_graph_sql_contains_required_tokens():
    sql = create_graph_sql("extracted_graph", "extracted_graph_nodes", "extracted_graph_edges")
    assert "CREATE OR REPLACE DIRECTED GRAPH" in sql
    assert "extracted_graph" in sql
    assert "extracted_graph_nodes" in sql
    assert "extracted_graph_edges" in sql
    assert "NODE1" in sql
    assert "NODE2" in sql

def test_create_graph_sql_qualified_graph_name():
    sql = create_graph_sql("myschema.mygraph", "myschema.mygraph_nodes", "myschema.mygraph_edges")
    assert "myschema.mygraph" in sql

def test_create_graph_sql_rejects_bad_graph_name():
    with pytest.raises(MappingError):
        create_graph_sql("a; DROP TABLE x", "t_nodes", "t_edges")


# ---------------------------------------------------------------------------
# node_rows / edge_rows -- pure row-shaping for the insert_records_json
# payload. All entity data lives here (never string-interpolated into SQL).
# ---------------------------------------------------------------------------

def _nodes():
    return [
        {"id": "n1", "label": "Person", "name": "Jerome Powell", "attrs": {"role": "chair"}},
        {"id": "n2", "label": "Organization", "name": "Federal Reserve", "attrs": {}},
    ]

def _edges():
    return [
        {"id": "e1", "src": "n1", "dst": "n2", "label": "WORKS_AT", "attrs": {"since": 2018}},
    ]

def test_node_rows_shape():
    rows = node_rows(_nodes())
    assert rows == [
        {"NODE": "n1", "LABEL": "Person", "name": "Jerome Powell"},
        {"NODE": "n2", "LABEL": "Organization", "name": "Federal Reserve"},
    ]

def test_edge_rows_shape():
    rows = edge_rows(_edges())
    assert rows == [
        {"edge_key": "e1", "NODE1": "n1", "NODE2": "n2", "LABEL": "WORKS_AT"},
    ]

def test_node_rows_skips_null_id():
    nodes = [{"id": None, "label": "Person", "name": "Ghost", "attrs": {}}] + _nodes()
    rows = node_rows(nodes)
    assert [r["NODE"] for r in rows] == ["n1", "n2"]

def test_edge_rows_skips_null_id_src_or_dst():
    edges = [
        {"id": None, "src": "n1", "dst": "n2", "label": "REL", "attrs": {}},
        {"id": "e2", "src": None, "dst": "n2", "label": "REL", "attrs": {}},
        {"id": "e3", "src": "n1", "dst": None, "label": "REL", "attrs": {}},
        {"id": "e4", "src": "n1", "dst": "n2", "label": "REL", "attrs": {}},
    ]
    rows = edge_rows(edges)
    assert [r["edge_key"] for r in rows] == ["e4"]

def test_node_rows_empty_input():
    assert node_rows([]) == []

def test_edge_rows_empty_input():
    assert edge_rows([]) == []

def test_builders_never_interpolate_entity_data_into_sql():
    # DDL/name builders only ever accept identifiers (graph/table names), never
    # entity rows -- so data (names, attrs) can only ever travel through
    # node_rows/edge_rows as a JSON payload, never as part of a SQL string.
    node_table = node_table_name("extracted_graph")
    edge_table = edge_table_name("extracted_graph")
    ddl_strings = [
        create_table_sql(node_table, "node"),
        create_table_sql(edge_table, "edge"),
        create_graph_sql("extracted_graph", node_table, edge_table),
    ]
    for sql in ddl_strings:
        assert "Jerome Powell" not in sql
        assert "Federal Reserve" not in sql
        assert "2018" not in sql
    # json.dumps of the payload is the only place entity data appears.
    payload = json.dumps(node_rows(_nodes()) + edge_rows(_edges()))
    assert "Jerome Powell" in payload


# ---------------------------------------------------------------------------
# KineticaAdapter.ingest_elements -- live (SKIP if Kinetica unreachable).
# ---------------------------------------------------------------------------

from xgraph_gateway import config
from xgraph_gateway.adapters.kinetica_adapter import KineticaAdapter

_TEST_GRAPH = "xgraph_extract_test"

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

def _drop_test_graph(adapter):
    try:
        adapter._db.delete_graph(graph_name=_TEST_GRAPH)
    except Exception:
        pass
    for table in (node_table_name(_TEST_GRAPH), edge_table_name(_TEST_GRAPH)):
        try:
            adapter._db.execute_sql(f"DROP TABLE IF EXISTS {table}")
        except Exception:
            pass

@pytest.fixture
def live_adapter():
    a = _adapter_or_skip()
    _drop_test_graph(a)
    yield a
    _drop_test_graph(a)

def test_ingest_elements_creates_graph_with_nodes_and_edges(live_adapter):
    nodes = [
        {"id": "ing-n1", "label": "Person", "name": "Ada Lovelace", "attrs": {}},
        {"id": "ing-n2", "label": "Organization", "name": "Analytical Engine Co", "attrs": {}},
    ]
    edges = [
        {"id": "ing-e1", "src": "ing-n1", "dst": "ing-n2", "label": "WORKS_AT", "attrs": {}},
    ]
    out = live_adapter.ingest_elements(_TEST_GRAPH, nodes, edges)
    assert out["nodes"] == 2
    assert out["edges"] == 1
    assert out["nodes_created"] == 2
    assert out["edges_created"] == 1
    assert out["labels"]["node_labels"] == ["Organization", "Person"]
    assert out["labels"]["edge_labels"] == ["WORKS_AT"]

    graphs = live_adapter.list_graphs()
    assert any(_TEST_GRAPH in g for g in graphs)

    sizes = live_adapter.graph_sizes()
    matching = [v for k, v in sizes.items() if _TEST_GRAPH in k]
    assert matching
    assert matching[0]["nodes"] >= 2
    assert matching[0]["edges"] >= 1

def test_ingest_elements_accumulates_and_upserts_on_rerun(live_adapter):
    nodes = [{"id": "ing-n1", "label": "Person", "name": "Ada Lovelace", "attrs": {}}]
    live_adapter.ingest_elements(_TEST_GRAPH, nodes, [])
    # Re-running with the same id upserts (update_on_existing_pk) rather than
    # duplicating the row -- still counted in "nodes" (total present), but
    # "nodes_created" is 0 since insert_records_json only updated it.
    out = live_adapter.ingest_elements(_TEST_GRAPH, nodes, [])
    assert out["nodes"] == 1
    assert out["nodes_created"] == 0
    sizes = live_adapter.graph_sizes()
    matching = [v for k, v in sizes.items() if _TEST_GRAPH in k]
    assert matching[0]["nodes"] == 1

def test_ingest_elements_empty_inputs_returns_zeros_without_error(live_adapter):
    out = live_adapter.ingest_elements(_TEST_GRAPH, [], [])
    assert out == {"nodes": 0, "edges": 0, "nodes_created": 0, "edges_created": 0,
                    "labels": {"node_labels": [], "edge_labels": []}}
