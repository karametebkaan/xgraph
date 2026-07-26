# Extraction Label Folding + Facets/Axes + Document Bookkeeping — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add engine-neutral extraction label folding (alias→canonical, LLM-assisted), faceted multi-label ontology (labels grouped by axis), and a timestamped document-provenance ledger to xgraph's `/extract` pipeline.

**Architecture:** A metadata store (documents ledger + ontology) lives as new methods on the `ComputeEngine` contract, so it follows the session's selected OLAP engine (DuckDB tables, or Kinetica tables = kgr's schema verbatim). A new engine-neutral module `extract_fold.py` holds the folding + facet-resolution algorithm (ported from kgr `ontology.py`, minus induced-attr columns), depending only on the store interface + an injectable LLM func. `/extract` gains sha256-based idempotency, folding, and facet handling; both graph adapters gain multi-label ingest.

**Tech Stack:** Python 3, FastAPI, DuckDB (embedded, persistent metadata file), FalkorDB (RESP/Cypher, multi-label), Kinetica (gpudb, SQL backing tables + CREATE GRAPH), pytest.

## Global Constraints

- **No `git commit` under `xgraph/`** — this plan's "Commit" steps are LOCAL commits the user has NOT authorized; DO NOT run them unless the user explicitly says to commit. Treat each "Commit" step as "stage mentally / stop for review". (Spec + CLAUDE.md rule.)
- Backend has its own venv: run everything as `cd backend && ./.venv/bin/python -m pytest ...`.
- Self-contained: no imports from `../falkor` / `../graphrag` / `kgr`. Port kgr code by copying, not importing.
- Live tests (FalkorDB/Kinetica) **SKIP** (never fail) when the engine is unreachable — mirror existing `tests/test_extract_ask_live.py` skip pattern.
- DuckDB returns DECIMAL as `Decimal` — not relevant here (metadata is VARCHAR/TIMESTAMP), but never string-interpolate untrusted values into SQL; use parameters.
- Timestamps: `datetime.now(timezone.utc)` in normal backend code (this is not a Workflow script).
- Attribute hydration is unchanged: attrs stay on nodes/edges (FalkorDB props / Kinetica evolved columns). Do NOT add `attr_name`/`attr_sql_type` to the ontology table.

---

## File Structure

**New files:**
- `backend/xgraph_gateway/extract_fold.py` — neutral folding + facet/axis resolution (uses store + LLM).
- `backend/tests/test_metadata_store.py` — ledger + ontology CRUD (embedded DuckDB, no service).
- `backend/tests/test_extract_fold.py` — folding logic with a fake store + fake LLM.
- `backend/tests/test_extract_folding_live.py` — FalkorDB folding + idempotency (SKIP if down).

**Modified files:**
- `backend/xgraph_gateway/config.py` — add `resolve_meta_path()`.
- `backend/xgraph_gateway/compute/duckdb_engine.py` — metadata methods + persistent meta connection.
- `backend/xgraph_gateway/compute/kinetica_engine.py` — metadata methods over Kinetica tables (live).
- `backend/xgraph_gateway/extract.py` — `_EXTRACT_SCHEMA` + `_prompt` + `extract_document` carry facets.
- `backend/xgraph_gateway/adapters/falkordb_adapter.py` — `build_ingest_cypher` multi-label + provenance.
- `backend/xgraph_gateway/adapters/kinetica_adapter.py` — array LABEL, label_raw, provenance, label_keys.
- `backend/xgraph_gateway/adapters/fake.py` — mirror ingest shape for tests.
- `backend/xgraph_gateway/app.py` — wire `/extract`; response fields; optional `/documents`.
- `backend/tests/test_extract_endpoint.py` — extend for folding + ledger response fields.

**Phasing:** Tasks 1–4 = **Slice A** (ledger + single-canonical folding + idempotency), each independently shippable. Tasks 5–11 = **Slice B** (facets/axes). Frontend array-`LABEL` display is deferred to a separate plan.

---

## Task 1: Metadata connection + documents ledger (DuckDB)

**Files:**
- Modify: `backend/xgraph_gateway/config.py`
- Modify: `backend/xgraph_gateway/compute/duckdb_engine.py`
- Test: `backend/tests/test_metadata_store.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `config.resolve_meta_path() -> str`
  - `DuckDBComputeEngine(meta_path: str | None = None)` (new optional ctor arg; default `config.resolve_meta_path()`)
  - `DuckDBComputeEngine.record_document(graph: str, doc_uri: str, sha256: str, source_type: str) -> dict` returning `{"status": "new"|"unchanged"|"updated", "first_ingested_ts": str, "last_ingested_ts": str}` (timestamps ISO strings)
  - `DuckDBComputeEngine.list_documents(graph: str) -> list[dict]`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_metadata_store.py`:

```python
import os
from xgraph_gateway.compute.duckdb_engine import DuckDBComputeEngine


def _engine(tmp_path):
    return DuckDBComputeEngine(meta_path=str(tmp_path / "meta.duckdb"))


def test_record_document_new_then_unchanged_then_updated(tmp_path):
    eng = _engine(tmp_path)

    first = eng.record_document("g1", "doc:a", "sha-1", "text")
    assert first["status"] == "new"
    assert first["first_ingested_ts"] == first["last_ingested_ts"]

    # Same uri + same sha256 => unchanged, first_ingested_ts preserved.
    again = eng.record_document("g1", "doc:a", "sha-1", "text")
    assert again["status"] == "unchanged"
    assert again["first_ingested_ts"] == first["first_ingested_ts"]
    assert again["last_ingested_ts"] >= first["last_ingested_ts"]

    # Same uri + different sha256 => updated.
    changed = eng.record_document("g1", "doc:a", "sha-2", "text")
    assert changed["status"] == "updated"
    assert changed["first_ingested_ts"] == first["first_ingested_ts"]


def test_record_document_is_per_graph(tmp_path):
    eng = _engine(tmp_path)
    eng.record_document("g1", "doc:a", "sha-1", "text")
    other = eng.record_document("g2", "doc:a", "sha-1", "text")
    assert other["status"] == "new"  # different graph => distinct ledger row


def test_list_documents(tmp_path):
    eng = _engine(tmp_path)
    eng.record_document("g1", "doc:a", "sha-1", "file")
    eng.record_document("g1", "doc:b", "sha-2", "text")
    docs = eng.list_documents("g1")
    assert {d["doc_uri"] for d in docs} == {"doc:a", "doc:b"}
    assert all(d["graph"] == "g1" for d in docs)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_metadata_store.py -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'meta_path'` (or `AttributeError: record_document`).

- [ ] **Step 3: Add `resolve_meta_path` to config.py**

Append to `backend/xgraph_gateway/config.py`:

```python
def resolve_meta_path() -> str:
    """Absolute path to the persistent DuckDB metadata database (documents
    ledger + ontology). Override with XGRAPH_META_DB; defaults to
    `<data_dir>/xgraph_meta.duckdb`."""
    override = os.environ.get("XGRAPH_META_DB")
    if override:
        return os.path.abspath(override)
    return os.path.join(load_settings().data_dir, "xgraph_meta.duckdb")
```

- [ ] **Step 4: Add ctor + ledger to DuckDBComputeEngine**

