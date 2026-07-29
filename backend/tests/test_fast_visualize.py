from __future__ import annotations
from fastapi.testclient import TestClient

from xgraph_gateway.adapters.fake import FakeAdapter
from xgraph_gateway.app import create_app


# --- Task 1: fetch_subgraph (concise/columnar, index-pair edges) -------------

def test_fetch_subgraph_concise_shape_full_pull():
    a = FakeAdapter()
    out = a.fetch_subgraph("demo_graph", 1000)
    for k in ("ids", "labels", "src", "dst", "etype", "total_nodes", "total_edges", "capped"):
        assert k in out, k
    # parallel-array invariants
    assert len(out["ids"]) == len(out["labels"])
    assert len(out["src"]) == len(out["dst"]) == len(out["etype"])
    # every edge index is a valid position in ids[]
    n = len(out["ids"])
    assert all(0 <= i < n for i in out["src"])
    assert all(0 <= i < n for i in out["dst"])
    # a generous limit is not capped, and true totals match the pulled full set
    assert out["capped"] is False
    assert out["total_nodes"] == n


def test_fetch_subgraph_index_pairs_resolve_to_fetch_entities_endpoints():
    # The concise src/dst indices must reconstruct the SAME endpoint ids that
    # the row-based fetch_entities returns for a full pull.
    a = FakeAdapter()
    ent = a.fetch_entities("demo_graph", 1000)
    sub = a.fetch_subgraph("demo_graph", 1000)
    ids = sub["ids"]
    concise_edges = {(ids[s], ids[d], t)
                     for s, d, t in zip(sub["src"], sub["dst"], sub["etype"])}
    row_edges = {(e["source"], e["target"], e["type"]) for e in ent["edges"]}
    assert concise_edges == row_edges


def test_fetch_subgraph_induced_filter_and_capped_flag():
    # Cap below the node total: only edges whose BOTH endpoints survived the cap
    # are kept, and capped is True with the true totals preserved.
    a = FakeAdapter()
    full = a.fetch_subgraph("demo_graph", 1000)
    cap = 1
    sub = a.fetch_subgraph("demo_graph", cap)
    assert len(sub["ids"]) <= cap
    assert sub["capped"] is (len(sub["ids"]) < full["total_nodes"])
    assert sub["total_nodes"] == full["total_nodes"]
    kept = set(range(len(sub["ids"])))
    assert all(s in kept and d in kept for s, d in zip(sub["src"], sub["dst"]))


# --- Task 2: GET /visualize endpoint -----------------------------------------

def _client():
    return TestClient(create_app(adapter_factory=lambda e: FakeAdapter()))


def test_visualize_endpoint_returns_concise_shape():
    c = _client()
    r = c.get("/visualize", params={"engine": "fake", "graph": "demo_graph", "limit": 1000})
    assert r.status_code == 200
    body = r.json()
    for k in ("ids", "labels", "src", "dst", "etype", "total_nodes", "total_edges", "capped"):
        assert k in body, k
    n = len(body["ids"])
    assert all(0 <= i < n for i in body["src"])


def test_visualize_endpoint_error_envelope_on_bad_graph():
    # An adapter that raises -> uniform error envelope, not a 200 payload.
    class Boom(FakeAdapter):
        def fetch_subgraph(self, graph, limit):
            raise ValueError("no such graph")
    c = TestClient(create_app(adapter_factory=lambda e: Boom()))
    r = c.get("/visualize", params={"engine": "fake", "graph": "nope", "limit": 10})
    assert "error" in r.json()
