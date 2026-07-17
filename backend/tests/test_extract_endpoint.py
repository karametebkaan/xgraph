from __future__ import annotations

import os
import pytest
from fastapi.testclient import TestClient

from xgraph_gateway import extract
from xgraph_gateway.adapters.fake import FakeAdapter
from xgraph_gateway.app import create_app
from xgraph_gateway.compute.duckdb_engine import DuckDBComputeEngine

_ENTITIES = [
    {"id": "e1", "label": "Person", "name": "Jerome Powell", "attrs": {}},
    {"id": "e2", "label": "Organization", "name": "Fed", "attrs": {}},
]
_RELATIONS = [
    {"id": "r1", "src": "e1", "dst": "e2", "label": "WORKS_AT", "attrs": {}},
]


def _client(tmp_path):
    # Isolated meta DB: extract_endpoint now records document provenance via
    # DuckDBComputeEngine.record_document, which defaults to the shared
    # on-disk data/xgraph_meta.duckdb. Without isolation, re-running the
    # suite would see the same doc_uri/sha256 as already-ingested and
    # short-circuit to "unchanged" (entities == 0), polluting a real repo
    # file across test runs.
    compute = DuckDBComputeEngine(meta_path=str(tmp_path / "meta.duckdb"))
    return TestClient(create_app(adapter_factory=lambda e: FakeAdapter(), compute=compute))


def _patch_extract_document(monkeypatch, truncated=False):
    def fake_extract_document(text, hint=None, llm=None, max_chunks=40):
        assert text  # non-empty doc reached extract_document
        return {"entities": list(_ENTITIES), "relations": list(_RELATIONS), "truncated": truncated}
    monkeypatch.setattr(extract, "extract_document", fake_extract_document)


def test_extract_with_text_field(monkeypatch, tmp_path):
    _patch_extract_document(monkeypatch)
    r = _client(tmp_path).post("/extract", data={"graph": "g1", "text": "hi there", "engine": "fake"})
    assert r.status_code == 200
    body = r.json()
    assert body["graph"] == "g1"
    assert body["entities"] == 2
    assert body["relations"] == 1
    assert body["entities_new"] == 2
    assert body["relations_new"] == 1
    assert body["labels"] == {"node_labels": ["Organization", "Person"],
                               "edge_labels": ["WORKS_AT"]}
    assert body["truncated"] is False


def test_extract_with_file_upload(monkeypatch, tmp_path):
    _patch_extract_document(monkeypatch)
    r = _client(tmp_path).post(
        "/extract",
        data={"graph": "g1", "engine": "fake"},
        files={"file": ("d.txt", b"hello world", "text/plain")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["entities"] == 2
    assert body["relations"] == 1
    assert body["entities_new"] == 2
    assert body["relations_new"] == 1


def test_extract_truncated_flag_passthrough(monkeypatch, tmp_path):
    _patch_extract_document(monkeypatch, truncated=True)
    r = _client(tmp_path).post("/extract", data={"graph": "g1", "text": "hi", "engine": "fake"})
    assert r.status_code == 200
    assert r.json()["truncated"] is True


def test_extract_unsupported_file_extension_returns_error_envelope(monkeypatch, tmp_path):
    _patch_extract_document(monkeypatch)
    r = _client(tmp_path).post(
        "/extract",
        data={"graph": "g1", "engine": "fake"},
        files={"file": ("d.docx", b"x", "application/octet-stream")},
    )
    assert r.status_code == 400
    body = r.json()
    assert "error" in body
    assert "unsupported file type" in body["error"]["message"]


def test_extract_requires_nonempty_text_or_file(tmp_path):
    r = _client(tmp_path).post("/extract", data={"graph": "g1", "engine": "fake"})
    assert r.status_code == 400
    assert "error" in r.json()


def test_extract_blank_text_is_rejected(tmp_path):
    r = _client(tmp_path).post("/extract", data={"graph": "g1", "text": "   ", "engine": "fake"})
    assert r.status_code == 400
    assert "error" in r.json()


@pytest.fixture
def client_with_store(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from xgraph_gateway.adapters.fake import FakeAdapter
    from xgraph_gateway.app import create_app
    compute = DuckDBComputeEngine(meta_path=str(tmp_path / "meta.duckdb"))
    app = create_app(adapter_factory=lambda e: FakeAdapter(), compute=compute)
    return TestClient(app)


def _patch_fold_identity(monkeypatch):
    """fold_labels that folds 'Firm'->'Company' deterministically, no LLM."""
    from xgraph_gateway import extract_fold

    def fake_fold(store, graph, entities, relations, source_uri, llm=None):
        report = []
        for e in entities:
            if e.get("label") == "Firm":
                e["label"] = "Company"
                report.append({"kind": "entity", "from": "Firm",
                               "to": "Company", "axis": "EntityType"})
        return report
    monkeypatch.setattr(extract_fold, "fold_labels", fake_fold)


def test_extract_returns_document_record_and_folded(client_with_store, monkeypatch):
    from xgraph_gateway import extract

    def fake_extract_document(text, hint=None, **kw):
        return {"entities": [{"id": "acme", "name": "Acme", "label": "Firm", "attrs": {}}],
                "relations": [], "truncated": False}
    monkeypatch.setattr(extract, "extract_document", fake_extract_document)
    _patch_fold_identity(monkeypatch)

    resp = client_with_store.post("/extract", data={"text": "Acme is a firm.",
                                                    "graph": "g1", "engine": "fake"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["document"]["status"] == "new"
    assert body["document"]["reused"] is False
    assert {"kind": "entity", "from": "Firm", "to": "Company",
            "axis": "EntityType"} in body["folded"]


def test_extract_same_text_is_reused(client_with_store, monkeypatch):
    from xgraph_gateway import extract

    def fake_extract_document(text, hint=None, **kw):
        return {"entities": [{"id": "acme", "name": "Acme", "label": "Firm", "attrs": {}}],
                "relations": [], "truncated": False}
    monkeypatch.setattr(extract, "extract_document", fake_extract_document)
    _patch_fold_identity(monkeypatch)

    payload = {"text": "Acme is a firm.", "graph": "g1", "engine": "fake"}
    first = client_with_store.post("/extract", data=payload).json()
    assert first["document"]["reused"] is False
    second = client_with_store.post("/extract", data=payload).json()
    assert second["document"]["reused"] is True
    assert second["document"]["status"] == "unchanged"
    assert second["entities"] == 0 and second["relations"] == 0
