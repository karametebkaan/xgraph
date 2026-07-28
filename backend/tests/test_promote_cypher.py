from xgraph_gateway.adapters.falkordb_adapter import build_promote_cypher


def test_builder_is_match_only_never_merge():
    stmts = build_promote_cypher(
        [{"NODE": "b1", "party:party_name": "Acme"}],
        key="NODE", columns=["party:party_name"])
    assert len(stmts) == 1
    cypher, params = stmts[0]
    assert "MATCH (n {NODE: r.id})" in cypher
    assert "SET n += r.attrs" in cypher
    assert "MERGE" not in cypher            # never creates nodes
    assert "RETURN count(n)" in cypher      # for nodes_matched accounting


def test_verbatim_key_travels_in_params_not_query_text():
    stmts = build_promote_cypher(
        [{"NODE": "b1", "party:party_name": "Acme"}],
        key="NODE", columns=["party:party_name"])
    cypher, params = stmts[0]
    # colon-bearing column name is a MAP KEY in params, never in the query text
    assert "party:party_name" not in cypher
    assert params["rows"] == [{"id": "b1", "attrs": {"party:party_name": "Acme"}}]


def test_null_cells_are_stripped_and_allnull_rows_dropped():
    stmts = build_promote_cypher(
        [
            {"NODE": "b1", "a": 1, "b": None},   # b dropped
            {"NODE": "b2", "a": None, "b": None},# all-null -> row dropped
            {"NODE": None, "a": 5, "b": 6},      # null key -> row dropped
        ],
        key="NODE", columns=["a", "b"])
    rows = stmts[0][1]["rows"]
    assert rows == [{"id": "b1", "attrs": {"a": 1}}]


def test_batches_respect_batch_size():
    rows = [{"NODE": f"b{i}", "a": i} for i in range(12)]
    stmts = build_promote_cypher(rows, key="NODE", columns=["a"], batch_size=5)
    assert [len(s[1]["rows"]) for s in stmts] == [5, 5, 2]


def test_empty_payload_yields_no_statements():
    stmts = build_promote_cypher(
        [{"NODE": None, "a": None}], key="NODE", columns=["a"])
    assert stmts == []


def test_promote_columns_unsupported_raises_valueerror_kinetica():
    # Kinetica (and the base default) must reject promotion with a ValueError
    # whose message avoids "timeout"/"unreachable"/"connection" so the gateway
    # maps it to 400, not 502/504. Instantiated via __new__ -- no live service.
    import pytest
    from xgraph_gateway.adapters.kinetica_adapter import KineticaAdapter
    from xgraph_gateway.adapters.fake import FakeAdapter

    kin = KineticaAdapter.__new__(KineticaAdapter)
    with pytest.raises(ValueError) as ei:
        kin.promote_columns("g", "src.parquet", columns=["a"])
    msg = str(ei.value).lower()
    assert "not supported" in msg
    assert not any(w in msg for w in ("timeout", "unreachable", "connection"))

    # Base default (via FakeAdapter, which inherits it) also raises ValueError.
    with pytest.raises(ValueError):
        FakeAdapter().promote_columns("g", "src.parquet", columns=["a"])
