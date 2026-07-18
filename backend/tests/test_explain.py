from __future__ import annotations
import duckdb
from fastapi.testclient import TestClient

from xgraph_gateway import nlcypher
from xgraph_gateway.app import create_app
from xgraph_gateway.adapters.fake import FakeAdapter
from xgraph_gateway.compute.duckdb_engine import ComputeEngine


def _wide(tmp_path):
    p = tmp_path / "wide.parquet"
    con = duckdb.connect()
    con.execute("""CREATE TABLE t AS SELECT * FROM (VALUES
        ('party-A','Acme'),('party-B','Beta')) AS v(NODE, party_name)""")
    con.execute(f"COPY t TO '{p}' (FORMAT parquet)"); con.close()
    return str(p)


_JOIN_SQL = ("SELECT w.party_name, COUNT(*) AS sar_paths FROM cypher c "
             "JOIN wide w ON c.c_node=w.NODE GROUP BY w.party_name ORDER BY sar_paths DESC")


def test_run_join_aggregates_over_registered_rows(tmp_path):
    eng = ComputeEngine()
    rows = [{"c_node": "party-A"}, {"c_node": "party-A"}, {"c_node": "party-B"}]
    out = eng.run_join(rows, _wide(tmp_path), _JOIN_SQL)
    assert out[0]["party_name"] == "Acme"
    assert out[0]["sar_paths"] == 2


def test_run_join_empty_rows_returns_empty(tmp_path):
    eng = ComputeEngine()
    assert eng.run_join([], _wide(tmp_path), _JOIN_SQL) == []


def test_run_join_rejects_unsafe_relation_name(tmp_path):
    eng = ComputeEngine()
    try:
        eng.run_join([{"c_node": "party-A"}], _wide(tmp_path), _JOIN_SQL,
                      cypher_relation="bad; DROP")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_describe_source_lists_columns(tmp_path):
    eng = ComputeEngine()
    cols = eng.describe_source(_wide(tmp_path))
    assert "NODE" in cols
    assert "party_name" in cols


def _client(compute=None):
    return TestClient(create_app(adapter_factory=lambda e: FakeAdapter(), compute=compute))