In `backend/xgraph_gateway/compute/duckdb_engine.py`, add imports at top (after existing imports):

```python
import os
from datetime import datetime, timezone
from xgraph_gateway import config
```

Add to `class DuckDBComputeEngine` (near the top of the class body):

```python
    def __init__(self, meta_path: str | None = None):
        self._meta_path = meta_path or config.resolve_meta_path()
        self._meta_ready = False

    def _meta_con(self):
        con = duckdb.connect(self._meta_path)
        if not self._meta_ready:
            con.execute(
                "CREATE TABLE IF NOT EXISTS xgraph_documents ("
                " graph VARCHAR, doc_uri VARCHAR, sha256 VARCHAR,"
                " source_type VARCHAR, first_ingested_ts TIMESTAMP,"
                " last_ingested_ts TIMESTAMP, status VARCHAR,"
                " PRIMARY KEY (graph, doc_uri))")
            con.execute(
                "CREATE TABLE IF NOT EXISTS xgraph_ontology ("
                " graph VARCHAR, type_kind VARCHAR, type_name VARCHAR,"
                " canonical_name VARCHAR, axis VARCHAR,"
                " first_seen_uri VARCHAR, first_seen_ts TIMESTAMP,"
                " PRIMARY KEY (graph, type_kind, type_name))")
            self._meta_ready = True
        return con

    def record_document(self, graph, doc_uri, sha256, source_type):
        now = datetime.now(timezone.utc)
        con = self._meta_con()
        try:
            existing = con.execute(
                "SELECT sha256, first_ingested_ts FROM xgraph_documents"
                " WHERE graph = ? AND doc_uri = ?", [graph, doc_uri]).fetchone()
            if existing is None:
                con.execute(
                    "INSERT INTO xgraph_documents VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [graph, doc_uri, sha256, source_type, now, now, "ingested"])
                status, first_ts = "new", now
            elif existing[0] == sha256:
                con.execute(
                    "UPDATE xgraph_documents SET last_ingested_ts = ?"
                    " WHERE graph = ? AND doc_uri = ?", [now, graph, doc_uri])
                status, first_ts = "unchanged", existing[1]
            else:
                con.execute(
                    "UPDATE xgraph_documents SET sha256 = ?, last_ingested_ts = ?,"
                    " status = ? WHERE graph = ? AND doc_uri = ?",
                    [sha256, now, "ingested", graph, doc_uri])
                status, first_ts = "updated", existing[1]
            return {"status": status,
                    "first_ingested_ts": _iso(first_ts),
                    "last_ingested_ts": _iso(now)}
        finally:
            con.close()

    def list_documents(self, graph):
        con = self._meta_con()
        try:
            cols = ["graph", "doc_uri", "sha256", "source_type",
                    "first_ingested_ts", "last_ingested_ts", "status"]
            rows = con.execute(
                f"SELECT {', '.join(cols)} FROM xgraph_documents WHERE graph = ?",
                [graph]).fetchall()
            return [dict(zip(cols, [_iso(v) if hasattr(v, 'isoformat') else v
                                    for v in r])) for r in rows]
        finally:
            con.close()
```

Add a module-level helper near the top of the file (after imports):

```python
def _iso(ts):
    """DuckDB TIMESTAMP (datetime) or datetime -> ISO string (stable, comparable)."""
    return ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
```

**Note:** `DuckDBComputeEngine` had no `__init__` before; the existing `ComputeEngine = DuckDBComputeEngine` alias and `ComputeEngine()` call in `app.py` still work (meta_path defaults). Verify no other ctor call passes positional args (there are none).

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_metadata_store.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Run the full suite to check for regressions**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_compute.py tests/test_extract_endpoint.py -v`
Expected: PASS (the new `__init__` must not break existing DuckDB usage).

- [ ] **Step 7: Commit (LOCAL ONLY — see Global Constraints; do NOT run without user go-ahead)**

```bash
git add backend/xgraph_gateway/config.py backend/xgraph_gateway/compute/duckdb_engine.py backend/tests/test_metadata_store.py
git commit -m "feat(extract): DuckDB documents ledger with sha256 idempotency"
```

---

## Task 2: Ontology CRUD (DuckDB)

**Files:**
- Modify: `backend/xgraph_gateway/compute/duckdb_engine.py`
- Test: `backend/tests/test_metadata_store.py`

**Interfaces:**
- Consumes: `DuckDBComputeEngine._meta_con()` (Task 1).
- Produces:
  - `record_type(graph, kind, type_name, canonical_name, axis, source_uri) -> None`
  - `resolve_canonical(graph, kind, type_name) -> str | None`
  - `get_canonicals(graph, kind) -> list[str]`
  - `axis_map(graph, kind) -> dict[str, str]`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_metadata_store.py`:

```python
def test_ontology_record_and_resolve(tmp_path):
    eng = _engine(tmp_path)
    # Canonical: type_name == canonical_name.
    eng.record_type("g1", "entity", "Company", "Company", "EntityType", "doc:a")
    # Alias: Firm folds to Company.
    eng.record_type("g1", "entity", "Firm", "Company", "EntityType", "doc:a")

    assert eng.resolve_canonical("g1", "entity", "Company") == "Company"
    assert eng.resolve_canonical("g1", "entity", "Firm") == "Company"
    # Case-insensitive normalized hit.
    assert eng.resolve_canonical("g1", "entity", "company") == "Company"
    # Unknown => None.
    assert eng.resolve_canonical("g1", "entity", "Planet") is None
    # Kind- and graph-scoped.
    assert eng.resolve_canonical("g1", "relation", "Company") is None
    assert eng.resolve_canonical("g2", "entity", "Company") is None


def test_get_canonicals_and_axis_map(tmp_path):
    eng = _engine(tmp_path)
    eng.record_type("g1", "entity", "Company", "Company", "EntityType", "doc:a")
    eng.record_type("g1", "entity", "AI", "AI", "Industry", "doc:a")
    eng.record_type("g1", "entity", "Firm", "Company", "EntityType", "doc:a")

    assert set(eng.get_canonicals("g1", "entity")) == {"Company", "AI"}
    amap = eng.axis_map("g1", "entity")
    assert amap["Company"] == "EntityType"
    assert amap["AI"] == "Industry"
    assert amap["Firm"] == "EntityType"  # aliases resolve to their axis too


def test_record_type_first_seen_wins(tmp_path):
    eng = _engine(tmp_path)
    eng.record_type("g1", "entity", "Company", "Company", "EntityType", "doc:a")
    # A second record for the same (graph,kind,name) must not overwrite.
    eng.record_type("g1", "entity", "Company", "SOMETHING_ELSE", "OtherAxis", "doc:b")
    assert eng.resolve_canonical("g1", "entity", "Company") == "Company"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_metadata_store.py::test_ontology_record_and_resolve -v`
Expected: FAIL — `AttributeError: 'DuckDBComputeEngine' object has no attribute 'record_type'`.

- [ ] **Step 3: Implement ontology CRUD**

Add to `class DuckDBComputeEngine` (after `list_documents`):

