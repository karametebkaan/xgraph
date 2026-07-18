import os
from xgraph_gateway.compute.duckdb_engine import DuckDBComputeEngine


def _engine(tmp_path):
    return DuckDBComputeEngine(meta_path=str(tmp_path / "meta.duckdb"))


def test_record_document_new_then_unchanged_then_updated(tmp_path):
    eng = _engine(tmp_path)

    first = eng.record_document("g1", "doc:a", "sha-1", "text")
    assert first["status"] == "new"
    assert first["first_ingested_ts"] == first["last_ingested_ts"]

    # Same uri + same sha256 => unchanged, first_ingested_ts preserved.
    again = eng.record_document("g1", "doc:a", "sha-1", "text")
    assert again["status"] == "unchanged"
    assert again["first_ingested_ts"] == first["first_ingested_ts"]
    assert again["last_ingested_ts"] >= first["last_ingested_ts"]

    # Same uri + different sha256 => updated.
    changed = eng.record_document("g1", "doc:a", "sha-2", "text")
    assert changed["status"] == "updated"
    assert changed["first_ingested_ts"] == first["first_ingested_ts"]


def test_record_document_is_per_graph(tmp_path):
    eng = _engine(tmp_path)
    eng.record_document("g1", "doc:a", "sha-1", "text")
    other = eng.record_document("g2", "doc:a", "sha-1", "text")
    assert other["status"] == "new"  # different graph => distinct ledger row


def test_list_documents(tmp_path):
    eng = _engine(tmp_path)
    eng.record_document("g1", "doc:a", "sha-1", "file")
    eng.record_document("g1", "doc:b", "sha-2", "text")
    docs = eng.list_documents("g1")
    assert {d["doc_uri"] for d in docs} == {"doc:a", "doc:b"}
    assert all(d["graph"] == "g1" for d in docs)


def test_ontology_record_and_resolve(tmp_path):
    eng = _engine(tmp_path)
    # Canonical: type_name == canonical_name.
    eng.record_type("g1", "entity", "Company", "Company", "EntityType", "doc:a")
    # Alias: Firm folds to Company.
    eng.record_type("g1", "entity", "Firm", "Company", "EntityType", "doc:a")

    assert eng.resolve_canonical("g1", "entity", "Company") == "Company"
    assert eng.resolve_canonical("g1", "entity", "Firm") == "Company"
    # Case-insensitive normalized hit.
    assert eng.resolve_canonical("g1", "entity", "company") == "Company"
    # Unknown => None.
    assert eng.resolve_canonical("g1", "entity", "Planet") is None
    # Kind- and graph-scoped.
    assert eng.resolve_canonical("g1", "relation", "Company") is None
    assert eng.resolve_canonical("g2", "entity", "Company") is None


def test_get_canonicals_and_axis_map(tmp_path):
    eng = _engine(tmp_path)
    eng.record_type("g1", "entity", "Company", "Company", "EntityType", "doc:a")
    eng.record_type("g1", "entity", "AI", "AI", "Industry", "doc:a")
    eng.record_type("g1", "entity", "Firm", "Company", "EntityType", "doc:a")

    assert set(eng.get_canonicals("g1", "entity")) == {"Company", "AI"}
    amap = eng.axis_map("g1", "entity")
    assert amap["Company"] == "EntityType"
    assert amap["AI"] == "Industry"
    assert amap["Firm"] == "EntityType"  # aliases resolve to their axis too


def test_record_type_first_seen_wins(tmp_path):
    eng = _engine(tmp_path)
    eng.record_type("g1", "entity", "Company", "Company", "EntityType", "doc:a")
    # A second record for the same (graph,kind,name) must not overwrite.
    eng.record_type("g1", "entity", "Company", "SOMETHING_ELSE", "OtherAxis", "doc:b")
    assert eng.resolve_canonical("g1", "entity", "Company") == "Company"


def test_resolve_canonical_prefers_exact_match(tmp_path):
    eng = _engine(tmp_path)
    # Two distinct type_name rows differing only by case, each with its own
    # canonical_name. An exact-casing lookup must resolve to its own row,
    # not whichever row happens to be returned first without ordering.
    eng.record_type("g1", "entity", "Company", "Company", "EntityType", "d")
    eng.record_type("g1", "entity", "COMPANY", "ShoutCo", "EntityType", "d")

    assert eng.resolve_canonical("g1", "entity", "Company") == "Company"
    assert eng.resolve_canonical("g1", "entity", "COMPANY") == "ShoutCo"


