from xgraph_gateway import extract_fold, extract
from xgraph_gateway.compute.duckdb_engine import DuckDBComputeEngine


def _store(tmp_path):
    return DuckDBComputeEngine(meta_path=str(tmp_path / "m.duckdb"))


# ── S2: fold records relation labels under their LLM-assigned axis ─────────

def test_fold_records_relation_under_llm_axis(tmp_path):
    store = _store(tmp_path)
    entities = [{"id": "Tan", "label": "Person", "name": "Tan", "facets": [], "attrs": {}},
                {"id": "Kaan", "label": "Person", "name": "Kaan", "facets": [], "attrs": {}}]
    relations = [{"id": "r1", "src": "Tan", "dst": "Kaan", "label": "SON_OF", "axis": "FAMILY", "attrs": {}}]
    extract_fold.fold_labels(store, "g", entities, relations, "doc:1", llm=None)
    assert store.axis_map("g", "relation").get("SON_OF") == "FAMILY"


def test_fold_relation_axis_falls_back_when_absent(tmp_path):
    store = _store(tmp_path)
    relations = [{"id": "r1", "src": "a", "dst": "b", "label": "KNOWS", "attrs": {}}]
    extract_fold.fold_labels(store, "g", [], relations, "doc:1", llm=None)
    assert store.axis_map("g", "relation").get("KNOWS") == "RelationType"


# ── S2: extract_document carries the LLM's relation axis ──────────────────

def test_extract_document_carries_relation_axis():
    def fake(prompt, schema=None, model=None):
        return {
            "entities": [{"name": "Tan", "label": "Person"},
                         {"name": "Blomberg", "label": "Organization"}],
            "relations": [{"source": "Tan", "target": "Blomberg",
                           "label": "WORKS_FOR", "axis": "EMPLOYMENT"}],
        }
    res = extract.extract_document("Tan works for Blomberg", llm=fake)
    rels = res["relations"]
    assert len(rels) == 1
    assert rels[0]["label"] == "WORKS_FOR"
    assert rels[0]["axis"] == "EMPLOYMENT"
