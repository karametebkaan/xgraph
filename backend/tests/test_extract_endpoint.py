from __future__ import annotations

from fastapi.testclient import TestClient

from xgraph_gateway import extract
from xgraph_gateway.adapters.fake import FakeAdapter
from xgraph_gateway.app import create_app

_ENTITIES = [
    {"id": "e1", "label": "Person", "name": "Jerome Powell", "attrs": {}},
    {"id": "e2", "label": "Organization", "name": "Fed", "attrs": {}},
]
_RELATIONS = [
    {"id": "r1", "src": "e1", "dst": "e2", "label": "WORKS_AT", "attrs": {}},
]


def _client():
    return TestClient(create_app(adapter_factory=lambda e: FakeAdapter()))


def _patch_extract_document(monkeypatch, truncated=False):
    def fake_extract_document(text, hint=None, llm=None, max_chunks=40):
        assert text  # non-empty doc reached extract_document
        return {"entities": list(_ENTITIES), "relations": list(_RELATIONS), "truncated": truncated}
    monkeypatch.setattr(extract, "extract_document", fake_extract_document)


def test_extract_with_text_field(monkeypatch):
    _patch_extract_document(monkeypatch)
    r = _client().post("/extract", data={"graph": "g1", "text": "hi there", "engine": "fake"})
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


def test_extract_with_file_upload(monkeypatch):
    _patch_extract_document(monkeypatch)
    r = _client().post(
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


def test_extract_truncated_flag_passthrough(monkeypatch):
    _patch_extract_document(monkeypatch, truncated=True)
    r = _client().post("/extract", data={"graph": "g1", "text": "hi", "engine": "fake"})
    assert r.status_code == 200
    assert r.json()["truncated"] is True


def test_extract_unsupported_file_extension_returns_error_envelope(monkeypatch):
    _patch_extract_document(monkeypatch)
    r = _client().post(
        "/extract",
        data={"graph": "g1", "engine": "fake"},
        files={"file": ("d.docx", b"x", "application/octet-stream")},
    )
    assert r.status_code == 400
    body = r.json()
    assert "error" in body
    assert "unsupported file type" in body["error"]["message"]


def test_extract_requires_nonempty_text_or_file():
    r = _client().post("/extract", data={"graph": "g1", "engine": "fake"})
    assert r.status_code == 400
    assert "error" in r.json()


def test_extract_blank_text_is_rejected():
    r = _client().post("/extract", data={"graph": "g1", "text": "   ", "engine": "fake"})
    assert r.status_code == 400
    assert "error" in r.json()
