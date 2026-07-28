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


def test_generate_cypher_prompt_includes_properties_and_name_guidance():
    captured = {}
    def capturing_llm(prompt, *, schema=None):
        captured["prompt"] = prompt
        return {"cypher": "MATCH (n) RETURN n LIMIT 1"}
    schema = dict(_SCHEMA, properties={
        "Organization": ["LABEL", "NODE", "name"],
        "Person": ["LABEL", "NODE", "name"],
    })
    nlcypher.generate_cypher(schema, "falkordb", "find Kinetica", llm=capturing_llm)
    prompt = captured["prompt"]
    assert "PROPERTIES" in prompt
    assert "Organization: LABEL, NODE, name" in prompt
    assert "Person: LABEL, NODE, name" in prompt
    assert "NODE" in prompt and "name" in prompt
    # Guidance phrase steering filters onto the human-readable property.
    assert "human-readable" in prompt.lower()
    assert "explicit id" in prompt.lower()


def test_schema_text_omits_properties_section_when_absent():
    assert "PROPERTIES" not in nlcypher._schema_text(_SCHEMA)


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


def test_synthesize_default_returns_string():
    # return_meta defaults False → grounded string, exactly as before.
    def fake(prompt, *, schema=None):
        return {"answer": "Two banks are involved.", "answered_from_results": True}
    out = nlcypher.synthesize("how many banks?", ["NODE"], [["b1"]], llm=fake)
    assert isinstance(out, str)
    assert out == "Two banks are involved."


def test_synthesize_return_meta_exposes_flag_true():
    def fake(prompt, *, schema=None):
        return {"answer": "grounded", "answered_from_results": True}
    out = nlcypher.synthesize("q", ["NODE"], [["b1"]], llm=fake, return_meta=True)
    assert out == {"answer": "grounded", "answered_from_results": True}


def test_synthesize_return_meta_flag_false_when_model_says_so():
    def fake(prompt, *, schema=None):
        return {"answer": "cannot be determined", "answered_from_results": False}
    out = nlcypher.synthesize("how old is X?", ["NODE"], [["b1"]], llm=fake, return_meta=True)
    assert out["answered_from_results"] is False


def test_synthesize_return_meta_empty_rows_forces_not_answered():
    # Robustness: empty results are always "not answered" regardless of the model flag.
    def fake(prompt, *, schema=None):
        return {"answer": "no rows", "answered_from_results": True}
    out = nlcypher.synthesize("q", ["NODE"], [], llm=fake, return_meta=True)
    assert out["answered_from_results"] is False


def test_synthesize_return_meta_defaults_flag_true_when_absent():
    # A model that omits the flag on non-empty rows is treated as answered.
    def fake(prompt, *, schema=None):
        return {"answer": "grounded"}
    out = nlcypher.synthesize("q", ["NODE"], [["b1"]], llm=fake, return_meta=True)
    assert out["answered_from_results"] is True


def test_general_knowledge_answer_uses_injected_llm():
    def fake(prompt, *, schema=None):
        assert "general knowledge" in prompt.lower()
        return {"answer": "Lindsey Graham is 70."}
    out = nlcypher.general_knowledge_answer("How old is Lindsey Graham?", llm=fake)
    assert out == "Lindsey Graham is 70."


def test_general_knowledge_answer_parses_json_string():
    def fake(prompt, *, schema=None):
        return '{"answer": "Lindsey Graham is 70."}'
    out = nlcypher.general_knowledge_answer("How old is Lindsey Graham?", llm=fake)
    assert out == "Lindsey Graham is 70."


def test_general_knowledge_answer_returns_plain_text():
    def fake(prompt, *, schema=None):
        return "  Lindsey Graham is 70.  "
    out = nlcypher.general_knowledge_answer("How old is Lindsey Graham?", llm=fake)
    assert out == "Lindsey Graham is 70."


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


def _capture_prompt(engine):
    """Run generate_cypher with a fake llm and return the prompt it built."""
    captured = {}
    def fake(prompt, *, schema=None):
        captured["p"] = prompt
        return {"cypher": "MATCH (n) RETURN n LIMIT 1"}
    schema = {"labels": ["Person"], "rel_types": ["WORKS_AT"],
              "properties": {"Person": ["NODE", "name"]}}
    nlcypher.generate_cypher(schema, engine, "who is not working at Kinetica?",
                             graph="g", llm=fake)
    return captured["p"]