```python
    def record_type(self, graph, kind, type_name, canonical_name, axis, source_uri):
        now = datetime.now(timezone.utc)
        con = self._meta_con()
        try:
            # First-seen wins: ON CONFLICT DO NOTHING preserves the original row.
            con.execute(
                "INSERT INTO xgraph_ontology VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (graph, type_kind, type_name) DO NOTHING",
                [graph, kind, type_name, canonical_name, axis, source_uri, now])
        finally:
            con.close()

    def resolve_canonical(self, graph, kind, type_name):
        con = self._meta_con()
        try:
            row = con.execute(
                "SELECT canonical_name FROM xgraph_ontology"
                " WHERE graph = ? AND type_kind = ?"
                " AND (type_name = ? OR lower(type_name) = lower(?)) LIMIT 1",
                [graph, kind, type_name, type_name]).fetchone()
            return row[0] if row else None
        finally:
            con.close()

    def get_canonicals(self, graph, kind):
        con = self._meta_con()
        try:
            rows = con.execute(
                "SELECT DISTINCT canonical_name FROM xgraph_ontology"
                " WHERE graph = ? AND type_kind = ?", [graph, kind]).fetchall()
            return [r[0] for r in rows]
        finally:
            con.close()

    def axis_map(self, graph, kind):
        con = self._meta_con()
        try:
            rows = con.execute(
                "SELECT type_name, axis FROM xgraph_ontology"
                " WHERE graph = ? AND type_kind = ?", [graph, kind]).fetchall()
            return {name: axis for name, axis in rows}
        finally:
            con.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_metadata_store.py -v`
Expected: PASS (6 tests total).

- [ ] **Step 5: Commit (LOCAL ONLY — do NOT run without user go-ahead)**

```bash
git add backend/xgraph_gateway/compute/duckdb_engine.py backend/tests/test_metadata_store.py
git commit -m "feat(extract): DuckDB ontology CRUD (folding aliases + axes)"
```

---

## Task 3: Folding logic (`extract_fold.py`, single canonical)

**Files:**
- Create: `backend/xgraph_gateway/extract_fold.py`
- Test: `backend/tests/test_extract_fold.py`

**Interfaces:**
- Consumes (duck-typed store): `store.resolve_canonical(graph, kind, name)`, `store.get_canonicals(graph, kind)`, `store.record_type(graph, kind, name, canonical, axis, source_uri)` (Task 2).
- Produces:
  - `fold_check_via_llm(kind: str, proposed_name: str, existing_canonicals: list[str], llm) -> str | None`
  - `fold_labels(store, graph: str, entities: list[dict], relations: list[dict], source_uri: str, llm=None) -> list[dict]` — mutates each `entity["label"]` / `relation["label"]` to its canonical; returns a report `[{"kind","from","to","axis"}]`. `entities` use kind `"entity"` (default axis `"EntityType"`), `relations` use kind `"relation"` (default axis `"RelationType"`).

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_extract_fold.py`:

```python
from xgraph_gateway import extract_fold


