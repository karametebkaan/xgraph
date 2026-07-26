# Store & show extracted source text — design

**Date:** 2026-07-26
**Status:** Approved design, pre-implementation
**Origin:** For graphs built by extracting from text/PDF, the Storage panel should show the **entire
source text(s)** used to create the graph — both text **read from a file** and text **input/pasted
(appended)** — not just a hash. Today Storage's "Provenance — documents" list shows only
`doc_uri` (for pasted text that is literally `text:<first-12-hex-of-sha256>`, the "hash number" the
user sees) plus status/timestamp. The full text is discarded after extraction.

## Problem

The extraction path (`POST /extract`) reads or accepts a document, computes `sha256`, extracts
entities/relations, ingests them, and records a **light provenance ledger row** in the DuckDB meta
store table `xgraph_documents` (`graph, doc_uri, sha256, source_type, first_ingested_ts,
last_ingested_ts, status`). **No column holds the document text**, and the in-memory `doc` string is
dropped once extraction finishes. So the source text cannot be shown, and for already-built graphs it
is **unrecoverable** (the bytes are gone).

This is the natural next step on top of the 2026-07-25 per-graph provenance spec
(`2026-07-25-storage-per-graph-provenance-design.md`), which surfaced *which* documents built a
graph; this spec adds *the text itself*.

## Decisions (locked during brainstorming)

1. **Persist the text going forward** — capture the full document text at extract time. Existing
   graphs have no stored text (bytes are gone); they are backfilled opportunistically when the same
   document is re-submitted (see reuse-path backfill).
2. **Separate table, fetched on-demand** — store text in a new `xgraph_document_texts` table keyed by
   `(graph, doc_uri)`, NOT as a column on `xgraph_documents`. This keeps the `/documents` list call
   light (it is polled to render the provenance list) and loads full text only when a row is expanded.
3. **Show within a length cap** — the UI shows up to a display cap (default 20,000 chars) with a
   "showing first N of M characters" note when truncated. A generic **paginated DB-table viewer** and
   file/Kinetica-backed storage for very large texts are explicit follow-ups, not this spec.
4. **Both source kinds** — files (`source_type="file"`, `doc_uri`=filename) and pasted/appended text
   (`source_type="text"`, `doc_uri`=`text:<sha[:12]>`) are captured and shown identically.
5. **No new dependency, no commit** — DuckDB meta-store only; spec written to disk, not committed
   (per CLAUDE.md).

## Architecture

### 1. Meta-store text table (`compute/duckdb_engine.py`)

New table created in `_meta_con()` alongside the existing ones (idempotent `CREATE TABLE IF NOT
EXISTS`, so no separate migration is needed — old DBs simply gain the table on next connect):

```sql
CREATE TABLE IF NOT EXISTS xgraph_document_texts (
  graph VARCHAR, doc_uri VARCHAR, text VARCHAR, char_len INTEGER,
  PRIMARY KEY (graph, doc_uri))
```

Methods:

- `record_document_text(graph, doc_uri, text)` — upsert (DELETE then INSERT keyed on
  `(graph, doc_uri)`), storing `text` and `char_len = len(text)`. Idempotent.
- `has_document_text(graph, doc_uri) -> bool` — cheap existence check for the reuse-path backfill
  (avoids re-reading/re-writing text that is already stored).
- `get_document_text(graph, doc_uri, limit=None) -> dict | None` — returns
  `{doc_uri, text, char_len, truncated}` where `text` is sliced to `limit` (full text when
  `limit` is None/≥ length) and `truncated = char_len > len(returned text)`. `None` if no row.
- `clear_graph_metadata(graph)` — add `DELETE FROM xgraph_document_texts WHERE graph = ?` so a
  deleted-then-re-extracted graph does not carry stale text.

### 2. `/extract` endpoint (`app.py`)

`doc` (the full text) is already in hand in both branches:

- **Full-extraction path** (after a successful `ingest_elements` + `record_document`):
  `store.record_document_text(graph, doc_uri, doc)`. Placed after the ingest/ledger commit so a
  failed extraction leaves no text row (mirrors the existing ledger-after-ingest rule).
- **Reuse short-circuit** (identical `sha256` already ingested): backfill only when missing —
  `if not store.has_document_text(graph, doc_uri): store.record_document_text(graph, doc_uri, doc)`.
  This lets a graph extracted before this feature capture its text when the same bytes are
  re-submitted, without doing work on every reuse.

### 3. New endpoint (`app.py`)

```
GET /document_text?graph=&doc_uri=&session=&limit=
    -> store.get_document_text(graph, doc_uri, limit) or {error:...}
```

- `limit` defaults to `20000` (the display cap). Response: `{doc_uri, text, char_len, truncated}`.
- Missing row → uniform error envelope (or `{doc_uri, text:"", char_len:0, truncated:false}`; the
  panel treats an empty/absent text as "text not captured for this document").