def test_falkordb_prompt_has_negation_pattern_guidance():
    p = _capture_prompt("falkordb")
    assert "WHERE NOT (p)-[:WORKS_AT]" in p       # supported form
    assert "EXISTS { MATCH" in p                  # named as unsupported


def test_kinetica_prompt_has_scalar_negation_guidance_and_formats():
    # Also guards the .format(graph=...) brace-escaping: any stray unescaped
    # brace in the Kinetica dialect would raise here.
    p = _capture_prompt("kinetica")
    assert "<> 'Kinetica'" in p                   # supported scalar-negation form
    assert "EXISTS { ... }" in p                  # named as unsupported (escaped in source)


def test_prompts_require_scalar_identity_return_and_untyped_related():
    fp = _capture_prompt("falkordb")
    kp = _capture_prompt("kinetica")
    # scalar name/type return (not bare node objects)
    assert "b.name AS name, b.LABEL AS type" in fp
    assert "b.entity_name AS name, b.LABEL AS type" in kp
    assert "NEVER return a bare node" in fp
    # untyped relationship for "related to" questions
    assert "(a)-[r]-(b)" in fp
    assert "(a)-[r]-(b)" in kp


def test_kinetica_prompt_requires_graph_table_wrapper_form():
    # The Kinetica dialect must steer the LLM to the SELECT ... FROM graph_table(...)
    # wrapper (MATCH/RETURN inlined; aggregation/GROUP BY/ORDER BY in the OUTER SELECT),
    # NOT a bare `GRAPH "g" MATCH ... RETURN ...` (which ignores ORDER BY/aggregation).
    kp = _capture_prompt("kinetica")
    assert "graph_table" in kp                       # the wrapper is named
    assert "FROM graph_table" in kp                  # the SELECT ... FROM shape
    assert "OUTER SELECT" in kp                      # aggregation/order belong outside
    # The FalkorDB dialect must NOT mention graph_table (Cypher has no such wrapper).
    fp = _capture_prompt("falkordb")
    assert "graph_table" not in fp


def test_kinetica_prompt_quotes_special_char_rel_types():
    # Kinetica GQL reads '/' (and other non-identifier chars) in a bare rel type
    # as a regex/path operator, so `employs/is_employed_by` silently matches 0
    # rows unless double-quoted: -[ab:"employs/is_employed_by"]->. The dialect
    # must steer the LLM to quote such rel types/labels.
    kp = _capture_prompt("kinetica")
    assert 'employs/is_employed_by' in kp             # the worked example
    assert '[ab:"employs/is_employed_by"]' in kp      # shown double-quoted


def test_kinetica_graph_table_query_passes_readonly_validation():
    # A realistic wrapped Kinetica query (aggregation in the outer SELECT) must
    # survive the read-only guard — SELECT/graph_table is not a write.
    q = ('SELECT bank, ROUND(SUM(amount),2) AS total FROM graph_table ( '
         'GRAPH "expero.banking_graph" MATCH (a:bank)-[:performed]->(w:wire_message) '
         'RETURN a.bank_name AS bank, w.wire_message_risk_score AS amount ) '
         'GROUP BY bank ORDER BY total DESC')
    ok, reason = nlcypher.validate_cypher(q, _SCHEMA)
    assert ok, reason


def test_ask_explain_nlcypher_uses_fast_tier_for_all_engines(monkeypatch):
    # Ask/Explain (nl2cypher / synthesize / join-SQL) pins the fast Haiku tier
    # via nlcypher._get_llm — engine-agnostic, so Kinetica Ask/Explain is fast too.
    from xgraph_gateway import nlcypher as nl, llm as llmmod
    captured = {"model": "SENTINEL"}
    def fake_llm(prompt, *, schema=None, model=None):
        captured["model"] = model
        return {"cypher": "SELECT 1"}
    monkeypatch.setattr(llmmod, "_llm", fake_llm)
    monkeypatch.setattr(nl, "_llm_fn", None)
    nl.generate_cypher(_SCHEMA, "kinetica", "who?", graph="g")
    assert captured["model"] == llmmod.fast_model()
    assert "haiku" in captured["model"]
