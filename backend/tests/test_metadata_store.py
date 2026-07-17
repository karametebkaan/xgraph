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