class FakeStore:
    """In-memory duck-typed metadata store (mirrors DuckDBComputeEngine's
    ontology methods) so folding tests need no DuckDB."""
    def __init__(self):
        self.rows = {}  # (graph, kind, name) -> (canonical, axis)

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_extract_fold.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'xgraph_gateway.extract_fold'`.

- [ ] **Step 3: Implement `extract_fold.py`**

Create `backend/xgraph_gateway/extract_fold.py`:

```python
"""Extraction label folding: rewrite LLM-proposed entity/relation type labels
to their canonical forms, learning aliases as it goes.

Engine-neutral: depends only on a duck-typed metadata store (the ComputeEngine
methods record_type / resolve_canonical / get_canonicals) and an injectable
LLM func `llm(prompt, *, schema=None)`. Ported from kgr `ontology.py`
(resolve_canonical / fold_check_via_llm / fold_proposal), single-canonical
subset; facet handling is layered on in a later task.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

LLMFunc = Callable[..., Any]

_ENTITY_AXIS = "EntityType"
_RELATION_AXIS = "RelationType"

_llm_fn: Optional[LLMFunc] = None


def _get_llm() -> LLMFunc:
    """Lazily bind the local `_llm` (mirrors extract.py) so importing this
    module never requires the `claude` CLI, and tests inject a fake."""
    global _llm_fn
    if _llm_fn is None:
        from .llm import _llm
        _llm_fn = _llm
    return _llm_fn


_FOLD_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["canonical"],
    "properties": {"canonical": {"type": ["string", "null"]}},
}


def fold_check_via_llm(kind, proposed_name, existing_canonicals, llm):
    """Ask the LLM whether `proposed_name` is a synonym of an existing
    canonical. Returns the canonical to fold into, or None. Never raises:
    on any error/absence, returns None (treat as new canonical)."""
    if not existing_canonicals:
        return None
    prompt = (
        f"You decide whether a newly-proposed {kind} type name is a synonym of "
        f"any existing canonical type in the ontology.\n\n"
        f"Proposed {kind} type: {proposed_name}\n"
        f"Existing canonical {kind} types: {', '.join(sorted(existing_canonicals))}\n\n"
        f"If the proposed type is semantically the same as one of the existing "
        f"canonicals, return that canonical's exact name. Otherwise return null.\n"
        f'Reply with only JSON: {{"canonical": "<existing name>"}} or {{"canonical": null}}.'
    )
    try:
        out = llm(prompt, schema=_FOLD_SCHEMA)
        if isinstance(out, str):
            import json
            out = json.loads(out)
        canonical = (out or {}).get("canonical")
        if isinstance(canonical, str) and canonical in existing_canonicals:
            return canonical
    except Exception:
        return None
    return None


def _resolve_one(store, graph, kind, name, axis, llm, cache, report):
    """Resolve a single (kind, name) to a canonical, learning + persisting the
    decision. `cache` dedupes within one call; `report` accumulates folds."""
    name = (name or "").strip()
    if not name:
        return name
    key = (kind, name)
    if key in cache:
        return cache[key]

    canonical = store.resolve_canonical(graph, kind, name)
    if canonical is None:
        existing = store.get_canonicals(graph, kind)
        fold_to = fold_check_via_llm(kind, name, existing, llm)
        if fold_to:
            store.record_type(graph, kind, name, fold_to, axis, "")
            canonical = fold_to
        else:
            store.record_type(graph, kind, name, name, axis, "")
            canonical = name

    if canonical != name:
        report.append({"kind": kind, "from": name, "to": canonical, "axis": axis})
    cache[key] = canonical
    return canonical


def fold_labels(store, graph, entities, relations, source_uri, llm=None):
    """Rewrite each entity/relation `label` to its canonical, in place.
    Returns a report list of the folds applied `[{kind, from, to, axis}]`."""
    llm = llm or _get_llm()
    cache: dict = {}
    report: list = []
    for e in entities:
        e["label"] = _resolve_one(store, graph, "entity",
                                  e.get("label", ""), _ENTITY_AXIS, llm, cache, report)
    for r in relations:
        r["label"] = _resolve_one(store, graph, "relation",
                                  r.get("label", ""), _RELATION_AXIS, llm, cache, report)
    return report
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_extract_fold.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit (LOCAL ONLY — do NOT run without user go-ahead)**

```bash
git add backend/xgraph_gateway/extract_fold.py backend/tests/test_extract_fold.py
git commit -m "feat(extract): engine-neutral label folding (deterministic + LLM)"
```

---

## Task 4: Wire folding + ledger into `/extract`

**Files:**
- Modify: `backend/xgraph_gateway/app.py:158-178` (the `extract_endpoint`)
- Test: `backend/tests/test_extract_endpoint.py`

**Interfaces:**
- Consumes: `_resolve_compute(session)` (returns a `DuckDBComputeEngine`/`KineticaComputeEngine`), `extract_fold.fold_labels`, `store.record_document`.
- Produces: `/extract` response gains `folded: list`, `document: dict`; on `unchanged` returns `reused: True` with zero new counts.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_extract_endpoint.py` (reuse its existing `_patch_extract_document`, `FakeAdapter`, `TestClient` helpers). First, at the top ensure the app uses an isolated meta DB and a compute with metadata methods. Add this fixture + tests:

```python
import os
import pytest
from xgraph_gateway.compute.duckdb_engine import DuckDBComputeEngine


@pytest.fixture
def client_with_store(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from xgraph_gateway.adapters.fake import FakeAdapter
    from xgraph_gateway.app import create_app
    compute = DuckDBComputeEngine(meta_path=str(tmp_path / "meta.duckdb"))
    app = create_app(adapter_factory=lambda e: FakeAdapter(), compute=compute)
    return TestClient(app)


def _patch_fold_identity(monkeypatch):
    """fold_labels that folds 'Firm'->'Company' deterministically, no LLM."""
    from xgraph_gateway import extract_fold

    def fake_fold(store, graph, entities, relations, source_uri, llm=None):
        report = []
        for e in entities:
            if e.get("label") == "Firm":
                e["label"] = "Company"
                report.append({"kind": "entity", "from": "Firm",
                               "to": "Company", "axis": "EntityType"})
        return report
    monkeypatch.setattr(extract_fold, "fold_labels", fake_fold)


def test_extract_returns_document_record_and_folded(client_with_store, monkeypatch):
    from xgraph_gateway import extract

    def fake_extract_document(text, hint=None, **kw):
        return {"entities": [{"id": "acme", "name": "Acme", "label": "Firm", "attrs": {}}],
                "relations": [], "truncated": False}
    monkeypatch.setattr(extract, "extract_document", fake_extract_document)
    _patch_fold_identity(monkeypatch)

    resp = client_with_store.post("/extract", data={"text": "Acme is a firm.",
                                                    "graph": "g1", "engine": "fake"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["document"]["status"] == "new"
    assert body["document"]["reused"] is False
    assert {"kind": "entity", "from": "Firm", "to": "Company",
            "axis": "EntityType"} in body["folded"]


def test_extract_same_text_is_reused(client_with_store, monkeypatch):
    from xgraph_gateway import extract

    def fake_extract_document(text, hint=None, **kw):
        return {"entities": [{"id": "acme", "name": "Acme", "label": "Firm", "attrs": {}}],
                "relations": [], "truncated": False}
    monkeypatch.setattr(extract, "extract_document", fake_extract_document)
    _patch_fold_identity(monkeypatch)

    payload = {"text": "Acme is a firm.", "graph": "g1", "engine": "fake"}
    first = client_with_store.post("/extract", data=payload).json()
    assert first["document"]["reused"] is False
    second = client_with_store.post("/extract", data=payload).json()
    assert second["document"]["reused"] is True
    assert second["document"]["status"] == "unchanged"
    assert second["entities"] == 0 and second["relations"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_extract_endpoint.py -k "document_record or reused" -v`
Expected: FAIL — `KeyError: 'document'` (endpoint doesn't return it yet).

- [ ] **Step 3: Rewrite `extract_endpoint` in app.py**

Replace the body of `extract_endpoint` (`app.py:158-178`). Add `import hashlib` at the top of app.py if not present. New body:

```python
    @app.post("/extract")
    async def extract_endpoint(file: UploadFile = File(None), text: str = Form(None),
                                graph: str = Form(...), hint: str = Form(None),
                                session: str = Form(None), engine: str = Form("")):
        try:
            if file is not None and file.filename:
                content = await file.read()
                doc = extract.read_document(file.filename, content)
                doc_uri, source_type = file.filename, "file"
            else:
                doc = text
                doc_uri, source_type = None, "text"
            if not doc or not doc.strip():
                raise ValueError("extract requires a non-empty file or text")

            sha = hashlib.sha256(doc.encode("utf-8")).hexdigest()
            if doc_uri is None:
                doc_uri = f"text:{sha[:12]}"

            store = _resolve_compute(session)
            record = store.record_document(graph, doc_uri, sha, source_type)
            doc_info = {"doc_uri": doc_uri, "sha256": sha, **record}

            # Idempotent short-circuit: identical bytes already ingested.
            if record["status"] == "unchanged":
                return {"graph": graph, "entities": 0, "relations": 0,
                        "entities_new": 0, "relations_new": 0,
                        "labels": {"node_labels": [], "edge_labels": []},
                        "truncated": False, "folded": [],
                        "document": {**doc_info, "reused": True}}

            res = extract.extract_document(doc, hint)
            folded = extract_fold.fold_labels(store, graph, res["entities"],
                                              res["relations"], doc_uri)
            adapter = _resolve_adapter(session, engine)
            out = adapter.ingest_elements(graph, res["entities"], res["relations"])
            return {"graph": graph, "entities": out["nodes"], "relations": out["edges"],
                    "entities_new": out.get("nodes_created", out["nodes"]),
                    "relations_new": out.get("edges_created", out["edges"]),
                    "labels": out["labels"], "truncated": res["truncated"],
                    "folded": folded,
                    "document": {**doc_info, "reused": False}}
        except Exception as e:
            return _err(engine, e)
```

Add the import near the other gateway imports at the top of `app.py`:

```python
from . import extract_fold
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_extract_endpoint.py -v`
Expected: PASS (existing tests + 2 new). If an existing test asserts an exact response-dict equality, update it to allow the new `folded`/`document` keys.

- [ ] **Step 5: Run full suite**

Run: `cd backend && ./.venv/bin/python -m pytest tests/ -x -q`
Expected: PASS (no regressions). Live tests SKIP.

- [ ] **Step 6: Commit (LOCAL ONLY — do NOT run without user go-ahead)**

```bash
git add backend/xgraph_gateway/app.py backend/tests/test_extract_endpoint.py
git commit -m "feat(extract): /extract folds labels + records document provenance"
```

**END OF SLICE A** — folding + ledger + idempotency work end-to-end on the DuckDB metadata store, verifiable without any graph engine. Stop here for review before Slice B.

---

## Task 5: Extraction proposal carries facets

**Files:**
- Modify: `backend/xgraph_gateway/extract.py` (`_EXTRACT_SCHEMA`, `_prompt`, `extract_document`)
- Test: `backend/tests/test_extract.py`

**Interfaces:**
- Produces: `extract_document(...)` entities now include `facets: list[dict]` where each facet is `{"name": str, "axis": str}` (empty list when none). Existing keys (`id`, `name`, `label`, `attrs`) unchanged.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_extract.py`:

```python
def test_extract_document_carries_facets():
    from xgraph_gateway import extract

    def fake_llm(prompt, *, schema=None):
        return {"entities": [{"name": "Anthropic", "label": "Company",
                              "facets": [{"name": "AI", "axis": "Industry"}],
                              "attrs": {}}],
                "relations": []}

    out = extract.extract_document("Anthropic is an AI company.", llm=fake_llm)
    ent = out["entities"][0]
    assert ent["label"] == "Company"
    assert ent["facets"] == [{"name": "AI", "axis": "Industry"}]


def test_extract_document_defaults_facets_to_empty():
    from xgraph_gateway import extract

    def fake_llm(prompt, *, schema=None):
        return {"entities": [{"name": "Bob", "label": "Person", "attrs": {}}],
                "relations": []}

    out = extract.extract_document("Bob exists.", llm=fake_llm)
    assert out["entities"][0]["facets"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_extract.py -k facets -v`
Expected: FAIL — `KeyError: 'facets'`.

- [ ] **Step 3: Extend the schema, prompt, and merge**

In `extract.py`, add a `facets` property to the entity schema inside `_EXTRACT_SCHEMA["properties"]["entities"]["items"]["properties"]` (after `attrs`):

```python
                    "facets": {
                        "type": "array",
                        "description": "Optional classifying facets, each a {name, axis} pair "
                                       "(e.g. {\"name\":\"AI\",\"axis\":\"Industry\"}). The primary "
                                       "structural type stays in `label`.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "axis": {"type": "string"},
                            },
                            "required": ["name", "axis"],
                            "additionalProperties": False,
                        },
                    },
```

In `_prompt`, add a rule line after the entity-label rule:

```python
        "- Optionally add `facets`: classifying dimensions of an entity beyond its "
        "structural type, each `{name, axis}` (e.g. a Company with "
        "`{\"name\":\"AI\",\"axis\":\"Industry\"}`). Keep the structural type in `label`.\n"
```

And update the closing "Return JSON only ..." sentence to mention `facets?`:

```python
        "Return JSON only (no markdown fences, no commentary) with `entities` "
        "(each `{name, label, facets?, attrs?}`) and `relations` (each "
        "`{source, target, label, attrs?}`).\n\n"
```

In `extract_document`, where an entity is first stored (the `else` branch creating `entities[eid]`), include facets; and in the merge branch keep first-seen facets. Change the entity-creation block:

```python
            facets = e.get("facets") or []
            if eid in entities:
                existing = entities[eid]
                if not existing.get("label"):
                    existing["label"] = label
                if not existing.get("name"):
                    existing["name"] = name
                if not existing.get("facets"):
                    existing["facets"] = facets
                existing["attrs"] = {**attrs, **existing["attrs"]}
            else:
                entities[eid] = {"id": eid, "label": label, "name": name,
                                 "facets": facets, "attrs": dict(attrs)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_extract.py -v`
Expected: PASS (existing + 2 new).

- [ ] **Step 5: Commit (LOCAL ONLY — do NOT run without user go-ahead)**

```bash
git add backend/xgraph_gateway/extract.py backend/tests/test_extract.py
git commit -m "feat(extract): LLM proposes classifying facets per entity"
```

---

## Task 6: Fold facets + build label vector

**Files:**
- Modify: `backend/xgraph_gateway/extract_fold.py`
- Test: `backend/tests/test_extract_fold.py`

**Interfaces:**
- Produces: after `fold_labels`, each entity gains `labels: list[str]` (structural canonical first, then folded facet canonicals, deduped) and `label_raw: list[str]` (pre-fold structural + facet names). `entity["label"]` stays the structural canonical. Facet folds use the facet's own `axis`.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_extract_fold.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_extract_fold.py -k "facet or vector" -v`
Expected: FAIL — `KeyError: 'labels'`.

- [ ] **Step 3: Extend `fold_labels`**

In `extract_fold.py`, replace the entity loop inside `fold_labels` with facet-aware logic:

```python
    for e in entities:
        raw_struct = (e.get("label") or "").strip()
        struct_canon = _resolve_one(store, graph, "entity",
                                    raw_struct, _ENTITY_AXIS, llm, cache, report)
        e["label"] = struct_canon
        labels = [struct_canon] if struct_canon else []
        label_raw = [raw_struct] if raw_struct else []
        for f in e.get("facets") or []:
            f_name = (f.get("name") or "").strip()
            f_axis = (f.get("axis") or _ENTITY_AXIS).strip()
            if not f_name:
                continue
            f_canon = _resolve_one(store, graph, "entity",
                                   f_name, f_axis, llm, cache, report)
            label_raw.append(f_name)
            if f_canon and f_canon not in labels:
                labels.append(f_canon)
        e["labels"] = labels
        e["label_raw"] = label_raw
```

Leave the relation loop unchanged.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_extract_fold.py -v`
Expected: PASS (all folding tests).

- [ ] **Step 5: Commit (LOCAL ONLY — do NOT run without user go-ahead)**

```bash
git add backend/xgraph_gateway/extract_fold.py backend/tests/test_extract_fold.py
git commit -m "feat(extract): fold facets and build multi-label vector + label_raw"
```

---

## Task 7: FalkorDB multi-label ingest + provenance

**Files:**
- Modify: `backend/xgraph_gateway/adapters/falkordb_adapter.py` (`build_ingest_cypher`)
- Test: `backend/tests/test_falkordb_adapter.py` (create if absent; pure builder test, no live FalkorDB)

**Interfaces:**
- Consumes: entities with `labels: list[str]` + `label_raw: list[str]` (Task 6); falls back to `[label]` when `labels` absent (backward compatible).
- Produces: `build_ingest_cypher` emits Cypher setting all labels on the node, storing `LABEL` (array), `label_raw` (array), and `first_seen_ts`/`last_seen_ts`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_falkordb_adapter.py` (or add to an existing builder test file):

```python
from xgraph_gateway.adapters.falkordb_adapter import build_ingest_cypher


def test_build_ingest_cypher_sets_multiple_labels():
    nodes = [{"id": "n1", "name": "Anthropic", "label": "Company",
              "labels": ["Company", "AI"], "label_raw": ["Firm", "AI"], "attrs": {}}]
    stmts = build_ingest_cypher(nodes, [])
    query, params = stmts[0]
    # Both labels applied.
    assert ":Company" in query and ":AI" in query
    # Vector + provenance stored as node properties.
    assert "n.LABEL = $labels" in query or "n.LABEL = r.labels" in query
    assert "label_raw" in query
    assert "first_seen_ts" in query and "last_seen_ts" in query


def test_build_ingest_cypher_falls_back_to_single_label():
    nodes = [{"id": "n1", "name": "Bob", "label": "Person", "attrs": {}}]
    stmts = build_ingest_cypher(nodes, [])
    query, _ = stmts[0]
    assert ":Person" in query
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_falkordb_adapter.py -v`
Expected: FAIL — assertion on `label_raw`/`first_seen_ts` not present.

- [ ] **Step 3: Rewrite the node loop in `build_ingest_cypher`**

The node grouping key becomes the tuple of canonical labels (so nodes sharing the same label vector batch together). Replace the node section of `build_ingest_cypher` with:

```python
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()

    node_groups: dict[tuple, list[dict]] = {}
    for n in _valid_nodes(nodes):
        labels = n.get("labels") or [n.get("label")]
        labels = tuple(safe_ident(l) for l in labels if l)
        node_groups.setdefault(labels, []).append(n)
    for labels, rows in node_groups.items():
        label_clause = "".join(f":{l}" for l in labels)
        query = (
            "UNWIND $rows AS r "
            f"MERGE (n:Entity {{NODE: r.id}}) "
            f"SET n{label_clause}, n.LABEL = r.labels, n.label_raw = r.label_raw, "
            "n.name = r.name, n += r.attrs, n.last_seen_ts = $now "
            "ON CREATE SET n.first_seen_ts = $now"
        )
        payload = [{"id": r["id"], "name": r.get("name"),
                    "labels": list(labels),
                    "label_raw": r.get("label_raw") or list(labels),
                    "attrs": r.get("attrs") or {}} for r in rows]
        statements.append((query, {"rows": payload, "now": now_iso}))
```

**Note on Cypher validity:** FalkorDB requires `ON CREATE SET` to appear directly after the `MERGE` it qualifies, before other `SET` clauses referencing the merged node. If FalkorDB rejects the combined form during Task 10's live test, split into two statements: the `MERGE ... ON CREATE SET n.first_seen_ts=$now` first, then a follow-up `MATCH ... SET` for the rest. Keep the builder returning `[(query, params), ...]`.

For edges, add provenance similarly — replace the edge `SET` line:

```python
        query = (
            "UNWIND $rows AS e "
            "MATCH (a:Entity {NODE: e.src}), (b:Entity {NODE: e.dst}) "
            f"MERGE (a)-[x:{label} {{ID: e.id}}]->(b) "
            f"SET x.LABEL = $label, x += e.attrs, x.last_seen_ts = $now "
            "ON CREATE SET x.first_seen_ts = $now"
        )
```

and add `"now": now_iso` to the edge statement's params dict.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_falkordb_adapter.py -v`
Expected: PASS.

- [ ] **Step 5: Commit (LOCAL ONLY — do NOT run without user go-ahead)**

```bash
git add backend/xgraph_gateway/adapters/falkordb_adapter.py backend/tests/test_falkordb_adapter.py
git commit -m "feat(extract): FalkorDB multi-label ingest + label_raw + provenance ts"
```

---

## Task 8: Kinetica array-LABEL ingest + label_keys grouping (live)

**Files:**
- Modify: `backend/xgraph_gateway/adapters/kinetica_adapter.py` (`create_table_sql`, `node_rows`, `create_graph_sql`, `ingest_elements`)
- Test: `backend/tests/test_kinetica_adapter.py` (pure builder assertions; the live path is exercised in Task 11)

**Interfaces:**
- Consumes: entities with `labels`/`label_raw` (Task 6). This mirrors kgr's proven live model: node `LABEL` is `VARCHAR[]`, grouped by axis via `label_keys` fed into `CREATE GRAPH`.
- Produces: `create_table_sql(table,"node")` declares `LABEL VARCHAR[]` + `label_raw VARCHAR[]` + `first_seen_ts`/`last_seen_ts`; `create_graph_sql` accepts an optional label-keys grouping SELECT.

- [ ] **Step 1: Write the failing test (pure builders only)**

Create `backend/tests/test_kinetica_adapter.py`:

```python
from xgraph_gateway.adapters import kinetica_adapter as ka


def test_node_table_sql_declares_array_label_and_provenance():
    sql = ka.create_table_sql("s.g_nodes", "node")
    assert "LABEL VARCHAR[]" in sql
    assert "label_raw VARCHAR[]" in sql
    assert "first_seen_ts" in sql and "last_seen_ts" in sql


def test_node_rows_emit_label_vector():
    nodes = [{"id": "n1", "name": "Anthropic", "label": "Company",
              "labels": ["Company", "AI"], "label_raw": ["Firm", "AI"], "attrs": {}}]
    rows = ka.node_rows(nodes)
    assert rows[0]["LABEL"] == ["Company", "AI"]
    assert rows[0]["label_raw"] == ["Firm", "AI"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_kinetica_adapter.py -v`
Expected: FAIL — `LABEL VARCHAR[]` not in the DDL (currently `LABEL VARCHAR(256)`).

- [ ] **Step 3: Update the Kinetica builders**

In `kinetica_adapter.py`, change the node branch of `create_table_sql`:

```python
    if kind == "node":
        return (
            f"CREATE TABLE IF NOT EXISTS {table} (\n"
            "    NODE VARCHAR(256, PRIMARY_KEY, SHARD_KEY) NOT NULL,\n"
            "    LABEL VARCHAR[],\n"
            "    label_raw VARCHAR[],\n"
            "    name VARCHAR(1024),\n"
            "    first_seen_ts TIMESTAMP,\n"
            "    last_seen_ts TIMESTAMP\n"
            ")"
        )
```

Update `node_rows` to emit the vector + provenance:

```python
def node_rows(nodes: list[dict]) -> list[dict]:
    """[{id,label,labels?,label_raw?,name,attrs}] -> insert payload dicts with
    a VARCHAR[] LABEL vector (falls back to [label]) and label_raw."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    out = []
    for n in nodes:
        if n.get("id") is None:
            continue
        labels = n.get("labels") or ([n["label"]] if n.get("label") else [])
        out.append({"NODE": n["id"], "LABEL": labels,
                    "label_raw": n.get("label_raw") or labels,
                    "name": n.get("name"),
                    "first_seen_ts": now, "last_seen_ts": now})
    return out
