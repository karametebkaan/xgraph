# Label-key axes S1+S2 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** (S2) make the LLM assign an `axis` to each extracted relation (e.g. `SON_OF`→`FAMILY`), folded/recorded under that axis instead of the fixed `RelationType`; (S1) surface label-key/axis data — Kinetica's `<graph>_label_keys` table in Storage, and node+edge axes in the Ontology panel via `/schema`.

**Architecture:** Backend — extend the extraction relation schema + prompt (`extract.py`), fold relations under their LLM axis (`extract_fold.py`), expose `rel_axes` from `/schema` (`app.py`), add the label_keys table to Kinetica `storage()`. Frontend — render an Axes section in `OntologyViewer`. No graph-build change (edges keep scalar `LABEL`; the `EDGE_LABEL_KEY` column is S4).

**Tech Stack:** FastAPI + injectable LLM (`llm.py`), embedded DuckDB ontology store; React 18 UMD frontend (esbuild JSX check; browser-verified).

## Global Constraints
- **No `git commit` unless authorized**; background job commits on the worktree branch, fast-forwarded to `main`.
- **`axis` is optional end-to-end** — a missing axis falls back to `RelationType` (today's behavior); extraction must never fail on it.
- **No graph-build/adapter ingest change** — edges still MERGE with scalar `LABEL`; the axis lives in the ontology store only (S4 adds the graph column).
- Backend tests from `backend/` (`./.venv/bin/python -m pytest tests/ -v`); LLM-dependent paths use an injected fake `llm`. Kinetica live tests SKIP when down. Baseline 356 passed / 44 skipped.
- Frontend esbuild JSX check must print `ESBUILD_OK`; gateway `curl` 200.
- **Version:** bump `EXPLORER_VERSION` `0.13.0` → `0.14.0` in the frontend task.
- Commit messages: concise 1–2 lines, no `Co-Authored-By` footer.

### esbuild JSX check
```bash
cd /home/kkaramete/xgraph/frontend
end=$(grep -n '</script>' XGraph.html | tail -1 | cut -d: -f1)
sed -n "47,$((end-1))p" XGraph.html | ./node_modules/.bin/esbuild --loader=jsx > /dev/null && echo ESBUILD_OK || echo ESBUILD_FAIL
```

---

## Task 1 (S2): relation `axis` in extraction schema/prompt/shape + fold under it

**Files:** `backend/xgraph_gateway/extract.py`, `backend/xgraph_gateway/extract_fold.py`, `backend/tests/test_extract_fold.py` (or a new `test_edge_axes.py`).

**Interfaces:**
- Produces: extracted relations carry an optional `axis`; `fold_labels` records each relation type under `r.get("axis") or _RELATION_AXIS` via `store.record_type(graph, "relation", name, canonical, axis, uri)`.
- Consumes: existing injectable `llm`, `store.record_type`/`axis_map`.

**Context:** Relations currently get only `{source,target,label}` and fold under the fixed `_RELATION_AXIS="RelationType"`. Mirror the entity-facet `{name,axis}` pattern.

- [ ] **Step 1: Write failing tests**

Create/extend `backend/tests/test_edge_axes.py`:
```python
from xgraph_gateway import extract_fold
from xgraph_gateway.compute.duckdb_engine import DuckDBComputeEngine


def _store(tmp_path):
    return DuckDBComputeEngine(meta_path=str(tmp_path / "m.duckdb"))


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
```

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_edge_axes.py -v` → FAIL (fold uses fixed axis).

- [ ] **Step 2: Fold relations under their axis**

In `extract_fold.py`, the relation loop (≈143-146):
```python
    for r in relations:
        r["label"] = _resolve_one(store, graph, "relation",
                                  r.get("label", ""), _RELATION_AXIS, llm, cache, report,
                                  source_uri, pre_canon)
```
Change the axis arg to `r.get("axis") or _RELATION_AXIS`:
```python
    for r in relations:
        r["label"] = _resolve_one(store, graph, "relation",
                                  r.get("label", ""), (r.get("axis") or _RELATION_AXIS), llm, cache, report,
                                  source_uri, pre_canon)
```

Run the tests → PASS.

- [ ] **Step 3: Add `axis` to the relation schema + prompt + shape in `extract.py`**

In `_EXTRACT_SCHEMA` relations `properties` (≈134-137), add after `label`:
```python
                    "axis": {"type": "string", "description": "Coarse category grouping this relationship label, UPPER_SNAKE, e.g. FAMILY (SON_OF/WIFE_OF), EMPLOYMENT (WORKS_FOR), LOCATION."},
```
(Leave `"required": ["source", "target", "label"]` — axis optional.)

In `_prompt` (≈158), extend the relation instruction:
```python
        "- Give each relationship an UPPER_SNAKE `label` (e.g. WORKS_AT, LOCATED_IN, "
        "SON_OF) AND an `axis` — a coarse UPPER_SNAKE category grouping related labels "
        "(FAMILY for SON_OF/WIFE_OF, EMPLOYMENT for WORKS_FOR, LOCATION for LOCATED_IN).\n"
```
(match the surrounding prompt string style/spacing).

In the relation shape build (≈312):
```python
                relations[rid] = {"id": rid, "src": src_id, "dst": dst_id, "label": label,
                                   "attrs": dict(attrs)}
```
add `axis` (read from the LLM relation dict, e.g. `rel.get("axis")`):
```python
                relations[rid] = {"id": rid, "src": src_id, "dst": dst_id, "label": label,
                                   "axis": axis, "attrs": dict(attrs)}
```
where `axis` is parsed alongside `label` in the relations loop (add `axis = (rel.get("axis") or "").strip() or None` next to the existing `label = ...` parse). Read the loop first and match its variable names.

- [ ] **Step 4: Add an extract-level test (fake llm returns axis)**

Append to `test_edge_axes.py` a test using `extract.extract_document(text, llm=fake)` where the fake returns a relation with `axis`, asserting the returned relation carries `axis`. (Match `extract_document`'s llm contract — a callable returning the schema dict.)

Run all: `./.venv/bin/python -m pytest tests/test_edge_axes.py -v` → PASS.

- [ ] **Step 5: Commit**
```bash
git add backend/xgraph_gateway/extract.py backend/xgraph_gateway/extract_fold.py backend/tests/test_edge_axes.py
git commit -m "feat(extract): LLM assigns an axis per relation; fold records edge labels under it (not fixed RelationType)"
```

---

## Task 2 (S1): `/schema` `rel_axes` + Kinetica `storage()` label_keys table

**Files:** `backend/xgraph_gateway/app.py`, `backend/xgraph_gateway/adapters/kinetica_adapter.py`, tests.

**Interfaces:**
- Produces: `/schema` response gains `rel_axes` (relation types grouped by axis, like `axes` for entities); Kinetica `storage()` includes the `<graph>_label_keys` table.
- Consumes: `axis_map(graph, "relation")`, `label_keys_table_name(graph)`.

- [ ] **Step 1: Failing test — `/schema` returns `rel_axes`**

In `backend/tests/test_edge_axes.py`, add a gateway test: build a `create_app` with a real `SessionStore`/compute, record a relation axis via `fold_labels`, then GET `/schema` and assert `rel_axes` groups the relation under its axis. (FakeAdapter.get_schema returns `rel_types: ["performed"]`; seed the ontology so `performed`→some axis; or assert `rel_axes` shape exists.) Keep it minimal: assert `"rel_axes" in body`.

- [ ] **Step 2: Add `rel_axes` to `/schema`**

In `app.py` `schema(...)` (≈190-206), after the existing entity-axis block that sets `result["axes"]`, add the relation equivalent:
```python
                rmap = _resolve_compute(session).axis_map(graph, "relation")
                if rmap:
                    rel_axes = {}
                    for rt in result.get("rel_types", []):
                        rel_axes.setdefault(rmap.get(rt, "RelationType"), []).append(rt)
                    result["rel_axes"] = rel_axes
```
(mirror the `axes` block; guard the same way it does.)

- [ ] **Step 3: Add the label_keys table to Kinetica `storage()`**

In `kinetica_adapter.py` `storage()` (≈1191-1220), where it builds the candidate table list (≈1203, currently `[node_table_name(graph), edge_table_name(graph)]`), append the label_keys table:
```python
        candidate_tables = [node_table_name(graph), edge_table_name(graph),
                            label_keys_table_name(graph)]
```
(match the actual local variable name found in the read; `label_keys_table_name` is module-level in the same file.)

- [ ] **Step 4: Run backend suite**

```bash
cd /home/kkaramete/xgraph/backend
./.venv/bin/python -m pytest tests/test_edge_axes.py -v
./.venv/bin/python -m pytest tests/ -q
```
Expected: new tests PASS; full suite green (Kinetica live tests SKIP if down).

- [ ] **Step 5: Commit**
```bash
git add backend/xgraph_gateway/app.py backend/xgraph_gateway/adapters/kinetica_adapter.py backend/tests/test_edge_axes.py
git commit -m "feat(build): /schema exposes rel_axes; Kinetica storage() includes the label_keys table (S1)"
```

---

## Task 3 (S1): Ontology panel Axes section + version bump

**Files:** `frontend/XGraph.html` (`OntologyViewer`), `EXPLORER_VERSION`.

**Interfaces:** Consumes `schema.axes` (node) + `schema.rel_axes` (edge) from the `/schema` response the Ontology panel already fetches.

**Context:** `OntologyViewer` renders the DOT graph + label distributions but ignores the `axes` field. Add an Axes section showing `axis → [labels]` for both node and edge axes; degrade to nothing when absent.

- [ ] **Step 1: Find how OntologyViewer gets the schema**

```bash
cd /home/kkaramete/xgraph/frontend
grep -n "function OntologyViewer\|labelData\|\.axes\|rel_axes\|getSchema" XGraph.html | head
```
Identify the prop/state holding the `/schema` result (likely `labelData` or a schema object) and where its sections render.

- [ ] **Step 2: Add the Axes section**

In `OntologyViewer`'s render, near the label/edge distribution sections, add (guarded):
```jsx
{(function(){
    var nodeAxes = (labelData && labelData.axes) || {};
    var edgeAxes = (labelData && labelData.rel_axes) || {};
    var hasAny = Object.keys(nodeAxes).length || Object.keys(edgeAxes).length;
    if (!hasAny) return null;
    function axisBlock(title, m) {
        var keys = Object.keys(m);
        if (!keys.length) return null;
        return (
            <div style={{ marginBottom:10 }}>
                <div style={{ fontSize:11, fontWeight:700, color:'#636e72', textTransform:'uppercase', letterSpacing:0.5, marginBottom:4 }}>{title}</div>
                {keys.map(function(ax){ return (
                    <div key={ax} style={{ fontSize:12, margin:'2px 0' }}>
                        <span style={{ fontWeight:700, color:'#0984e3' }}>{ax}</span>
                        <span style={{ color:'#636e72' }}>{' — ' + (m[ax] || []).join(', ')}</span>
                    </div>
                ); })}
            </div>
        );
    }
    return (
        <div style={{ border:'1px solid #eef2f7', background:'#f7f9fb', borderRadius:8, padding:'10px 12px', margin:'12px 0' }}>
            <div style={{ fontSize:13, fontWeight:800, color:'#2d3436', marginBottom:6 }}>Axes (label keys)</div>
            {axisBlock('Node label axes', nodeAxes)}
            {axisBlock('Edge (relationship) axes', edgeAxes)}
        </div>
    );
})()}
```
(bind `labelData` to whatever the read in Step 1 shows the schema is stored as.)

- [ ] **Step 3: Bump version** `EXPLORER_VERSION` `0.13.0` → `0.14.0`.

- [ ] **Step 4: esbuild + gateway 200**
```bash
cd /home/kkaramete/xgraph/frontend
end=$(grep -n '</script>' XGraph.html | tail -1 | cut -d: -f1)
sed -n "47,$((end-1))p" XGraph.html | ./node_modules/.bin/esbuild --loader=jsx > /dev/null && echo ESBUILD_OK || echo ESBUILD_FAIL
cd /home/kkaramete/xgraph && (./xgraph status >/dev/null 2>&1 || ./xgraph start) && sleep 1 && curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8090/
```
Expected: `ESBUILD_OK` then `200`.

- [ ] **Step 5: Commit**
```bash
git add frontend/XGraph.html
git commit -m "feat(ontology): show node + edge axes (label keys) in the Ontology panel; v0.14.0"
```

---

## Manual (browser) acceptance
Reload → v0.14.0. Extract *"Kaan's son Tan works for Blomberg"* into a graph → **Ontology** shows edge axes `FAMILY — SON_OF`, `EMPLOYMENT — WORKS_FOR` (and node axes). For a Kinetica graph, **Storage** now also previews the `<graph>_label_keys` table.

## Self-Review
- **Spec coverage:** S2 edge axis (LLM-assigned, folded under it — Task 1); S1 `/schema rel_axes` + Kinetica storage label_keys (Task 2) + Ontology Axes UI (Task 3). No graph-build change (edges keep scalar LABEL; EDGE_LABEL_KEY is S4). Matches the design.
- **Placeholder scan:** the two "match the read" notes (extract loop var names, OntologyViewer schema var) are explicit read-first instructions, not TODOs; all code shown.
- **Type/name consistency:** relation `axis` produced in `extract.py`, consumed by `fold_labels` (`r.get("axis")`), recorded via `record_type(...,"relation",...,axis,...)`, read by `axis_map(graph,"relation")` → `/schema rel_axes` → `labelData.rel_axes` in the UI.
- **Scope:** S1+S2 only. S3 (deduced relations) + S4 (edge_label_key graph column/table) deferred.
- **Risk:** additive + optional (`axis` falls back to RelationType); no ingest/DDL change, so no graph rebuild risk. Frontend is display-only.