def test_explain_endpoint_hydrated(tmp_path, monkeypatch):
    source = _wide(tmp_path)
    cypher = "MATCH (c:party)-[:filed]->(s:sar) RETURN c.NODE AS c_node"
    columns = ["c_node"]
    rows = [["party-A"], ["party-A"], ["party-B"]]

    def fake_generate_join_sql(focus, cyp, result_columns, wide_columns, llm=None):
        assert focus == "who has the most SAR activity by party_name"
        assert cyp == cypher
        assert result_columns == columns
        return _JOIN_SQL

    def fake_synthesize(question, cols, rows_, llm=None, cypher=None):
        assert cols == ["party_name", "sar_paths"]
        assert rows_[0] == ["Acme", 2]
        return "Acme has the most SAR activity with 2 paths."

    monkeypatch.setattr(nlcypher, "generate_join_sql", fake_generate_join_sql)
    monkeypatch.setattr(nlcypher, "synthesize", fake_synthesize)

    c = _client()
    r = c.post("/explain", json={
        "question": "who has the most SAR activity by party_name",
        "columns": columns, "rows": rows, "cypher": cypher, "source": source,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["hydrated"] is True
    assert body["join_sql"] == _JOIN_SQL
    assert body["columns"] == ["party_name", "sar_paths"]
    assert body["rows"][0] == ["Acme", 2]
    assert body["answer"] == "Acme has the most SAR activity with 2 paths."


def test_explain_endpoint_no_wide_column_needed(tmp_path, monkeypatch):
    source = _wide(tmp_path)
    columns = ["c_node"]
    rows = [["party-A"]]

    def fake_generate_join_sql(focus, cyp, result_columns, wide_columns, llm=None):
        return ""

    calls = {}
    def fake_synthesize(question, cols, rows_, llm=None, cypher=None):
        calls["called"] = True
        assert cols == columns
        assert rows_ == rows
        return "plain answer"

    monkeypatch.setattr(nlcypher, "generate_join_sql", fake_generate_join_sql)
    monkeypatch.setattr(nlcypher, "synthesize", fake_synthesize)

    c = _client()
    r = c.post("/explain", json={
        "question": "how many rows?", "columns": columns, "rows": rows,
        "cypher": "MATCH (n) RETURN n.NODE AS c_node", "source": source,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["hydrated"] is False
    assert body["join_sql"] is None
    assert body["columns"] == columns
    assert body["rows"] == rows
    assert body["answer"] == "plain answer"
    assert calls.get("called") is True


def test_explain_endpoint_no_focus_falls_back_to_plain_synthesize(monkeypatch):
    columns = ["c_node"]
    rows = [["party-A"]]

    def boom_generate_join_sql(*a, **k):
        raise AssertionError("generate_join_sql must not be called when focus is empty")

    def fake_synthesize(question, cols, rows_, llm=None, cypher=None):
        assert question == "Explain these results"
        assert cols == columns
        assert rows_ == rows
        return "plain fallback answer"

    monkeypatch.setattr(nlcypher, "generate_join_sql", boom_generate_join_sql)
    monkeypatch.setattr(nlcypher, "synthesize", fake_synthesize)

    c = _client()
    r = c.post("/explain", json={
        "question": "", "columns": columns, "rows": rows,
        "cypher": "MATCH (n) RETURN n.NODE AS c_node",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["hydrated"] is False
    assert body["join_sql"] is None
    assert body["answer"] == "plain fallback answer"


def test_explain_endpoint_rejects_write_join_sql(tmp_path, monkeypatch):
    source = _wide(tmp_path)
    columns = ["c_node"]
    rows = [["party-A"]]

    def fake_generate_join_sql(focus, cyp, result_columns, wide_columns, llm=None):
        return "DROP TABLE wide"

    monkeypatch.setattr(nlcypher, "generate_join_sql", fake_generate_join_sql)

    c = _client()
    r = c.post("/explain", json={
        "question": "who has the most SAR activity?", "columns": columns, "rows": rows,
        "cypher": "MATCH (n) RETURN n.NODE AS c_node", "source": source,
    })
    assert r.status_code == 400
    assert "error" in r.json()
    assert r.json()["error"]["code"]


class _KineticaLikeCompute:
    """Simulates KineticaComputeEngine: has hydrate/run_sql but NOT the file-based
    describe_source/run_join. /explain must not depend on the session's compute."""
    def hydrate(self, *a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("/explain must not use the session compute")
    def run_sql(self, *a, **k):  # pragma: no cover
        raise AssertionError("/explain must not use the session compute")


class _FakeStore:
    def __init__(self, compute):
        self._c = compute
    def get(self, sid):
        return {"adapter": FakeAdapter(), "compute": self._c, "graph_engine": "kinetica"}


def test_explain_uses_duckdb_even_when_session_olap_is_kinetica(tmp_path, monkeypatch):
    # Regression: with OLAP/ingest = Kinetica the session compute has no
    # describe_source/run_join; the file-based post-join must still run via DuckDB.
    source = _wide(tmp_path)
    columns = ["c_node"]
    rows = [["party-A"], ["party-A"], ["party-B"]]

    monkeypatch.setattr(nlcypher, "generate_join_sql",
                        lambda focus, cyp, rc, wc, llm=None: _JOIN_SQL)
    monkeypatch.setattr(nlcypher, "synthesize",
                        lambda q, cols, rws, llm=None, cypher=None: "ok")

    app = create_app(adapter_factory=lambda e: FakeAdapter(),
                     store=_FakeStore(_KineticaLikeCompute()))
    r = TestClient(app).post("/explain", json={
        "session": "s1", "question": "who has the most SAR activity by party_name",
        "columns": columns, "rows": rows, "cypher": "x", "source": source,
    })

    assert r.status_code == 200
    body = r.json()
    assert body["hydrated"] is True
    assert body["columns"] == ["party_name", "sar_paths"]
    assert body["rows"][0] == ["Acme", 2]


def test_explain_hydrates_from_graph_when_nodes_have_attrs(monkeypatch):
    # Extracted-graph model: attributes live ON the nodes. Explain must post-join
    # the graph's own node attrs (fetch_node_attrs), NOT the external Parquet.
    client = TestClient(create_app(adapter_factory=lambda e: FakeAdapter()))
    monkeypatch.setattr(nlcypher, "generate_join_sql",
        lambda focus, cypher, cols, wide_cols:
            "SELECT wide.bank_name AS bank, COUNT(*) AS n FROM cypher "
            "JOIN wide ON cypher.NODE = wide.NODE GROUP BY wide.bank_name")
    monkeypatch.setattr(nlcypher, "synthesize", lambda *a, **k: "ok")
    r = client.post("/explain", json={
        "question": "which banks", "columns": ["NODE"], "rows": [["b1"]],
        "cypher": "MATCH (n) RETURN n.NODE", "graph": "g", "engine": "fake",
        "source": "vertexes.parquet"})  # source present, but graph attrs win
    body = r.json()
    assert body["hydrate_from"] == "graph"
    assert any("Acme" in str(v) for row in body["rows"] for v in row)
