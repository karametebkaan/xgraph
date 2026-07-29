import pytest
from xgraph_gateway import config

# Live FalkorDB coherence for fetch_subgraph (A+B fast Visualize). SKIPS (never
# fails) when FalkorDB is unreachable or banking_graph is absent -- mirrors the
# skip pattern in test_paging.py / test_falkordb_adapter.py.


def _falkordb_or_skip():
    from xgraph_gateway.adapters.falkordb_adapter import FalkorDBAdapter
    try:
        a = FalkorDBAdapter(config.load_settings())
        a.list_graphs()
        return a
    except Exception as e:
        pytest.skip(f"FalkorDB unreachable: {e}")


GRAPH = "banking_graph"


def test_live_fetch_subgraph_full_pull_is_coherent_and_keeps_all_edges():
    a = _falkordb_or_skip()
    if GRAPH not in a.list_graphs():
        pytest.skip(f"{GRAPH} not loaded")
    counts = a._counts(a._graph(GRAPH))
    # Full pull: limit >= node count -> every node and every edge present.
    full = a.fetch_subgraph(GRAPH, counts["nodes"])
    n = len(full["ids"])
    assert n == counts["nodes"]                        # complete node coverage
    assert len(set(full["ids"])) == n                  # no dup node ids
    assert len(full["labels"]) == n                    # parallel arrays
    assert len(full["src"]) == len(full["dst"]) == len(full["etype"])
    # every edge index is a valid position in ids[] (coherent subgraph)
    assert all(0 <= i < n for i in full["src"])
    assert all(0 <= i < n for i in full["dst"])
    # a full pull drops no edge, and is not capped; true totals reported
    assert len(full["src"]) == counts["edges"]
    assert full["capped"] is False
    assert full["total_nodes"] == counts["nodes"]
    assert full["total_edges"] == counts["edges"]


def test_live_fetch_subgraph_capped_pull_is_coherent_subset():
    a = _falkordb_or_skip()
    if GRAPH not in a.list_graphs():
        pytest.skip(f"{GRAPH} not loaded")
    counts = a._counts(a._graph(GRAPH))
    if counts["nodes"] <= 100:
        pytest.skip("graph too small to exercise the cap")
    cap = 100
    sub = a.fetch_subgraph(GRAPH, cap)
    n = len(sub["ids"])
    assert n == cap
    # induced subgraph: every kept edge's endpoints are within the pulled set
    assert all(0 <= i < n for i in sub["src"])
    assert all(0 <= i < n for i in sub["dst"])
    assert sub["capped"] is True                       # cap < true total
    assert sub["total_nodes"] == counts["nodes"]
