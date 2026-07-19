import duckdb
import pytest
from fastapi.testclient import TestClient
from xgraph_gateway.app import create_app
from xgraph_gateway.sessions import SessionStore
from xgraph_gateway.adapters.fake import FakeAdapter
from xgraph_gateway.compute.duckdb_engine import DuckDBComputeEngine


def _store():
    return SessionStore(adapter_factory=lambda e, c=None: FakeAdapter(),
                        compute_factory=lambda e, c=None: object())


# ── Task 1: SessionStore.register_file (unit) ─────────────────────────────

def test_register_file_records_path_on_session():
    st = _store()
    sid = st.create("fake", None, "duckdb", None)
    st.register_file(sid, "vertexes.parquet")
    st.register_file(sid, "vertexes.parquet")  # dedup
    st.register_file(sid, "edges.parquet")
    assert st.get(sid)["files"] == ["vertexes.parquet", "edges.parquet"]


def test_register_file_unknown_session_raises():
    st = _store()
    with pytest.raises(KeyError):
        st.register_file("s999", "x.parquet")


def test_new_session_has_empty_files():
    st = _store()
    sid = st.create("fake", None, "duckdb", None)
    assert st.get(sid)["files"] == []


# ── Task 2: POST /register_file endpoint + /tables·/columns merge ──────────

def _app(tmp_path):
    store = SessionStore(
        adapter_factory=lambda e, c=None: FakeAdapter(),
        compute_factory=lambda e, c=None: DuckDBComputeEngine(meta_path=str(tmp_path / "m.duckdb")))
    return TestClient(create_app(
        adapter_factory=lambda e: FakeAdapter(),
        compute=DuckDBComputeEngine(meta_path=str(tmp_path / "m2.duckdb")),
        store=store))


def _parquet(tmp_path):
    p = tmp_path / "v.parquet"
    con = duckdb.connect()
    con.execute(f"COPY (SELECT 1 AS id, 'bank' AS label) TO '{p}' (FORMAT PARQUET)")
    con.close()
    return str(p)


def _connect(client):
    return client.post("/connect", json={"graph": {"engine": "fake"},
                                         "compute": {"engine": "duckdb"}}).json()["session"]


def test_register_file_validates_and_lists(tmp_path):
    client = _app(tmp_path)
    sid = _connect(client)
    p = _parquet(tmp_path)
    r = client.post("/register_file", json={"session": sid, "path": p})
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == p and body["type"] == "file"
    assert body["columns"] == ["id", "label"]
    tbls = client.get("/tables", params={"session": sid}).json()
    assert {"name": p, "type": "file"} in tbls
    cols = client.get("/columns", params={"session": sid, "table": p}).json()
    assert cols == ["id", "label"]


def test_register_file_bad_path_errors(tmp_path):
    client = _app(tmp_path)
    sid = _connect(client)
    r = client.post("/register_file", json={"session": sid, "path": "/no/such/file.parquet"})
    assert r.status_code >= 400
    assert "error" in r.json()


def test_register_file_requires_session(tmp_path):
    client = _app(tmp_path)
    r = client.post("/register_file", json={"path": "x.parquet"})
    assert r.status_code >= 400
    assert "error" in r.json()
