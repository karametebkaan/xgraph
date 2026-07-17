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
