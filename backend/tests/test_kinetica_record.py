import pytest

from xgraph_gateway import config
from xgraph_gateway.adapters.kinetica_adapter import (
    KineticaAdapter,
    _escape_sql_literal,
    _row_to_record,
    _node_id_column_from_ddl,
)

# ---------------------------------------------------------------------------
# Canned show_graph response reused from test_kinetica_load's DDL fixture shape.
# ---------------------------------------------------------------------------

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

import json
_DDL_RESP = {
    "original_request": [json.dumps({"statement": _ORIGINAL_REQUEST_STATEMENT})],
}

# A graph built with `SELECT * FROM <table>` (no `AS NODE` alias) -- xgraph's own
# extract graphs and the user's `rvv_new` are shaped like this. The node id column
# is the literal `NODE` column, discoverable only by probing the backing table.
_SELECT_STAR_STATEMENT = (
    "create or replace directed graph ki_home.rvv_new (\n"
    "    nodes => INPUT_TABLES((SELECT * FROM rvv_new_nodes)),\n"
    "    EDGES => INPUT_TABLES((\n"
    "        SELECT entity_a_uuid as NODE1, entity_b_uuid as NODE2, relationship_a_b as LABEL\n"
    "        FROM rvv_new\n"
    "    )),\n"
    "    OPTIONS => KV_PAIRS(save_persist = 'true')\n"
    ");"
)
_SELECT_STAR_RESP = {
    "original_request": [json.dumps({"statement": _SELECT_STAR_STATEMENT})],
}


# ---------------------------------------------------------------------------
# Unit tests -- pure helpers.
# ---------------------------------------------------------------------------

def test_escape_sql_literal_doubles_single_quotes():
    assert _escape_sql_literal("O'Brien") == "O''Brien"

def test_escape_sql_literal_no_quotes_unchanged():
    assert _escape_sql_literal("b1") == "b1"

def test_escape_sql_literal_stringifies_non_str():
    assert _escape_sql_literal(123) == "123"

def test_row_to_record_shapes_full_row_as_props():
    row = {"id": "b1", "label": "bank", "bank:name": "Acme", "bank:risk": 42}
    rec = _row_to_record(row, "b1")
    assert rec == {"id": "b1", "label": "bank", "props": row}
    # props must be the full record, not a subset
    assert "bank:name" in rec["props"] and "bank:risk" in rec["props"]

def test_row_to_record_resolves_uppercase_node_and_label_columns():
    # Extract/rvv graphs store the identity in NODE and the type in LABEL (uppercase).
    row = {"NODE": "u1", "LABEL": "organization", "entity_a_name": "BARLOWS"}
    rec = _row_to_record(row, "u1")
    assert rec["id"] == "u1"
    assert rec["label"] == "organization"
    assert rec["props"] == row


def test_node_id_column_from_ddl_finds_explicit_alias():
    # banking: `id as NODE` in the NODES sub-select -> the source id column is `id`.
    assert _node_id_column_from_ddl(_ORIGINAL_REQUEST_STATEMENT) == "id"

def test_node_id_column_from_ddl_none_for_select_star():
    # `SELECT * FROM rvv_new_nodes` has no `AS NODE` alias -> None (caller probes).
    assert _node_id_column_from_ddl(_SELECT_STAR_STATEMENT) is None

def test_node_id_column_from_ddl_ignores_edge_node1_node2_aliases():
    # `as NODE1`/`as NODE2` in the EDGES section must not be mistaken for the id alias.
    assert _node_id_column_from_ddl(_SELECT_STAR_STATEMENT) is None

def test_node_id_column_from_ddl_empty_statement():
    assert _node_id_column_from_ddl("") is None


# ---------------------------------------------------------------------------
# get_record shaping / never-raises behavior via a fake KineticaAdapter.
# ---------------------------------------------------------------------------