```

Add `label_raw`, `first_seen_ts`, `last_seen_ts` to `_NODE_BASE_COLS` so they are not mistaken for attribute columns:

```python
_NODE_BASE_COLS = {"NODE", "LABEL", "label_raw", "name", _NAME_PROPERTY,
                   "first_seen_ts", "last_seen_ts"}
```

Extend `create_graph_sql` with an optional label-keys grouping SELECT (kgr's NODES grouping). Add a parameter and append the grouping when provided:

```python
def create_graph_sql(graph: str, node_table: str, edge_table: str,
                      node_attr_cols: list[str] | None = None,
                      edge_attr_cols: list[str] | None = None,
                      label_keys_table: str | None = None) -> str:
    graph_ident = ".".join(safe_ident(p) for p in str(graph).split("."))
    node_cols = [safe_ident(c) for c in (node_attr_cols or [])]
    edge_cols = [safe_ident(c) for c in (edge_attr_cols or [])]
    node_select = ", ".join(["NODE", "LABEL", f"name AS {_NAME_PROPERTY}"] + node_cols)
    edge_select = ", ".join(["NODE1", "NODE2", "LABEL"] + edge_cols)
    grouping = ""
    if label_keys_table:
        grouping = (f",\n    NODES => INPUT_TABLES((SELECT label_key AS LABEL_KEY, "
                    f"label AS LABEL FROM {label_keys_table}))")
    return (
        f"CREATE OR REPLACE DIRECTED GRAPH {graph_ident} (\n"
        f"    NODES => INPUT_TABLES((SELECT {node_select} FROM {node_table})){grouping},\n"
        f"    EDGES => INPUT_TABLES((SELECT {edge_select} FROM {edge_table})),\n"
        "    OPTIONS => KV_PAIRS(save_persist = 'true')\n"
        ")"
    )
