from xgraph_gateway.adapters.fake import FakeAdapter
from xgraph_gateway.adapters.base import GraphEngineAdapter

def test_fake_is_adapter():
    assert isinstance(FakeAdapter(), GraphEngineAdapter)

def test_fake_query_and_schema():
    a = FakeAdapter()
    assert a.list_graphs() == ["demo_graph"]
    q = a.run_query("demo_graph", "MATCH (n) RETURN n.NODE AS NODE")
    assert q["columns"] == ["NODE"]
    assert ["b1"] in q["rows"]
    assert q["graph"]["nodes"]
    assert q["graph"]["edges"]
    sch = a.get_schema("demo_graph")
    assert "bank" in sch["labels"]
    assert sch["dot"].startswith("digraph")
