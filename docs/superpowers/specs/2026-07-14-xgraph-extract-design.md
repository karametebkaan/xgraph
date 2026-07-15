# xGraph — Extract: text → entities/relationships → graph

**Goal:** A new **Extract** action that reads a PDF or text file, uses the LLM to extract entities and
relationships (open-ended ontology), and MERGEs them into a named graph on the session's graph engine
(FalkorDB or Kinetica). Mirrors the `kgr` extraction approach but is engine-neutral and self-contained
(no dependency on the graphrag repo).

## Decisions (approved 2026-07-14)
1. **Both engines in v1** — FalkorDB (Cypher MERGE) and Kinetica (table upsert + CREATE GRAPH).
2. **Open-ended ontology** — the LLM discovers entity/relationship labels per document (like kgr). No fixed schema.
3. **Accumulate / MERGE** — Extract merges into the named graph; re-running or adding documents accumulates one graph, deduping by entity id.

Out of scope for v1 (deferred): RSS/feed + web-article ingestion (feedparser/trafilatura), coreference/embedding entity-resolution, ontology folding/axes (kgr's synonym-collapsing machinery).

## Architecture

Everything runs server-side (LLM + PDF parsing). One new backend module, one endpoint, one adapter
method (two engine impls), one frontend action.

### Extraction — `xgraph_gateway/extract.py`
- `read_document(filename, data: bytes) -> str` — `.pdf` via **pypdf** (`PdfReader`, concatenate page text); `.txt/.md/.markdown` via UTF-8 decode; else error. New dependency: `pypdf`.
- `canonical_id(name: str) -> str` — deterministic id from a name: `slug(name.lower())[:48] + '-' + sha1(name.lower())[:8]`. Same real-world name → same id (the dedupe key). Mirrors kgr `concept_id`.
- `chunk(text: str) -> list[str]` — split on blank lines (`\n\s*\n+`), drop empties (kgr's paragraph model). Cap the number of chunks processed at `EXTRACT_MAX_CHUNKS` (default 40) and `log`/report if truncated (no silent cap).
- `extract_document(text, hint=None, llm=None) -> {"entities": [...], "relations": [...]}`:
  - Per chunk, one `_llm(prompt, schema=_EXTRACT_SCHEMA)` call (local `.llm._llm`; injectable `llm` for tests).
  - `_EXTRACT_SCHEMA` forces: `{ entities: [{name, label, attrs?}], relations: [{source, target, label, attrs?}] }` (strict, `additionalProperties:false`). Prompt instructs: use concise Title-Case entity labels (Person, Organization, Location…), UPPER_SNAKE relationship labels (WORKS_AT, LOCATED_IN…), reuse the same entity name spelling for the same real-world thing; `relations.source`/`target` must equal an entity `name` in the same chunk; incorporate the optional `hint` as focus.
  - Merge across chunks: assign `id = canonical_id(name)`; dedupe entities by id (first non-empty label/name wins, attrs shallow-merged); relations map source/target names → ids, drop relations whose endpoints aren't in the entity set, dedupe by `(src,dst,label)`.
  - Return normalized: `entities: [{id, label, name, attrs}]`, `relations: [{id, src, dst, label, attrs}]` where relation `id = sha1(src|dst|label)[:16]`.
- Guardrails: labels pass `graph_loader.mapper.safe_ident` before any interpolation into Cypher/DDL; values are parameters/escaped literals.

### Ingestion — `GraphEngineAdapter.ingest_elements(graph, nodes, edges) -> {"nodes": int, "edges": int, "labels": {...}}`
Add to the ABC (`adapters/base.py`). Both impls MERGE (idempotent / accumulating).

- **FalkorDBAdapter.ingest_elements** (`falkordb_adapter.py`): group nodes by label, one UNWIND MERGE per label L (L via `safe_ident`):
  ```
  UNWIND $rows AS r MERGE (n:Entity {NODE:r.id}) SET n:`L`, n.LABEL=$label, n.name=r.name, n += r.attrs
  ```
  Group edges by label, one UNWIND per type T (`safe_ident`):
  ```
  UNWIND $rows AS e MATCH (a:Entity {NODE:e.src}),(b:Entity {NODE:e.dst})
  MERGE (a)-[x:`T` {ID:e.id}]->(b) SET x.LABEL=$label, x += e.attrs
  ```
  Uses the connected graph via the adapter (create graph if new). Follows the project's `:Entity(NODE)` + `LABEL` conventions so Visualize / Ontology / Query work unchanged. Returns created counts from query stats.
- **KineticaAdapter.ingest_elements** (`kinetica_adapter.py`): mirror kgr's model, simplified (single LABEL string, no ontology tables):
  - Ensure schema + node table `<graph>_nodes(NODE VARCHAR PK, LABEL VARCHAR, name VARCHAR)` and edge table `<graph>_edges(edge_key VARCHAR PK, NODE1 VARCHAR, NODE2 VARCHAR, LABEL VARCHAR)` (create if absent).
  - Upsert rows via `db.insert_records_json(payload, table, options={"update_on_existing_pk":"true"})` (accumulate/dedupe by PK).
  - `CREATE OR REPLACE DIRECTED GRAPH "<graph>" (NODES => INPUT_TABLES((SELECT NODE, LABEL FROM <nodes>)), EDGES => INPUT_TABLES((SELECT NODE1, NODE2, LABEL FROM <edges>)), OPTIONS => KV_PAIRS(save_persist='true'))`.
  - Graph name via `safe_ident` on each dotted part; all data values as JSON payload (not interpolated).

### Endpoint — `POST /extract`
- Multipart: `file` (UploadFile, optional) OR form field `text` (optional), `graph` (name), `hint` (optional), `session`, `engine`.
- Flow: `text = read_document(file.filename, await file.read())` if file else `text` field → `extract.extract_document(text, hint)` → `adapter.ingest_elements(graph, entities, relations)` → return `{graph, entities: N, relations: M, labels: {node_labels:[...], edge_labels:[...]}, truncated: bool}`.
- Errors via the existing `_err` envelope. Resolve adapter via `_resolve_adapter(session, engine)`.
- `gateway.js`: `extract(graph, file|text, hint)` → multipart POST (adds `session`). If no file, send `text`.

### Frontend — new **Extract** action (`XGraph.html`)
- Add `extract` to the `ACTIONS` array after `create`; reachable when connected.
- `ExtractPanel`: file input (`.pdf,.txt,.md`) or a paste-text `<textarea>`; target graph name (default e.g. `extracted_graph`); optional focus/hint input; **Extract & Build** button → calls `gwClient.extract(...)` → shows a spinner (extraction can take a while), then a result card: entity/relationship counts, the discovered node/edge labels (chips), a "truncated" note if applicable, and buttons to jump to **Visualize** / **Ontology** on the new graph (set `activeGraph`, refresh graph list). Inline error on failure. Keep the file's inline-style vocabulary and xGraph branding.

## Testing
- `extract.py` unit tests (fake `llm`): `canonical_id` stability; `chunk` paragraph split + cap; `extract_document` merges/dedupes entities across chunks, maps relation names→ids, drops dangling relations, dedupes relations; PDF read tested with a tiny generated PDF (skip if pypdf missing) and a text read.
- `ingest_elements`: FalkorDB live test (SKIP if down) — ingest a small nodes/edges set, assert counts + that a follow-up query returns them; a unit test for the group-by-label Cypher builder (pure, no DB). Kinetica: unit-test the row/DDL builders (pure); live path SKIPs if Kinetica down.
- `/extract` endpoint: TestClient with a `FakeAdapter.ingest_elements` + monkeypatched `extract.extract_document` (fake) — multipart file and text-field paths return the expected counts/labels; bad file type → error envelope.
- Full backend suite stays green; new live tests SKIP if services down. Frontend: `@babel/standalone` transpile PASS.

## Self-review
- Text/PDF → entities/relationships via LLM (open-ended) → both engines, accumulate/MERGE → covered. ✓
- Engine-neutral + self-contained (mirrors kgr, imports nothing from graphrag) → `extract.py` uses local `_llm`; `safe_ident` from vendored `graph_loader`. ✓
- New Extract action feeding a new graph per Setup engine → endpoint resolves session adapter. ✓
- No placeholders: signatures, schema shape, Cypher/DDL templates, and dedupe rules are explicit; RSS/web explicitly deferred.
