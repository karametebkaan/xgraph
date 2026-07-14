from __future__ import annotations
from fastapi.testclient import TestClient

from xgraph_gateway import nlcypher
from xgraph_gateway.app import create_app
from xgraph_gateway.adapters.fake import FakeAdapter


def fake_llm(prompt, *, schema=None):
    """Canned LLM: never shells out to the real `claude` CLI."""
    if schema and "cypher" in schema.get("properties", {}):
        return {"cypher": "MATCH (n) RETURN n LIMIT 1"}
    if schema and "answer" in schema.get("properties", {}):
        return {"answer": "There is one node."}
    return "There is one node."


_SCHEMA = {"labels": ["bank", "wire_message"], "rel_types": ["performed"],
           "dot": 'digraph { "bank" -> "wire_message" [label="performed"]; }',
           "counts": {"nodes": 2, "edges": 1}}


def test_generate_cypher_uses_injected_llm():
    cypher = nlcypher.generate_cypher(_SCHEMA, "falkordb", "how many banks?", llm=fake_llm)
    assert cypher == "MATCH (n) RETURN n LIMIT 1"


def test_generate_cypher_kinetica_dialect_prompt_mentions_graph_clause():
    captured = {}
    def capturing_llm(prompt, *, schema=None):
        captured["prompt"] = prompt
        return {"cypher": "GRAPH \"demo_graph\" MATCH (a) RETURN a"}
    cypher = nlcypher.generate_cypher(_SCHEMA, "kinetica", "who?", graph="demo_graph", llm=capturing_llm)
    assert cypher == 'GRAPH "demo_graph" MATCH (a) RETURN a'
    assert 'GRAPH "demo_graph"' in captured["prompt"]
    assert "Kinetica GQL" in captured["prompt"]


def test_validate_cypher_rejects_delete():
    ok, reason = nlcypher.validate_cypher("MATCH (n) DELETE n", _SCHEMA)
    assert ok is False
    assert "read-only" in reason.lower() or "DELETE" in reason.upper()


def test_validate_cypher_rejects_create():
    ok, reason = nlcypher.validate_cypher("CREATE (n:bank) RETURN n", _SCHEMA)
    assert ok is False


def test_validate_cypher_accepts_match_return():
    ok, reason = nlcypher.validate_cypher("MATCH (n:bank) RETURN n LIMIT 10", _SCHEMA)
    assert ok is True
    assert reason == ""


def test_synthesize_uses_injected_llm():
    answer = nlcypher.synthesize("how many banks?", ["NODE"], [["b1"]], llm=fake_llm)
    assert answer == "There is one node."


def test_synthesize_prompt_includes_cypher_and_domain_guidance():
    captured = {}
    def capturing_llm(prompt, *, schema=None):
        captured["prompt"] = prompt
        return {"answer": "There is one node."}
    cypher = "MATCH (a:bank)-[:performed]->(b:wire_message) RETURN a, b"
    answer = nlcypher.synthesize("how many banks?", ["NODE"], [["b1"]],
                                  llm=capturing_llm, cypher=cypher)
    assert answer == "There is one node."
    prompt = captured["prompt"]
    assert cypher in prompt
    assert "analyst" in prompt.lower()
    assert "do not" in prompt.lower()


def test_synthesize_prompt_omits_cypher_block_when_not_provided():
    captured = {}
    def capturing_llm(prompt, *, schema=None):
        captured["prompt"] = prompt
        return {"answer": "ok"}
    nlcypher.synthesize("how many banks?", ["NODE"], [["b1"]], llm=capturing_llm)
    assert "Query (Cypher)" not in captured["prompt"]


def _client():
    return TestClient(create_app(adapter_factory=lambda e: FakeAdapter()))


