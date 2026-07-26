# Label-key axes — Slices S1 + S2 design (Storage/Ontology visibility + edge axes in extraction)

**Date:** 2026-07-21
**Status:** Approved design, pre-implementation
**Origin:** xGraph already has a node-label "axis" (label_key) concept — Kinetica materializes a
`<graph>_label_keys` table and the DuckDB ontology store records an `axis` per type — but it's
invisible (Storage omits the table, the Ontology UI ignores axes) and **edges have no axis at all**
(every relation is folded under a fixed `RelationType`). The user wants relation labels to carry a
real axis (e.g. `SON_OF`/`WIFE_OF` → `FAMILY`, `WORKS_FOR` → `EMPLOYMENT`), assigned by the LLM.

This is the first of four slices (S1–S4). **S1** makes the existing label_key/axis data visible;
**S2** makes the LLM assign an axis to each edge and records it. Deduced relations (S3) and the
graph-level `edge_label_key` column/table (S4) are separate follow-ups.

## Decisions (locked during brainstorming)

1. **Edge axis is LLM-assigned**, mirroring entity facets: the relation schema gains an `axis`
   field; `fold_labels` records each relation type under that axis (not the fixed `RelationType`).
2. **Build S1 + S2 now**, then S3, then S4.
3. **S2 is extraction/ontology only** — edges still carry a scalar `LABEL` in the built graph; the
   `EDGE_LABEL_KEY` column + `<graph>_edge_label_keys` table is **S4**. S2's outcome is that the
   ontology + Ontology UI show edge axes, and future S4 build has the data to use.

## S1 — Storage + Ontology label_key visibility (read-side only)

### Backend
- **Kinetica `storage()`** (`kinetica_adapter.py`): add `label_keys_table_name(graph)` to the
  candidate tables it previews, so the existing `<graph>_label_keys` table (axis → node-label array)
  shows up alongside the node/edge tables. No new table — just report the one that already exists.
- **`/schema`** already returns `axes` (entity/node labels grouped by axis, from the DuckDB
  ontology `axis_map`). Extend it to also return **`rel_axes`** — relation types grouped by axis
  (`axis_map(graph, "relation")`) — so the UI can show edge axes (populated for real by S2; today
  they'd all read `RelationType`).

### Frontend
- **Ontology panel** (`OntologyViewer`): render an **Axes** section — node axes (`schema.axes`) and
  edge axes (`schema.rel_axes`) as `axis → [labels]` chips. Today the `axes` field is returned but
  unused; this surfaces it. Degrades gracefully when absent (FalkorDB with no ontology store rows
  simply shows no axes).

## S2 — Edge axes in extraction (LLM-assigned)

### Backend
- **`extract.py`**: add an optional `axis` (UPPER_SNAKE category) to the **relation** entries of
  `_EXTRACT_SCHEMA`, and instruct it in `_prompt` ("give each relationship an `axis` — a coarse
  category like FAMILY, EMPLOYMENT, LOCATION — grouping specific labels like SON_OF/WIFE_OF under
  FAMILY"). The relation shape becomes `{id, src, dst, label, axis, attrs}` (axis optional; falls
  back when the model omits it).
- **`extract_fold.py`** (`fold_labels`): for each relation, fold `r["label"]` under
  `r.get("axis") or _RELATION_AXIS` instead of the fixed `_RELATION_AXIS` — exactly how entity
  facets already use `f.get("axis") or _ENTITY_AXIS`. This records the relation type in the ontology
  store under its LLM-assigned axis via the existing `store.record_type(graph, "relation", name,
  canonical, axis, source_uri)`. First-seen-wins (an axis is fixed once recorded).
- No adapter/graph-build change: edges still MERGE with a scalar `LABEL`. The axis lives in the
  ontology store and flows to `/schema` `rel_axes` (S1) → Ontology UI.

### Data flow (worked example)
Extract *"Kaan's son Tan works for Blomberg"* →
- entities: Kaan, Tan (person), Blomberg (org);
- relations: `SON_OF` (Tan→Kaan, axis `FAMILY`), `WORKS_FOR` (Tan→Blomberg, axis `EMPLOYMENT`).
- `fold_labels` records `relation SON_OF` under `FAMILY`, `relation WORKS_FOR` under `EMPLOYMENT`.
- `/schema` `rel_axes` → `{FAMILY: [SON_OF], EMPLOYMENT: [WORKS_FOR]}`; the Ontology panel shows them.
(The deduced `FATHER_OF`/`MOTHER_OF` edges are **S3**; the edge_label_key graph column is **S4**.)

## Error handling
- `axis` is optional end-to-end: a missing/empty axis falls back to `RelationType` (today's
  behavior), so extraction never fails on it. Storage/Ontology additions degrade to empty when the
  table/ontology rows are absent (FalkorDB, pre-existing graphs).

## Testing
- **Backend (unit, no LLM):** `fold_labels` records a relation under its supplied `axis` (inject a
  fake `llm`/store); `record_type`/`axis_map(graph, "relation")` round-trip; `/schema` returns
  `rel_axes`. Kinetica `storage()` includes the label_keys table (live-skip) + a FakeAdapter unit for
  the endpoint shape. `extract_document` relation schema accepts+passes `axis` (fake llm returning an
  axis).
- **Frontend:** Ontology axes section is browser-verified (React not headless); esbuild gate + `curl`
  200.

## Files (indicative)
- **Backend:** `extract.py` (relation `axis` in schema+prompt+shape), `extract_fold.py` (fold under
  relation axis), `app.py` (`/schema` `rel_axes`), `kinetica_adapter.py` (`storage()` +label_keys),
  tests.
- **Frontend:** `XGraph.html` `OntologyViewer` (Axes section), version bump.

## Deferred (later slices)
- **S3:** LLM deduced relations (FATHER_OF/MOTHER_OF from son+wife) under FAMILY, tagged `deduced`.
- **S4:** `EDGE_LABEL_KEY` column + `<graph>_edge_label_keys` table in Kinetica CREATE GRAPH (mirror
  the node label_keys machinery), FalkorDB edge axis property, and Storage showing the edge
  label_key table.
