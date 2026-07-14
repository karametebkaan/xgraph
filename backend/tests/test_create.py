import os
import pytest
from fastapi.testclient import TestClient

from xgraph_gateway import config
from xgraph_gateway.adapters.fake import FakeAdapter
from xgraph_gateway.adapters.falkordb_adapter import _mapping_from_spec
from xgraph_gateway.app import create_app

# ---------------------------------------------------------------------------
# Unit: spec -> Mapping builder, no DB.
# ---------------------------------------------------------------------------

_SPEC = {
    "graph": "xgraph_create_test",
    "tables": {"expero.vertexes": "v.parquet", "expero.edges": "e.parquet"},
    "nodes": [{
        "sql": "SELECT id AS node_id, label AS label, name AS name FROM expero.vertexes",
        "id": "node_id",
        "label_column": "label",
        "properties": ["name"],
    }],
    "edges": [{
        "sql": "SELECT id AS edge_id, source_name AS node1, target_name AS node2, label AS label FROM expero.edges",
        "id": "edge_id",
        "type_column": "label",
        "source_key": "node1",
        "target_key": "node2",
        "properties": [],
    }],
}

def test_mapping_from_spec_uses_defaults():
    mapping = _mapping_from_spec(_SPEC)
    assert mapping.graph == "xgraph_create_test"
    assert mapping.node_key_property == "NODE"

    node = mapping.nodes[0]
    assert node.sql == _SPEC["nodes"][0]["sql"]
    assert node.id == "node_id"
    assert node.id_property == "NODE"          # default
    assert node.label_column == "label"
    assert node.label_property == "LABEL"       # default
    assert node.properties == ["name"]

    edge = mapping.edges[0]
    assert edge.sql == _SPEC["edges"][0]["sql"]
    assert edge.id == "edge_id"
    assert edge.id_property == "ID"             # default
    assert edge.type_column == "label"
    assert edge.type_property == "LABEL"        # default
    assert edge.source_key == "node1"
    assert edge.target_key == "node2"
    assert edge.properties == []

def test_mapping_from_spec_honors_explicit_overrides():
    spec = dict(_SPEC)
    spec["node_key_property"] = "MY_ID"
    spec["nodes"] = [dict(_SPEC["nodes"][0], id_property="MY_ID", label_property="TYPE")]
    spec["edges"] = [dict(_SPEC["edges"][0], id_property="EID", type_property="ETYPE")]
    mapping = _mapping_from_spec(spec)
    assert mapping.node_key_property == "MY_ID"
    assert mapping.nodes[0].id_property == "MY_ID"
    assert mapping.nodes[0].label_property == "TYPE"
    assert mapping.edges[0].id_property == "EID"
    assert mapping.edges[0].type_property == "ETYPE"


# ---------------------------------------------------------------------------
# Gateway: /create routes to the resolved adapter's load_graph.
# ---------------------------------------------------------------------------

def _client():
    return TestClient(create_app(adapter_factory=lambda e: FakeAdapter()))

def test_create_endpoint_returns_fake_counts():
    r = _client().post("/create", json={"engine": "fake", "spec": {"graph": "g"}})
    assert r.status_code == 200
    assert r.json() == {"nodes": {"bank": 2}, "edges": {"performed": 1}}

def test_create_endpoint_error_returns_envelope():
    def boom(e):
        class A(FakeAdapter):
            def load_graph(self, spec):
                raise ValueError("bad spec")
        return A()
    c = TestClient(create_app(adapter_factory=boom))
    r = c.post("/create", json={"engine": "fake", "spec": {}})
    assert r.status_code == 400
    assert r.json()["error"]["message"] == "bad spec"


# ---------------------------------------------------------------------------
# Live: build a real FalkorDB graph from the banking demo Parquet via
# /connect + /create. Skips (not fails) if FalkorDB is unreachable or the
# Parquet is absent (run ./scripts/unzip-data.sh).
# ---------------------------------------------------------------------------

DEMO_DATA_DIR = os.environ.get("XGRAPH_DATA_DIR", config.load_settings().data_dir)
VERTEXES_PARQUET = os.path.join(DEMO_DATA_DIR, "vertexes.parquet")
EDGES_PARQUET = os.path.join(DEMO_DATA_DIR, "edges.parquet")

LIVE_GRAPH = "xgraph_create_test"

_LIVE_SPEC = {
    "graph": LIVE_GRAPH,
    "tables": {
        "expero.vertexes": VERTEXES_PARQUET,
        "expero.edges": EDGES_PARQUET,
    },
    "nodes": [{
        "sql": """
            SELECT
              id    AS node_id,
              label AS label,
              "party:party_name" AS party_name,
              "bank:bank_name"   AS bank_name
            FROM expero.vertexes
        """,
        "id": "node_id",
        "label_column": "label",
        "properties": ["party_name", "bank_name"],
    }],
    "edges": [{
        "sql": """
            SELECT
              id          AS edge_id,
              source_name AS node1,
              target_name AS node2,
              label       AS label
            FROM expero.edges
        """,
        "id": "edge_id",
        "type_column": "label",
        "source_key": "node1",
        "target_key": "node2",
        "properties": [],
    }],
}

def _live_client_or_skip():
    if not os.path.exists(VERTEXES_PARQUET) or not os.path.exists(EDGES_PARQUET):
        pytest.skip("falkor vertexes/edges parquet fixtures not present")
    c = TestClient(create_app())
    settings = config.load_settings()
    conn = {"host": settings.falkordb_host, "port": settings.falkordb_port,
            "password": settings.falkordb_password}
    try:
        r = c.post("/connect", json={
            "graph": {"engine": "falkordb", "conn": conn},
            "compute": {"engine": "duckdb", "conn": None},
        })
        if r.status_code != 200:
            pytest.skip(f"FalkorDB unreachable: {r.json()}")
        return c, r.json()["session"]
    except Exception as e:
        pytest.skip(f"FalkorDB unreachable: {e}")

def test_live_create_builds_graph_from_parquet():
    c, session = _live_client_or_skip()
    r = c.post("/create", json={"session": session, "spec": _LIVE_SPEC})
    assert r.status_code == 200, r.json()
    counts = r.json()
    total_nodes = sum(counts["nodes"].values())
    total_edges = sum(counts["edges"].values())
    assert total_nodes > 0
    assert total_edges > 0

    graphs = c.get("/graphs", params={"session": session}).json()
    assert LIVE_GRAPH in graphs

    # Cleanup: delete the test graph via the same session's FalkorDB connection.
    from falkordb import FalkorDB
    settings = config.load_settings()
    db = FalkorDB(host=settings.falkordb_host, port=settings.falkordb_port,
                  password=settings.falkordb_password)
    if LIVE_GRAPH in db.list_graphs():
        db.select_graph(LIVE_GRAPH).delete()