def test_ask_endpoint_full_roundtrip(monkeypatch):
    monkeypatch.setattr(nlcypher, "_get_llm", lambda: fake_llm)
    c = _client()
    r = c.post("/ask", json={"engine": "fake", "graph": "demo_graph", "question": "how many banks?"})
    assert r.status_code == 200
    body = r.json()
    assert body["question"] == "how many banks?"
    assert body["cypher"] == "MATCH (n) RETURN n LIMIT 1"
    assert body["columns"] == ["NODE"]
    assert ["b1"] in body["rows"]
    assert body["answer"] == "There is one node."


def test_nl2cypher_endpoint(monkeypatch):
    monkeypatch.setattr(nlcypher, "_get_llm", lambda: fake_llm)
    c = _client()
    r = c.post("/nl2cypher", json={"engine": "fake", "graph": "demo_graph", "question": "x"})
    assert r.status_code == 200
    assert r.json() == {"cypher": "MATCH (n) RETURN n LIMIT 1"}


def test_synthesize_endpoint(monkeypatch):
    monkeypatch.setattr(nlcypher, "_get_llm", lambda: fake_llm)
    c = _client()
    r = c.post("/synthesize", json={"engine": "fake", "question": "x",
                                     "columns": ["NODE"], "rows": [["b1"]]})
    assert r.status_code == 200
    assert r.json() == {"answer": "There is one node."}


def test_generate_join_sql_uses_injected_llm_and_prompt_contents():
    captured = {}
    def capturing_llm(prompt, *, schema=None):
        captured["prompt"] = prompt
        captured["schema"] = schema
        return {"sql": "SELECT w.party_name, COUNT(*) AS n FROM cypher c "
                        "JOIN wide w ON c.c_node=w.NODE GROUP BY 1;"}
    focus = "who has the most SAR activity by party_name"
    cypher = "MATCH (c:party)-[:filed]->(s:sar) RETURN c.NODE AS c_node, s.NODE AS s_node"
    result_columns = ["c_node", "s_node"]
    wide_columns = ["NODE", "party_name", "tin"]
    sql = nlcypher.generate_join_sql(focus, cypher, result_columns, wide_columns,
                                      llm=capturing_llm)
    assert sql == ("SELECT w.party_name, COUNT(*) AS n FROM cypher c "
                    "JOIN wide w ON c.c_node=w.NODE GROUP BY 1")
    prompt = captured["prompt"]
    assert focus in prompt
    assert cypher in prompt
    assert "c_node" in prompt
    assert "party_name" in prompt
    assert captured["schema"] == nlcypher._JOIN_SQL_SCHEMA


def test_generate_join_sql_empty_when_no_wide_column_needed():
    def empty_llm(prompt, *, schema=None):
        return {"sql": ""}
    sql = nlcypher.generate_join_sql("how many banks?", "MATCH (n) RETURN n", ["NODE"],
                                      ["NODE", "party_name"], llm=empty_llm)
    assert sql == ""


def test_validate_sql_accepts_select_join_groupby():
    ok, reason = nlcypher.validate_sql(
        "SELECT w.party_name, COUNT(*) FROM cypher c JOIN wide w ON c.c_node=w.NODE GROUP BY 1")
    assert ok is True
    assert reason == ""


def test_validate_sql_rejects_drop():
    ok, reason = nlcypher.validate_sql("DROP TABLE wide")
    assert ok is False
    assert reason


def test_validate_sql_rejects_multi_statement():
    ok, reason = nlcypher.validate_sql("SELECT 1; DELETE FROM wide")
    assert ok is False
    assert reason


def test_validate_sql_rejects_empty():
    ok, reason = nlcypher.validate_sql("")
    assert ok is False
    assert reason


def test_ask_endpoint_rejects_write_query(monkeypatch):
    def write_llm(prompt, *, schema=None):
        if schema and "cypher" in schema.get("properties", {}):
            return {"cypher": "MATCH (n) DELETE n"}
        return {"answer": "n/a"}
    monkeypatch.setattr(nlcypher, "_get_llm", lambda: write_llm)
    c = _client()
    r = c.post("/ask", json={"engine": "fake", "graph": "demo_graph", "question": "delete everything"})
    assert r.status_code == 400
    assert "error" in r.json()
