import os, pytest
from fastapi.testclient import TestClient
from xgraph_gateway.app import create_app
from xgraph_gateway.config import load_settings

PARQUET = os.environ.get("XGRAPH_VERTEXES_PARQUET",
                         os.path.join(load_settings().data_dir, "vertexes.parquet"))

@pytest.fixture
def client():
    c = TestClient(create_app())
    if "banking_graph" not in c.get("/graphs", params={"engine": "falkordb"}).json():
        pytest.skip("banking_graph not available")
    return c

def test_query_then_hydrate_surfaces_ungraphed_column(client):
    if not os.path.exists(PARQUET):
        pytest.skip("vertexes.parquet not present")
    q = client.post("/query", json={"engine": "falkordb", "graph": "banking_graph",
        "cypher": "MATCH (b:bank) RETURN b.NODE AS NODE LIMIT 5"})
    assert q.status_code == 200
    ids = [{"NODE": row[0]} for row in q.json()["rows"]]
    h = client.post("/hydrate", json={"rows": ids, "source": PARQUET, "key": "NODE",
        "columns": 'NODE, "bank:bank_number" AS bank_number'})
    assert h.status_code == 200
    out = h.json()
    assert len(out) == 5
    assert all("bank_number" in r for r in out)   # column never stored in the graph

def test_bank_count_matches_between_falkordb_and_kinetica():
    c = TestClient(create_app())
    fk = c.get("/graphs", params={"engine": "falkordb"}).json()
    if "banking_graph" not in fk:
        pytest.skip("banking_graph not available")
    fq = c.post("/query", json={"engine": "falkordb", "graph": "banking_graph",
        "cypher": "MATCH (b:bank) RETURN count(b) AS c"})
    if fq.status_code != 200:
        pytest.skip(f"falkordb query failed: {fq.json()}")
    kq = c.post("/query", json={"engine": "kinetica", "graph": "",
        "cypher": "SELECT COUNT(DISTINCT id) AS c FROM expero.vertexes WHERE label = 'bank'"})
    if kq.status_code != 200:
        pytest.skip(f"kinetica unavailable: {kq.json()}")
    falkor_count = fq.json()["rows"][0][0]
    kinetica_count = kq.json()["rows"][0][0]
    print(f"\n[parity] FalkorDB bank count = {falkor_count}, Kinetica COUNT(DISTINCT id) WHERE label='bank' = {kinetica_count}")
    assert falkor_count == kinetica_count