class _FakeSrc:
    def __init__(self, rows=None, raise_on_query=False):
        self._rows = rows or []
        self._raise = raise_on_query
        self.last_sql = None

    def rows(self, sql):
        self.last_sql = sql
        if self._raise:
            raise RuntimeError("boom")
        yield from self._rows

class _FakeDb:
    def __init__(self, show_graph_resp=None, raise_on_show_graph=False):
        self._resp = show_graph_resp or {}
        self._raise = raise_on_show_graph

    def show_graph(self, graph_name="", options=None):
        if self._raise:
            raise ConnectionError("network error")
        return self._resp

def _bare_adapter(show_graph_resp=None, rows=None, raise_on_show_graph=False, raise_on_query=False):
    adapter = KineticaAdapter.__new__(KineticaAdapter)
    adapter._db = _FakeDb(show_graph_resp, raise_on_show_graph=raise_on_show_graph)
    adapter._src = _FakeSrc(rows, raise_on_query=raise_on_query)
    return adapter

def test_get_record_returns_full_row_as_props():
    row = {"id": "b1", "label": "bank", "bank:name": "Acme Bank", "bank:risk_score": 42}
    adapter = _bare_adapter(_DDL_RESP, rows=[row])
    rec = adapter.get_record("expero.banking_graph", "b1")
    assert rec == {"id": "b1", "label": "bank", "props": row}

def test_get_record_escapes_quote_in_node_id():
    adapter = _bare_adapter(_DDL_RESP, rows=[])
    adapter.get_record("expero.banking_graph", "b1' OR '1'='1")
    sql = adapter._src.last_sql
    assert "b1'' OR ''1''=''1" in sql
    # the raw unescaped id must not appear verbatim (would break out of the literal)
    assert "id = 'b1' OR '1'='1'" not in sql

def test_get_record_no_row_returns_empty_dict():
    adapter = _bare_adapter(_DDL_RESP, rows=[])
    assert adapter.get_record("expero.banking_graph", "missing") == {}

def test_get_record_backing_table_not_discoverable_returns_empty_dict():
    adapter = _bare_adapter({"original_request": []})
    assert adapter.get_record("expero.banking_graph", "b1") == {}

def test_get_record_never_raises_when_query_blows_up():
    adapter = _bare_adapter(_DDL_RESP, raise_on_query=True)
    assert adapter.get_record("expero.banking_graph", "b1") == {}

def test_get_record_never_raises_when_show_graph_blows_up():
    adapter = _bare_adapter(raise_on_show_graph=True)
    assert adapter.get_record("expero.banking_graph", "b1") == {}

def test_get_record_resolves_NODE_column_via_probe_when_no_alias():
    # rvv_new: NODES is `SELECT *` (no alias) and the id column is `NODE`. The
    # adapter must probe the backing table's columns and query `WHERE NODE = ...`,
    # NOT the hardcoded `id` (which doesn't exist on rvv_new_nodes).
    row = {"NODE": "c2a9d904-a1d8-488f-a71d-8746841ab901", "LABEL": "organization",
           "entity_a_name": "BARLOWS", "source_table_id": 7}
    adapter = _bare_adapter(_SELECT_STAR_RESP, rows=[row])
    rec = adapter.get_record("ki_home.rvv_new", "c2a9d904-a1d8-488f-a71d-8746841ab901")
    sql = adapter._src.last_sql
    assert "WHERE NODE = 'c2a9d904-a1d8-488f-a71d-8746841ab901'" in sql
    assert "FROM rvv_new_nodes" in sql
    assert rec["id"] == "c2a9d904-a1d8-488f-a71d-8746841ab901"
    assert rec["label"] == "organization"
    assert rec["props"] == row

def test_get_record_escapes_quote_in_probed_node_column():
    # Escaping must still apply when the id column comes from a probe.
    row = {"NODE": "x", "LABEL": "t"}
    adapter = _bare_adapter(_SELECT_STAR_RESP, rows=[row])
    adapter.get_record("ki_home.rvv_new", "a' OR '1'='1")
    sql = adapter._src.last_sql
    assert "NODE = 'a'' OR ''1''=''1'" in sql


