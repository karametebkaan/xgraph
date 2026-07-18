import json

import pytest

from graph_loader.mapper import MappingError
from xgraph_gateway import config
from xgraph_gateway.adapters.kinetica_adapter import (
    KineticaAdapter,
    _NODE_BASE_COLS,
    _EDGE_BASE_COLS,
    _infer_col_type,
    add_column_sql,
    create_graph_sql,
    discover_attr_columns,
    edge_table_name,
    label_keys_table_name,
    node_table_name,
)


# ---------------------------------------------------------------------------
# _infer_col_type -- pure, no DB.
# ---------------------------------------------------------------------------

def test_infer_col_type_bool():
    assert _infer_col_type(True) == "BOOLEAN"
    assert _infer_col_type(False) == "BOOLEAN"

def test_infer_col_type_int():
    assert _infer_col_type(5) == "BIGINT"

def test_infer_col_type_float():
    assert _infer_col_type(3.14) == "DOUBLE"

def test_infer_col_type_str():
    assert _infer_col_type("hello") == "VARCHAR(1024)"

def test_infer_col_type_none_defaults_varchar():
    assert _infer_col_type(None) == "VARCHAR(1024)"

def test_infer_col_type_bool_before_int_subclass():
    # bool is an int subclass in Python -- bool must win.
    assert _infer_col_type(True) != "BIGINT"


# ---------------------------------------------------------------------------
# discover_attr_columns -- unions attrs keys, infers types from first
# non-null value, skips base-column collisions and unsafe identifiers.
# ---------------------------------------------------------------------------

def test_discover_attr_columns_unions_keys_and_infers_types():
    nodes = [
        {"id": "n1", "attrs": {"population": 2100000, "capital": True}},
        {"id": "n2", "attrs": {"area_km2": 105.4}},
    ]
    cols = discover_attr_columns(nodes, _NODE_BASE_COLS)
    assert cols == {"population": "BIGINT", "capital": "BOOLEAN", "area_km2": "DOUBLE"}

def test_discover_attr_columns_skips_base_column_collisions():
    nodes = [{"id": "n1", "attrs": {"NODE": "dup", "LABEL": "dup", "name": "dup",
                                      "entity_name": "dup", "population": 100}}]
    cols = discover_attr_columns(nodes, _NODE_BASE_COLS)
    assert cols == {"population": "BIGINT"}

def test_discover_attr_columns_edge_base_collisions():
    edges = [{"id": "e1", "attrs": {"edge_key": "dup", "NODE1": "dup", "NODE2": "dup",
                                      "LABEL": "dup", "since": 2018}}]
    cols = discover_attr_columns(edges, _EDGE_BASE_COLS)
    assert cols == {"since": "BIGINT"}

def test_discover_attr_columns_skips_unsafe_identifier():
    nodes = [{"id": "n1", "attrs": {"bad key": "x", "good_key": "y"}}]
    cols = discover_attr_columns(nodes, _NODE_BASE_COLS)
    assert cols == {"good_key": "VARCHAR(1024)"}

def test_discover_attr_columns_no_attrs_key():
    assert discover_attr_columns([{"id": "n1"}], _NODE_BASE_COLS) == {}

def test_discover_attr_columns_empty_input():
    assert discover_attr_columns([], _NODE_BASE_COLS) == {}

def test_discover_attr_columns_waits_for_non_null_value_to_infer_type():
    # First element has a null value for "population" -- type inference must
    # not lock in VARCHAR just because the first sighting was null; a later
    # non-null value should still resolve the real type.
    nodes = [
        {"id": "n1", "attrs": {"population": None}},
        {"id": "n2", "attrs": {"population": 5000}},
    ]
    cols = discover_attr_columns(nodes, _NODE_BASE_COLS)
    assert cols == {"population": "BIGINT"}

def test_discover_attr_columns_all_null_defaults_varchar():
    nodes = [{"id": "n1", "attrs": {"mystery": None}}]
    cols = discover_attr_columns(nodes, _NODE_BASE_COLS)
    assert cols == {"mystery": "VARCHAR(1024)"}


# ---------------------------------------------------------------------------
# create_graph_sql -- attr columns appended to the NODES/EDGES select, name
# still aliased to entity_name (the NODE_NAME landmine).
# ---------------------------------------------------------------------------

def test_create_graph_sql_includes_node_attr_columns():
    sql = create_graph_sql("g", "g_nodes", "g_edges", node_attr_cols=["population", "capital"])
    assert "SELECT NODE, LABEL, name AS entity_name, population, capital FROM g_nodes" in sql

def test_create_graph_sql_includes_edge_attr_columns():
    sql = create_graph_sql("g", "g_nodes", "g_edges", edge_attr_cols=["since"])
    assert "SELECT NODE1, NODE2, LABEL, since FROM g_edges" in sql

def test_create_graph_sql_still_aliases_name_to_entity_name_with_attrs():
    sql = create_graph_sql("g", "g_nodes", "g_edges", node_attr_cols=["population"])
    assert "name AS entity_name" in sql
    # The landmine: a bare "name" output column must never appear unaliased.
    assert "SELECT NODE, LABEL, name," not in sql

def test_create_graph_sql_no_attr_cols_keeps_base_shape():
    sql = create_graph_sql("g", "g_nodes", "g_edges")
    assert "SELECT NODE, LABEL, name AS entity_name FROM g_nodes" in sql
    assert "SELECT NODE1, NODE2, LABEL FROM g_edges" in sql