### 4. gateway.js client (`frontend/gateway.js`)

Add, next to `documents`/`sourcePreview`:

```js
documentText: function (graph, docUri, limit) {
  var qs = "/document_text?graph=" + encodeURIComponent(graph) +
           "&doc_uri=" + encodeURIComponent(docUri) +
           (limit != null ? "&limit=" + encodeURIComponent(limit) : "");
  return getJSON(q(qs));
},
```

### 5. StoragePanel (`frontend/XGraph.html`)

In the existing "Provenance — documents" section, make each document row **expandable**:

- Row header stays compact: `📄`/`✎` + `doc_uri` + `· status · ingested <ts>`, now clickable
  (cursor pointer, a caret/▸▾ affordance).
- State: `openDoc` (which `doc_uri` is expanded) and a `docText` cache (`doc_uri -> {text,char_len,
  truncated} | {error} | 'loading'`).
- On first expand, call `gwClient.documentText(activeGraph, d.doc_uri)`; cache the result.
- Render text in a scrollable `<pre>` (`whiteSpace:'pre-wrap'`, `maxHeight` ~320, `overflow:auto`,
  same dark style as the recipe box). When `truncated`, a muted line: *"showing first 20,000 of
  {char_len} characters"*. On `{error}` or empty text: *"Source text not captured for this document
  (extracted before text was stored — re-run Extract on the same document to capture it)."*
- Version bump (`EXPLORER_VERSION`).

### Data flow

```
Extract → POST /extract (file or text)
    → doc = read_document(...) | text
    → extract_document → ingest_elements → record_document (ledger)
    → record_document_text(graph, doc_uri, doc)          # NEW: full text persisted

Storage → "Provenance — documents" list (GET /documents, unchanged — light)
    → user clicks a row
    → GET /document_text?graph=&doc_uri=&limit=20000 → {text, char_len, truncated}
    → scrollable <pre> + truncation note
```

## Error handling

- **Fresh table** via `CREATE TABLE IF NOT EXISTS` — no migration step; old meta DBs gain the table
  on next connect.
- **Text row missing** (pre-feature graph, or an extraction that failed before the text write):
  `get_document_text` returns `None`/empty → panel shows the "not captured" note; other rows and the
  recipe box are unaffected.
- **record_document_text failure** during `/extract`: wrapped so it never fails the extraction result
  the user cares about — text capture is best-effort provenance, not part of the ingest contract.
  (Extraction/ingest already succeeded and the ledger row is committed at that point.)
- **Per-row fetch failure** in the UI: isolated to that row (its cache entry becomes `{error}`);
  sibling rows unaffected.

## Testing

- **Backend unit (`tests/test_metadata_store.py`):** `record_document_text` + `get_document_text`
  round-trip; `limit` slices text and sets `truncated`; `has_document_text` true/false;
  `clear_graph_metadata` removes text rows; fresh-DB table creation.
- **Endpoint (`tests/test_app.py` or the extract test):** `/extract` with FakeAdapter + a real
  DuckDB compute store + stub LLM persists text; `/document_text` returns it with `char_len`; the
  reuse path backfills a missing text row without re-extracting.
- **Frontend:** esbuild JSX check (`ESBUILD_OK`) + gateway `curl` 200 (React app not headlessly
  verifiable per CLAUDE.md); `gateway.js` client test for `documentText` with an injected fake fetch.

## Files

- `backend/xgraph_gateway/compute/duckdb_engine.py` — `xgraph_document_texts` table;
  `record_document_text`, `has_document_text`, `get_document_text`; `clear_graph_metadata` clears it.
- `backend/xgraph_gateway/app.py` — `/extract` records text (full path + reuse backfill); new
  `GET /document_text`.
- `frontend/gateway.js` — `documentText(graph, docUri, limit)` client method.
- `frontend/XGraph.html` — StoragePanel expandable per-document text; version bump.
- `backend/tests/test_metadata_store.py`, `backend/tests/test_app.py` (or extract test) — coverage.
- `frontend/tests/test_client.mjs` — `documentText` assertion.

## Out of scope (follow-ups)

- **Generic paginated DB-table viewer** in the UI (the user flagged wanting table viewing by
  pagination) — separate spec.
- **File-on-disk (DuckDB-hydrate) or Kinetica-table backing** for very large source texts — the
  table + display cap covers the common case now.
- Capturing text for **non-extraction** build routes (files/DDL) — those already preview their real
  source files (2026-07-25 provenance spec).

## Note on committing

Per CLAUDE.md (*"Do NOT git commit anything under xgraph/"*), this spec is written to disk but **not
committed**. The brainstorming skill's "commit the design doc" step is overridden by that project
rule until the user says otherwise.
