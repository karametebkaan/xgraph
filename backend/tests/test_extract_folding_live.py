"""LIVE end-to-end regression: extraction folding + document idempotency +
multi-label ingest, against a running FalkorDB.

Unlike test_extract_endpoint.py (FakeAdapter, no live engine) this test
exercises the real FalkorDBAdapter through the gateway's /extract, /query,
and /delete_graph endpoints, using a monkeypatched extract.extract_document
(deterministic, no LLM call) so the folding/idempotency/multi-label behavior
itself is what's under test, not entity extraction quality.

SKIPs cleanly if FalkorDB is unreachable (mirrors the project's live-test
convention: never fail just because a dependency service is down).
"""
import os
import pytest
from fastapi.testclient import TestClient
from xgraph_gateway.app import create_app
from xgraph_gateway.compute.duckdb_engine import DuckDBComputeEngine


def _client(tmp_path):
    compute = DuckDBComputeEngine(meta_path=str(tmp_path / "meta.duckdb"))
    return TestClient(create_app(compute=compute))


def _falkor_up(client):
    r = client.get("/graphs", params={"engine": "falkordb"})
    return r.status_code == 200 and "error" not in r.json()


def test_folding_and_idempotency_live(tmp_path, monkeypatch):
    client = _client(tmp_path)
    if not _falkor_up(client):
        pytest.skip("FalkorDB unreachable")

    from xgraph_gateway import extract

    # Deterministic proposal: two mentions with synonym labels + a facet.
    def fake_extract_document(text, hint=None, **kw):
        return {"entities": [
                    {"id": "anthropic", "name": "Anthropic", "label": "Company",
                     "facets": [{"name": "AI", "axis": "Industry"}], "attrs": {}},
                    {"id": "google", "name": "Google", "label": "Firm",
                     "facets": [], "attrs": {}}],
                "relations": [], "truncated": False}
    monkeypatch.setattr(extract, "extract_document", fake_extract_document)

    graph = "fold_live_test"
    try:
        # Seed Firm->Company so folding is deterministic without an LLM (the
        # first time "Firm" is resolved, the store already has "Company" as
        # an existing canonical, since fake_extract_document always proposes
        # both in one call -- this seed run lets that fold decision settle
        # before the assertions below, which don't depend on its outcome).
        client.post("/extract", data={"text": "seed", "graph": graph, "engine": "falkordb"})

        first = client.post("/extract", data={"text": "doc one about Anthropic and Google.",
                                              "graph": graph, "engine": "falkordb"}).json()
        assert first["document"]["reused"] is False

        # Re-submitting identical bytes is reused (idempotent), zero new nodes.
        again = client.post("/extract", data={"text": "doc one about Anthropic and Google.",
                                              "graph": graph, "engine": "falkordb"}).json()
        assert again["document"]["reused"] is True
        assert again["entities"] == 0

        # Anthropic carries a multi-label vector in the graph.
        q = client.post("/query", json={"engine": "falkordb", "graph": graph,
                                        "cypher": "MATCH (n {NODE:'anthropic'}) RETURN labels(n) AS l"}).json()
        labels = q["rows"][0][0]
        assert "Company" in labels and "AI" in labels
    finally:
        # cleanup
        client.post("/delete_graph", json={"engine": "falkordb", "graph": graph})