```

**Note:** the exact `CREATE GRAPH` multi-grouping syntax must match kgr's `graph.sql` — before running live, read `~/github-graph/graph/graphrag/kgr/graph.sql` and mirror its NODES/label_keys grouping form exactly (Kinetica is strict). Adjust the `grouping` string to match. This is the one place to verify against kgr verbatim.

Wire label_keys materialization into `ingest_elements`: after computing the node payload and before `create_graph_sql`, materialize a per-graph label_keys table from the session's ontology store. Since `ingest_elements` is on the adapter (not the compute store), materialize label_keys directly in Kinetica from the distinct labels seen. Add, before the `create_graph_sql` call:

```python
        label_keys_table = self._materialize_label_keys(graph, nodes)
```

and pass `label_keys_table=label_keys_table` to `create_graph_sql(...)`. Implement `_materialize_label_keys` on `KineticaAdapter` (builds a `<graph>_label_keys` table grouping each distinct label under its axis; axis defaults to `EntityType` when unknown). Keep it best-effort and idempotent (`CREATE OR REPLACE TABLE`). Because axis lives in the metadata store (DuckDB or Kinetica), and the adapter may not have it, default all labels to `EntityType` here and refine in Task 9 where the store is reachable.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_kinetica_adapter.py -v`
Expected: PASS (pure builder tests). The live ingest is verified in Task 11.

