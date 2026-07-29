import pytest
from fastapi.testclient import TestClient
from xgraph_gateway.app import create_app
from xgraph_gateway.adapters.fake import FakeAdapter
from xgraph_gateway import config


def test_fake_adapter_first_page_no_after():
    a = FakeAdapter()
    page = a.fetch_entities("demo_graph", 1)
    assert page["nodes"][0]["id"] == "b1"
    assert "props" not in page["nodes"][0]        # slim payload
    assert page["next_cursor"] == "b1"            # full page -> cursor


def test_fake_adapter_second_page_via_cursor():
    a = FakeAdapter()
    page = a.fetch_entities("demo_graph", 1, after="b1")
    assert page["nodes"][0]["id"] == "w1"


def test_fake_adapter_end_returns_null_cursor():
    a = FakeAdapter()
    page = a.fetch_entities("demo_graph", 10)     # both nodes, not a full page
    assert {n["id"] for n in page["nodes"]} == {"b1", "w1"}
    assert page["next_cursor"] is None


def test_fake_adapter_edges_scoped_to_page_source():
    a = FakeAdapter()
    # e1 has source b1: present on the page that contains b1, absent otherwise
    with_b1 = a.fetch_entities("demo_graph", 1, after=None)      # -> b1
    assert [e["id"] for e in with_b1["edges"]] == ["e1"]
    only_w1 = a.fetch_entities("demo_graph", 1, after="b1")      # -> w1
    assert only_w1["edges"] == []


def test_fake_full_coverage_no_dups():
    a = FakeAdapter()
    seen, cursor = [], None
    while True:
        page = a.fetch_entities("demo_graph", 1, after=cursor)
        if not page["nodes"]:
            break
        seen += [n["id"] for n in page["nodes"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == ["b1", "w1"]                   # every node once, in key order


def _client():
    return TestClient(create_app(adapter_factory=lambda e: FakeAdapter()))


def test_entities_endpoint_first_page():
    r = _client().get("/entities", params={"engine": "fake", "graph": "g", "limit": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["nodes"][0]["id"] == "b1"
    assert body["next_cursor"] == "b1"


def test_entities_endpoint_after_cursor():
    r = _client().get("/entities", params={"engine": "fake", "graph": "g", "limit": 1, "after": "b1"})
    assert r.status_code == 200
    assert r.json()["nodes"][0]["id"] == "w1"


def _falkordb_or_skip():
    from xgraph_gateway.adapters.falkordb_adapter import FalkorDBAdapter
    try:
        a = FalkorDBAdapter(config.load_settings())
        a.list_graphs()
        return a
    except Exception as e:
        pytest.skip(f"FalkorDB unreachable: {e}")


def test_live_falkordb_keyset_full_coverage():
    a = _falkordb_or_skip()
    if "banking_graph" not in a.list_graphs():
        pytest.skip("banking_graph not loaded")
    total = a.get_schema("banking_graph")["counts"]["nodes"]
    seen, cursor, pages = set(), None, 0
    while True:
        page = a.fetch_entities("banking_graph", 5000, after=cursor)
        ns = page["nodes"]
        if not ns:
            break
        before = len(seen)
        seen.update(n["id"] for n in ns)
        assert len(seen) == before + len(ns)      # no dup ids across pages
        assert "props" not in ns[0]               # slim payload
        cursor = page["next_cursor"]
        pages += 1
        if cursor is None:
            break
    assert len(seen) == total                      # complete coverage
    assert pages > 1
