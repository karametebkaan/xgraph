from xgraph_gateway import extract_fold


class FakeStore:
    """In-memory duck-typed metadata store (mirrors DuckDBComputeEngine's
    ontology methods) so folding tests need no DuckDB."""
    def __init__(self):
        self.rows = {}  # (graph, kind, name) -> (canonical, axis)
        self.source_uris = {}  # (graph, kind, name) -> source_uri (first-seen)

    def resolve_canonical(self, graph, kind, name):
        hit = self.rows.get((graph, kind, name))
        if hit:
            return hit[0]
        for (g, k, n), (canon, _axis) in self.rows.items():
            if g == graph and k == kind and n.lower() == name.lower():
                return canon
        return None

    def get_canonicals(self, graph, kind):
        return sorted({c for (g, k, _n), (c, _a) in self.rows.items()
                       if g == graph and k == kind})

    def record_type(self, graph, kind, name, canonical, axis, source_uri):
        self.rows.setdefault((graph, kind, name), (canonical, axis))
        self.source_uris.setdefault((graph, kind, name), source_uri)


def _no_llm(prompt, *, schema=None):
    raise AssertionError("LLM should not be called in this test")


def test_known_alias_resolves_without_llm():
    store = FakeStore()
    store.record_type("g", "entity", "Company", "Company", "EntityType", "doc")
    store.record_type("g", "entity", "Firm", "Company", "EntityType", "doc")
    ents = [{"name": "Acme", "label": "Firm", "attrs": {}}]
    report = extract_fold.fold_labels(store, "g", ents, [], "doc", llm=_no_llm)
    assert ents[0]["label"] == "Company"
    assert {"kind": "entity", "from": "Firm", "to": "Company", "axis": "EntityType"} in report


def test_new_name_llm_folds_to_existing_canonical():
    store = FakeStore()
    store.record_type("g", "entity", "Company", "Company", "EntityType", "doc")

    def llm(prompt, *, schema=None):
        return {"canonical": "Company"}  # LLM says "Corporation" ~ "Company"

    ents = [{"name": "Acme", "label": "Corporation", "attrs": {}}]
    report = extract_fold.fold_labels(store, "g", ents, [], "doc", llm=llm)
    assert ents[0]["label"] == "Company"
    # Alias persisted so next time is deterministic.
    assert store.resolve_canonical("g", "entity", "Corporation") == "Company"


def test_genuinely_new_becomes_its_own_canonical():
    store = FakeStore()

    def llm(prompt, *, schema=None):
        return {"canonical": None}

    ents = [{"name": "Mars", "label": "Planet", "attrs": {}}]
    extract_fold.fold_labels(store, "g", ents, [], "doc", llm=llm)
    assert ents[0]["label"] == "Planet"
    assert store.resolve_canonical("g", "entity", "Planet") == "Planet"


def test_llm_error_treated_as_new_canonical():
    store = FakeStore()
    store.record_type("g", "entity", "Company", "Company", "EntityType", "doc")

    def llm(prompt, *, schema=None):
        raise RuntimeError("llm down")

    ents = [{"name": "Acme", "label": "Startup", "attrs": {}}]
    extract_fold.fold_labels(store, "g", ents, [], "doc", llm=llm)
    assert ents[0]["label"] == "Startup"  # never blocks ingest


def test_relations_fold_on_relation_kind():
    store = FakeStore()
    store.record_type("g", "relation", "WORKS_AT", "WORKS_AT", "RelationType", "doc")
    store.record_type("g", "relation", "EMPLOYED_BY", "WORKS_AT", "RelationType", "doc")
    rels = [{"src": "a", "dst": "b", "label": "EMPLOYED_BY", "attrs": {}}]
    extract_fold.fold_labels(store, "g", [], rels, "doc", llm=_no_llm)
    assert rels[0]["label"] == "WORKS_AT"


def test_source_uri_is_persisted():
    store = FakeStore()

    def llm(prompt, *, schema=None):
        return {"canonical": None}

    ents = [{"name": "Ceres", "label": "DwarfPlanet", "attrs": {}}]
    extract_fold.fold_labels(store, "g", ents, [], "doc:xyz", llm=llm)
    assert ents[0]["label"] == "DwarfPlanet"
    assert store.source_uris[("g", "entity", "DwarfPlanet")] == "doc:xyz"


def test_facets_folded_and_vector_built():
    store = FakeStore()
    store.record_type("g", "entity", "Company", "Company", "EntityType", "doc")

    def llm(prompt, *, schema=None):
        return {"canonical": None}  # AI is genuinely new

    ents = [{"name": "Anthropic", "label": "Firm",
             "facets": [{"name": "AI", "axis": "Industry"}], "attrs": {}}]
    # Seed the Firm->Company alias so structural folds deterministically.
    store.record_type("g", "entity", "Firm", "Company", "EntityType", "doc")

    extract_fold.fold_labels(store, "g", ents, [], "doc", llm=llm)
    assert ents[0]["label"] == "Company"
    assert ents[0]["labels"] == ["Company", "AI"]
    assert ents[0]["label_raw"] == ["Firm", "AI"]
    # AI registered on the Industry axis.
    assert store.rows[("g", "entity", "AI")] == ("AI", "Industry")


def test_facet_folds_to_existing_canonical():
    store = FakeStore()
    store.record_type("g", "entity", "Company", "Company", "EntityType", "doc")
    store.record_type("g", "entity", "AI", "AI", "Industry", "doc")
    store.record_type("g", "entity", "Company", "Company", "EntityType", "doc")

    def llm(prompt, *, schema=None):
        return {"canonical": "AI"}  # "Artificial Intelligence" ~ "AI"

    ents = [{"name": "X", "label": "Company",
             "facets": [{"name": "Artificial Intelligence", "axis": "Industry"}], "attrs": {}}]
    extract_fold.fold_labels(store, "g", ents, [], "doc", llm=llm)
    assert ents[0]["labels"] == ["Company", "AI"]


def test_no_facets_still_builds_singleton_vector():
    store = FakeStore()
    store.record_type("g", "entity", "Person", "Person", "EntityType", "doc")
    ents = [{"name": "Bob", "label": "Person", "facets": [], "attrs": {}}]
    extract_fold.fold_labels(store, "g", ents, [], "doc", llm=_no_llm)
    assert ents[0]["labels"] == ["Person"]
    assert ents[0]["label_raw"] == ["Person"]