- [ ] **Step 5: Commit (LOCAL ONLY — do NOT run without user go-ahead)**

```bash
git add backend/xgraph_gateway/adapters/kinetica_adapter.py backend/tests/test_kinetica_adapter.py
git commit -m "feat(extract): Kinetica array-LABEL ingest + label_keys grouping"
```

---

## Task 9: Kinetica metadata store + `get_schema` axis grouping

**Files:**
- Modify: `backend/xgraph_gateway/compute/kinetica_engine.py` (metadata methods)
- Modify: `backend/xgraph_gateway/adapters/fake.py`, `backend/xgraph_gateway/adapters/falkordb_adapter.py`, `backend/xgraph_gateway/adapters/kinetica_adapter.py` (`get_schema` axis grouping)
- Test: `backend/tests/test_metadata_store.py` (Kinetica part SKIPs if down), `backend/tests/test_extract_endpoint.py` (fake schema grouping)

**Interfaces:**
- Produces: `KineticaComputeEngine` implements the same metadata methods as `DuckDBComputeEngine` (Tasks 1–2) over kgr-style Kinetica tables. `get_schema` return dict gains `axes: dict[str, list[str]]` (axis → labels) when an ontology is present.

- [ ] **Step 1: Write the failing tests**

Add a Kinetica-skipping test to `backend/tests/test_metadata_store.py`:

```python
def test_kinetica_metadata_roundtrip():
    import pytest
    try:
        from xgraph_gateway.compute.kinetica_engine import KineticaComputeEngine
        eng = KineticaComputeEngine()
        rec = eng.record_document("wf_meta_test", "doc:a", "sha-1", "text")
    except Exception:
        pytest.skip("Kinetica unreachable")
    assert rec["status"] in {"new", "unchanged", "updated"}
    eng.record_type("wf_meta_test", "entity", "Firm", "Company", "EntityType", "doc:a")
    assert eng.resolve_canonical("wf_meta_test", "entity", "Firm") == "Company"
```

Add a fake schema-axes test to `backend/tests/test_extract_endpoint.py`:

```python
def test_schema_reports_axes(client_with_store):
    resp = client_with_store.get("/schema", params={"graph": "demo_graph", "engine": "fake"})
    assert resp.status_code == 200
    assert "axes" in resp.json()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_extract_endpoint.py -k axes tests/test_metadata_store.py -k kinetica -v`
Expected: fake axes test FAILS (`KeyError: 'axes'`); Kinetica test SKIPs (or FAILs on missing method if Kinetica up).

- [ ] **Step 3: Implement Kinetica metadata methods**

In `kinetica_engine.py`, add to `class KineticaComputeEngine` the same method set as DuckDB, issuing Kinetica SQL against kgr-style tables in a `xgraph_meta` schema. Read `~/github-graph/graph/graphrag/kgr/schema.sql` for the exact `documents`/`ontology` DDL and mirror it (add a `graph` column, drop `attr_*`). Use the engine's existing SQL execution path (`self._src` / `run_sql`). Implement `record_document`, `list_documents`, `record_type`, `resolve_canonical`, `get_canonicals`, `axis_map` with the same signatures/returns as Task 1–2. Timestamps via `datetime.now(timezone.utc)`; return ISO strings through the same `_iso` helper (import or duplicate it).

- [ ] **Step 4: Implement `get_schema` axis grouping**

In `fake.py` `get_schema`, add a static `axes` key:

```python
        return {"labels": ["bank", "wire_message"], "rel_types": ["performed"],
                "dot": 'digraph { "bank" -> "wire_message" [label="performed"]; }',
                "axes": {"EntityType": ["bank", "wire_message"]},
                "counts": {"nodes": len(_NODES), "edges": len(_EDGES)}}
```

In `falkordb_adapter.get_schema` and `kinetica_adapter.get_schema`, add an `axes` grouping derived from labels (group each label under its axis when an ontology is available, else default all to `EntityType`). For FalkorDB, since the adapter has no store handle, default `axes = {"EntityType": labels}` and note that the app can enrich it from the session store later. Return `axes` in both dicts.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_extract_endpoint.py -k axes -v`
Expected: PASS. Kinetica test PASSes if a database is reachable, else SKIPs.

- [ ] **Step 6: Commit (LOCAL ONLY — do NOT run without user go-ahead)**

```bash
git add backend/xgraph_gateway/compute/kinetica_engine.py backend/xgraph_gateway/adapters/ backend/tests/
git commit -m "feat(extract): Kinetica metadata store + get_schema axis grouping"
```

---

## Task 10: Optional `/documents` provenance endpoint

**Files:**
- Modify: `backend/xgraph_gateway/app.py`
- Test: `backend/tests/test_extract_endpoint.py`

**Interfaces:**
- Produces: `GET /documents?graph=&engine=&session=` → `store.list_documents(graph)`.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_extract_endpoint.py`:

```python
def test_documents_endpoint_lists_ledger(client_with_store, monkeypatch):
    from xgraph_gateway import extract, extract_fold

    def fake_extract_document(text, hint=None, **kw):
        return {"entities": [], "relations": [], "truncated": False}
    monkeypatch.setattr(extract, "extract_document", fake_extract_document)
    monkeypatch.setattr(extract_fold, "fold_labels",
                        lambda *a, **k: [])

    client_with_store.post("/extract", data={"text": "hi", "graph": "gL", "engine": "fake"})
    resp = client_with_store.get("/documents", params={"graph": "gL", "engine": "fake"})
    assert resp.status_code == 200
    assert any(d["doc_uri"].startswith("text:") for d in resp.json()["documents"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_extract_endpoint.py -k documents_endpoint -v`