def test_create_graph_sql_rejects_unsafe_attr_column():
    with pytest.raises(MappingError):
        create_graph_sql("g", "g_nodes", "g_edges", node_attr_cols=["bad; DROP TABLE x"])


# ---------------------------------------------------------------------------
# add_column_sql -- well-formed ALTER, identifier-safe.
# ---------------------------------------------------------------------------

def test_add_column_sql_well_formed():
    sql = add_column_sql("g_nodes", "population", "BIGINT")
    assert sql == "ALTER TABLE g_nodes ADD COLUMN population BIGINT"

def test_add_column_sql_rejects_bad_identifier():
    with pytest.raises(MappingError):
        add_column_sql("g_nodes", "bad; DROP TABLE x", "BIGINT")

def test_add_column_sql_rejects_spaced_identifier():
    with pytest.raises(MappingError):
        add_column_sql("g_nodes", "bad key", "VARCHAR(1024)")


# ---------------------------------------------------------------------------
# KineticaAdapter._evolve_columns -- fake db capturing executed DDL, no live
# connection.
# ---------------------------------------------------------------------------

class _FakeShowTableDb:
    """Fakes show_table (for _current_columns) + execute_sql (for
    _execute_ddl's ALTER calls), capturing every DDL statement executed."""
    def __init__(self, existing_cols):
        self._existing_cols = existing_cols
        self.executed = []

    def show_table(self, table_name, options=None):
        return {
            "table_names": [table_name],
            "type_schemas": [json.dumps({"fields": [{"name": c} for c in self._existing_cols]})],
        }

    def execute_sql(self, statement):
        self.executed.append(statement)
        class _Resp(dict):
            def is_ok(self):
                return True
        return _Resp({"status_info": {}})

def _adapter_with_fake_db(existing_cols):
    adapter = KineticaAdapter.__new__(KineticaAdapter)
    db = _FakeShowTableDb(existing_cols)
    adapter._db = db
    return adapter, db

def test_evolve_columns_adds_missing_column():
    adapter, db = _adapter_with_fake_db(["NODE", "LABEL", "name"])
    adapter._evolve_columns("g_nodes", {"population": "BIGINT"})
    assert db.executed == ["ALTER TABLE g_nodes ADD COLUMN population BIGINT"]

def test_evolve_columns_skips_already_present_column():
    adapter, db = _adapter_with_fake_db(["NODE", "LABEL", "name", "population"])
    adapter._evolve_columns("g_nodes", {"population": "BIGINT"})
    assert db.executed == []

def test_evolve_columns_noop_for_empty_attr_cols():
    adapter, db = _adapter_with_fake_db(["NODE", "LABEL", "name"])
    adapter._evolve_columns("g_nodes", {})
    assert db.executed == []

def test_all_attr_columns_excludes_base_cols():
    adapter, _db = _adapter_with_fake_db(["NODE", "LABEL", "name", "population", "country"])
    assert adapter._all_attr_columns("g_nodes", _NODE_BASE_COLS) == ["population", "country"]


# ---------------------------------------------------------------------------
# KineticaAdapter.ingest_elements -- live (SKIP if Kinetica unreachable).
# ---------------------------------------------------------------------------

_TEST_GRAPH = "attr_evo_test"

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
    for table in (node_table_name(_TEST_GRAPH), edge_table_name(_TEST_GRAPH),
                  label_keys_table_name(_TEST_GRAPH)):
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

def test_live_ingest_evolves_population_column_and_is_queryable(live_adapter):
    nodes = [{"id": "paris", "label": "Location", "name": "Paris",
              "attrs": {"population": 2100000}}]
    out = live_adapter.ingest_elements(_TEST_GRAPH, nodes, [])
    assert out["nodes"] == 1
    assert out["nodes_created"] == 1

    cols = live_adapter._current_columns(node_table_name(_TEST_GRAPH))
    assert "population" in cols

    result = live_adapter.run_query(
        _TEST_GRAPH,
        f'GRAPH "{_TEST_GRAPH}" MATCH (o:Location WHERE (o.population > 1000000)) '
        "RETURN o.entity_name AS name")
    names = [r[0] for r in result["rows"]]
    assert "Paris" in names

def test_live_ingest_adds_new_attr_column_on_rerun_without_regressing_base_match(live_adapter):
    nodes = [{"id": "paris", "label": "Location", "name": "Paris",
              "attrs": {"population": 2100000}}]
    live_adapter.ingest_elements(_TEST_GRAPH, nodes, [])

    nodes2 = [{"id": "lyon", "label": "Location", "name": "Lyon",
               "attrs": {"country": "France"}}]
    out = live_adapter.ingest_elements(_TEST_GRAPH, nodes2, [])
    assert out["nodes"] == 1
    assert out["nodes_created"] == 1

    cols = live_adapter._current_columns(node_table_name(_TEST_GRAPH))
    assert "country" in cols
    assert "population" in cols

    # Base label MATCH still works -- no NODE_NAME regression from the `name`
    # column, and no regression in the previously-evolved `population` column
    # from a run that didn't mention it.
    result = live_adapter.run_query(
        _TEST_GRAPH, f'GRAPH "{_TEST_GRAPH}" MATCH (o:Location) RETURN o.entity_name AS name')
    names = {r[0] for r in result["rows"]}
    assert {"Paris", "Lyon"}.issubset(names)