def test_clear_graph_metadata_removes_only_that_graph(tmp_path):
    eng = _engine(tmp_path)
    eng.record_document("g1", "doc:a", "sha-1", "text")
    eng.record_document("g2", "doc:a", "sha-1", "text")
    eng.record_type("g1", "entity", "Company", "Company", "EntityType", "doc:a")
    eng.record_type("g2", "entity", "Company", "Company", "EntityType", "doc:a")

    eng.clear_graph_metadata("g1")

    assert eng.list_documents("g1") == []
    assert eng.get_canonicals("g1", "entity") == []
    # g2's rows are untouched.
    assert len(eng.list_documents("g2")) == 1
    assert eng.get_canonicals("g2", "entity") == ["Company"]


def test_clear_graph_metadata_is_idempotent(tmp_path):
    eng = _engine(tmp_path)
    eng.clear_graph_metadata("nonexistent")  # must not raise, even with no tables/rows


def test_get_document_returns_row_or_none(tmp_path):
    eng = _engine(tmp_path)
    assert eng.get_document("g1", "doc:a") is None
    eng.record_document("g1", "doc:a", "sha-1", "text")
    doc = eng.get_document("g1", "doc:a")
    assert doc is not None
    assert doc["sha256"] == "sha-1"
    assert doc["graph"] == "g1"
    assert doc["doc_uri"] == "doc:a"
    assert eng.get_document("g1", "doc:other") is None


def test_record_type_first_seen_ts_is_naive_utc(tmp_path):
    eng = _engine(tmp_path)
    eng.record_type("g1", "entity", "Company", "Company", "EntityType", "doc:a")

    # Read the raw column back via a fresh connection on the same meta_path
    # to confirm no tz-aware value ever slipped into the tz-naive TIMESTAMP
    # column (which would silently shift on readback).
    con = DuckDBComputeEngine(meta_path=eng._meta_path)._meta_con()
    try:
        row = con.execute(
            "SELECT first_seen_ts FROM xgraph_ontology"
            " WHERE graph = ? AND type_kind = ? AND type_name = ?",
            ["g1", "entity", "Company"]).fetchone()
    finally:
        con.close()
    assert row is not None
    assert row[0].tzinfo is None


# ---------------------------------------------------------------------------
# KineticaComputeEngine metadata-store mirror -- live (SKIP if Kinetica
# unreachable). Same contract as DuckDBComputeEngine above, backed by
# xgraph_meta.documents/xgraph_meta.ontology Kinetica tables (see
# compute/kinetica_engine.py). Uses a throwaway graph name so it never
# touches a real graph's rows; cleanup deletes only rows WHERE
# graph = the throwaway name (never drops the xgraph_meta tables themselves,
# in case a real deployment already has rows in them).
# ---------------------------------------------------------------------------

import pytest
from xgraph_gateway import config
from xgraph_gateway.compute.kinetica_engine import (
    KineticaComputeEngine, _DOCUMENTS_TABLE, _ONTOLOGY_TABLE, _lit,
)

_KINETICA_TEST_GRAPH = "xgraph_meta_test"
_KINETICA_NULL_TEST_GRAPH = "xgraph_meta_null_test"


def test_lit_renders_none_as_unquoted_null():
    # None must become the bare SQL token NULL -- never the quoted string
    # literal 'None' (that's the bug: str(None) == "None" would otherwise
    # silently persist as data instead of a real SQL NULL).
    assert _lit(None) == "NULL"


def test_lit_renders_string_as_escaped_quoted_literal():
    # Normal values still go through _escape_sql_literal's quoting/escaping.
    assert _lit("O'Brien") == "'O''Brien'"
    assert _lit("Company") == "'Company'"


def _kinetica_engine_or_skip():
    s = config.load_settings()
    if not s.kinetica_url:
        pytest.skip("KINETICA_URL not set")
    conn = {"url": s.kinetica_url, "user": s.kinetica_user, "password": s.kinetica_pass}
    eng = KineticaComputeEngine(conn=conn)
    try:
        eng._ensure_meta_schema()
    except Exception as e:
        pytest.skip(f"Kinetica unreachable: {e}")
    return eng


def _cleanup_kinetica_meta_rows(eng):
    g = f"'{_KINETICA_TEST_GRAPH}'"
    for table in (_DOCUMENTS_TABLE, _ONTOLOGY_TABLE):
        try:
            list(eng._src.rows(f"DELETE FROM {table} WHERE graph = {g}"))
        except Exception:
            pass