Expected: FAIL — 404 (route missing).

- [ ] **Step 3: Add the route**

In `app.py`, after the `/extract` route:

```python
    @app.get("/documents")
    def documents(graph: str, engine: str = "", session: str | None = None):
        try:
            return {"documents": _resolve_compute(session).list_documents(graph)}
        except Exception as e:
            return _err(engine, e)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_extract_endpoint.py -k documents_endpoint -v`
Expected: PASS.

- [ ] **Step 5: Commit (LOCAL ONLY — do NOT run without user go-ahead)**

```bash
git add backend/xgraph_gateway/app.py backend/tests/test_extract_endpoint.py
git commit -m "feat(extract): GET /documents provenance ledger endpoint"
```

---

## Task 11: Live regression + full verification

**Files:**
- Create: `backend/tests/test_extract_folding_live.py`

**Interfaces:**
- Consumes: everything above, end-to-end against a live FalkorDB (SKIP if down).

- [ ] **Step 1: Write the live regression test**

Create `backend/tests/test_extract_folding_live.py`:

```python
import os
import pytest
from fastapi.testclient import TestClient
from xgraph_gateway.app import create_app
from xgraph_gateway.compute.duckdb_engine import DuckDBComputeEngine


def _client(tmp_path):
    compute = DuckDBComputeEngine(meta_path=str(tmp_path / "meta.duckdb"))
    return TestClient(create_app(compute=compute))


def _falkor_up(client):
    r = client.get("/graphs", params={"engine": "falkordb"})
    return r.status_code == 200 and "error" not in r.json()


def test_folding_and_idempotency_live(tmp_path, monkeypatch):
    client = _client(tmp_path)
    if not _falkor_up(client):
        pytest.skip("FalkorDB unreachable")

    from xgraph_gateway import extract

    # Deterministic proposal: two mentions with synonym labels + a facet.
    def fake_extract_document(text, hint=None, **kw):
        return {"entities": [
                    {"id": "anthropic", "name": "Anthropic", "label": "Company",
                     "facets": [{"name": "AI", "axis": "Industry"}], "attrs": {}},
                    {"id": "google", "name": "Google", "label": "Firm",
                     "facets": [], "attrs": {}}],
                "relations": [], "truncated": False}
    monkeypatch.setattr(extract, "extract_document", fake_extract_document)

    # Seed Firm->Company so folding is deterministic without an LLM.
    client.post("/extract", data={"text": "seed", "graph": "fold_live_test",
                                  "engine": "falkordb"})
    # Manually assert Firm folds to Company via the store after a run below.

    graph = "fold_live_test"
    first = client.post("/extract", data={"text": "doc one about Anthropic and Google.",
                                          "graph": graph, "engine": "falkordb"}).json()
    assert first["document"]["reused"] is False

    # Re-submitting identical bytes is reused (idempotent), zero new nodes.
    again = client.post("/extract", data={"text": "doc one about Anthropic and Google.",
                                          "graph": graph, "engine": "falkordb"}).json()
    assert again["document"]["reused"] is True
    assert again["entities"] == 0

    # Anthropic carries a multi-label vector in the graph.
    q = client.post("/query", json={"engine": "falkordb", "graph": graph,
                                    "cypher": "MATCH (n {NODE:'anthropic'}) RETURN labels(n) AS l"}).json()
    labels = q["rows"][0][0]
    assert "Company" in labels and "AI" in labels

    # cleanup
    client.post("/delete_graph", json={"engine": "falkordb", "graph": graph})
```

- [ ] **Step 2: Run it (expect PASS if FalkorDB up, else SKIP)**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_extract_folding_live.py -v`
Expected: PASS if FalkorDB reachable; SKIP otherwise. If FalkorDB rejects the combined `MERGE ... ON CREATE SET`, apply the split-statement fallback noted in Task 7 Step 3, then re-run.

- [ ] **Step 3: Full suite**

Run: `cd backend && ./.venv/bin/python -m pytest tests/ -q`
Expected: all PASS; live tests SKIP when engines down. Confirm the pre-existing count (was 227) increased by the new tests and none regressed.

- [ ] **Step 4: Manual end-to-end sanity via the running gateway**

```bash
cd /home/kkaramete/xgraph && ./xgraph restart
curl -s -X POST localhost:8090/extract -F 'graph=fold_curl_test' -F 'engine=falkordb' \
  -F 'text=Anthropic is an AI firm. Google is a technology firm.' | python3 -m json.tool
# Expect: "folded" shows Firm->Company (after the ontology learns it), "document.status":"new".
curl -s 'localhost:8090/documents?engine=falkordb&graph=fold_curl_test' | python3 -m json.tool
curl -s -X POST localhost:8090/delete_graph -H 'Content-Type: application/json' \
  -d '{"engine":"falkordb","graph":"fold_curl_test"}'
```

- [ ] **Step 5: Update README HTTP API table + Commit (LOCAL ONLY — do NOT run commit without user go-ahead)**

Add `GET /documents` and the `/extract` `folded`/`document` fields to the README `## HTTP API` request-bodies table (from the earlier README work).

```bash
git add backend/tests/test_extract_folding_live.py README.md
git commit -m "test(extract): live folding + idempotency regression; docs"
```

---

## Self-Review

**Spec coverage:**
- State store follows OLAP engine → Tasks 1–2 (DuckDB), 9 (Kinetica), on the `ComputeEngine`. ✓
- Documents ledger + timestamps + sha256 idempotency → Tasks 1, 4 (reused short-circuit), 11 (live). ✓
- Hybrid folding (deterministic + LLM) → Task 3. ✓
- Facets/axes multi-label + label_raw → Tasks 5, 6, 7 (FalkorDB), 8 (Kinetica). ✓
- Per-graph scoping → `graph` column + all methods keyed by graph (Tasks 1–2 tests assert it). ✓
- Attribute hydration unchanged (no ontology-induced columns) → not modified; `_NODE_BASE_COLS` extended so provenance cols aren't treated as attrs (Task 8). ✓
- `get_schema` axis grouping → Task 9. ✓
- `/extract` response `folded` + `document`; optional `/documents` → Tasks 4, 10. ✓
- Testing: unit (no services) Tasks 1–7,10; live SKIP Tasks 9,11. ✓
- Frontend array-LABEL display → explicitly deferred (spec + File Structure). ✓

**Placeholder scan:** No TBD/TODO. Two "verify against kgr verbatim before live" notes (Task 8 `CREATE GRAPH` grouping, Task 9 DDL) are genuine live-Kinetica correctness anchors, not deferred work — they point at exact files to mirror.

**Type consistency:** `record_document` returns `{status, first_ingested_ts, last_ingested_ts}` (Task 1) — consumed in Task 4 as `record["status"]` and spread into `doc_info`. `fold_labels(store, graph, entities, relations, source_uri, llm=None) -> report` consistent across Tasks 3/4/6. `record_type(graph, kind, type_name, canonical_name, axis, source_uri)` consistent Tasks 2/3/9. Entity vector keys `labels`/`label_raw` consistent Tasks 6/7/8. `create_graph_sql` new `label_keys_table` kwarg added Task 8 with default None (backward compatible with existing callers).
