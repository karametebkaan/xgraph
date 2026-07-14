import pytest

from xgraph_gateway import config
from xgraph_gateway.adapters.kinetica_adapter import (
    KineticaAdapter,
    _escape_sql_literal,
    _row_to_record,
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