@pytest.fixture
def kinetica_engine():
    eng = _kinetica_engine_or_skip()
    _cleanup_kinetica_meta_rows(eng)
    yield eng
    _cleanup_kinetica_meta_rows(eng)


def test_kinetica_metadata_roundtrip(kinetica_engine):
    eng = kinetica_engine

    first = eng.record_document(_KINETICA_TEST_GRAPH, "doc:a", "sha-1", "text")
    assert first["status"] in ("new", "unchanged", "updated")
    assert "first_ingested_ts" in first and "last_ingested_ts" in first

    again = eng.record_document(_KINETICA_TEST_GRAPH, "doc:a", "sha-1", "text")
    assert again["status"] == "unchanged"
    assert again["first_ingested_ts"] == first["first_ingested_ts"]

    changed = eng.record_document(_KINETICA_TEST_GRAPH, "doc:a", "sha-2", "text")
    assert changed["status"] == "updated"

    docs = eng.list_documents(_KINETICA_TEST_GRAPH)
    assert any(d["doc_uri"] == "doc:a" for d in docs)

    eng.record_type(_KINETICA_TEST_GRAPH, "entity", "Company", "Company", "EntityType", "doc:a")
    eng.record_type(_KINETICA_TEST_GRAPH, "entity", "Firm", "Company", "EntityType", "doc:a")

    assert eng.resolve_canonical(_KINETICA_TEST_GRAPH, "entity", "Company") == "Company"
    assert eng.resolve_canonical(_KINETICA_TEST_GRAPH, "entity", "Firm") == "Company"
    assert eng.resolve_canonical(_KINETICA_TEST_GRAPH, "entity", "company") == "Company"
    assert eng.resolve_canonical(_KINETICA_TEST_GRAPH, "entity", "Planet") is None

    assert "Company" in eng.get_canonicals(_KINETICA_TEST_GRAPH, "entity")
    amap = eng.axis_map(_KINETICA_TEST_GRAPH, "entity")
    assert amap["Company"] == "EntityType"


def test_kinetica_record_type_first_seen_wins(kinetica_engine):
    eng = kinetica_engine
    eng.record_type(_KINETICA_TEST_GRAPH, "entity", "Company", "Company", "EntityType", "doc:a")
    eng.record_type(_KINETICA_TEST_GRAPH, "entity", "Company", "SOMETHING_ELSE", "OtherAxis", "doc:b")
    assert eng.resolve_canonical(_KINETICA_TEST_GRAPH, "entity", "Company") == "Company"


def test_kinetica_clear_graph_metadata(kinetica_engine):
    eng = kinetica_engine
    eng.record_document(_KINETICA_TEST_GRAPH, "doc:a", "sha-1", "text")
    eng.record_type(_KINETICA_TEST_GRAPH, "entity", "Company", "Company", "EntityType", "doc:a")

    eng.clear_graph_metadata(_KINETICA_TEST_GRAPH)

    assert eng.list_documents(_KINETICA_TEST_GRAPH) == []
    assert eng.get_canonicals(_KINETICA_TEST_GRAPH, "entity") == []


def test_kinetica_get_document(kinetica_engine):
    eng = kinetica_engine
    assert eng.get_document(_KINETICA_TEST_GRAPH, "doc:a") is None
    eng.record_document(_KINETICA_TEST_GRAPH, "doc:a", "sha-1", "text")
    doc = eng.get_document(_KINETICA_TEST_GRAPH, "doc:a")
    assert doc is not None
    assert doc["sha256"] == "sha-1"
    assert doc["graph"] == _KINETICA_TEST_GRAPH


def test_kinetica_record_type_null_axis_and_canonical_roundtrip(kinetica_engine):
    # Regression for the _escape_sql_literal(None) -> "'None'" bug: record_type
    # is a legal call with canonical_name/axis/source_uri all None (mirroring
    # DuckDBComputeEngine, whose parameterized binding persists a real NULL for
    # None). Uses its own throwaway graph (never the shared _KINETICA_TEST_GRAPH)
    # so it doesn't interfere with the other ontology rows in that fixture.
    eng = kinetica_engine
    g = _KINETICA_NULL_TEST_GRAPH
    try:
        eng.record_type(g, "entity", "SomeType", canonical_name=None, axis=None, source_uri="d")
        amap = eng.axis_map(g, "entity")
        assert amap["SomeType"] is None  # NULL round-trips as None, not the string "None"
        assert eng.get_canonicals(g, "entity") == [None]
    finally:
        list(eng._src.rows(f"DELETE FROM {_ONTOLOGY_TABLE} WHERE graph = '{g}'"))
