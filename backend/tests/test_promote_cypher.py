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