# ---------------------------------------------------------------------------
# fetch_node_attrs -- Explain's "hydrate from the graph's own nodes" path.
# Kinetica extract graphs store attributes ON the backing node table, so this
# must return them (keyed by NODE) instead of Explain falling through to an
# unrelated external Parquet.
# ---------------------------------------------------------------------------

def test_fetch_node_attrs_keys_by_node_and_drops_label():
    row = {"NODE": "u1", "LABEL": "organization",
           "entity_a_name": "BARLOWS", "source_field_a": "supplier_name"}
    adapter = _bare_adapter(_SELECT_STAR_RESP, rows=[row])
    out = adapter.fetch_node_attrs("ki_home.rvv_new", ["u1"])
    assert len(out) == 1
    assert out[0]["NODE"] == "u1"
    assert out[0]["entity_a_name"] == "BARLOWS"
    assert out[0]["source_field_a"] == "supplier_name"
    assert "LABEL" not in out[0]           # metadata dropped
    sql = adapter._src.last_sql
    assert "FROM rvv_new_nodes" in sql
    assert "WHERE NODE IN (" in sql        # resolved id column, not hardcoded id

def test_fetch_node_attrs_empty_ids_returns_empty():
    adapter = _bare_adapter(_SELECT_STAR_RESP, rows=[{"NODE": "u1", "LABEL": "t"}])
    assert adapter.fetch_node_attrs("ki_home.rvv_new", []) == []

def test_fetch_node_attrs_filters_non_string_ids():
    adapter = _bare_adapter(_SELECT_STAR_RESP, rows=[{"NODE": "u1", "LABEL": "t"}])
    # None/ints (e.g. an aggregate/count value in a result row) are ignored.
    out = adapter.fetch_node_attrs("ki_home.rvv_new", [None, 7, "u1"])
    assert out and out[0]["NODE"] == "u1"
    assert "'u1'" in adapter._src.last_sql
    assert "7" not in adapter._src.last_sql.split("IN (")[1]

def test_fetch_node_attrs_escapes_quotes_in_ids():
    adapter = _bare_adapter(_SELECT_STAR_RESP, rows=[{"NODE": "x", "LABEL": "t"}])
    adapter.fetch_node_attrs("ki_home.rvv_new", ["a'b"])
    assert "'a''b'" in adapter._src.last_sql

def test_fetch_node_attrs_never_raises_returns_empty():
    adapter = _bare_adapter(_SELECT_STAR_RESP, rows=[{"NODE": "x"}], raise_on_query=True)
    assert adapter.fetch_node_attrs("ki_home.rvv_new", ["x"]) == []

def test_fetch_node_attrs_no_backing_table_returns_empty():
    adapter = _bare_adapter({"original_request": []}, rows=[{"NODE": "x"}])
    assert adapter.fetch_node_attrs("ki_home.rvv_new", ["x"]) == []


# ---------------------------------------------------------------------------
# Live integration test -- skip if Kinetica is unreachable.
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

def test_live_get_record_returns_full_bank_record():
    a = _adapter_or_skip()
    out = a.run_query("", "SELECT id FROM expero.vertexes WHERE label = 'bank' LIMIT 1")
    if not out["rows"]:
        pytest.skip("no bank rows in expero.vertexes")
    bank_id = out["rows"][0][0]

    rec = a.get_record("expero.banking_graph", bank_id)
    assert rec != {}
    assert rec["id"] == bank_id
    assert isinstance(rec["props"], dict)
    assert rec["props"].get("id") == bank_id
    # multiple attribute columns present -- the "post-join" pulled the full record
    assert len(rec["props"]) > 2
    assert any(k.startswith("bank:") for k in rec["props"].keys())
