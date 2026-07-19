# Kinetica file-import — design (LOAD DATA INTO for the structured builder)

**Date:** 2026-07-19
**Status:** Approved design, pre-implementation
**Origin:** Slice B3 (`POST /register_file`) made file paths first-class builder sources for DuckDB/FalkorDB but explicitly deferred Kinetica, because Kinetica needs server-side ingestion (external table / LOAD DATA / KiFS / DATA SOURCE) rather than a path the gateway can read locally. This closes that gap so "build a graph from files" works on Kinetica too.

## Problem

For DuckDB/FalkorDB, a file *is* a relation the build reads directly, so B3's `register_file` just remembers the path in the session and the picker lists it. Kinetica is a separate server: a gateway-host path is not readable by Kinetica, and Kinetica has **no existing file-import code** (verified: zero `EXTERNAL` / `LOAD DATA` / `DATA SOURCE` / `KiFS` references in `kinetica_adapter.py` or `kinetica_source.py`). To use a file as a builder source on Kinetica, the file must first become a Kinetica **table**.

## Decisions (locked during brainstorming)

1. **Mechanism: `LOAD DATA INTO` (materialize)** — import the file into a real Kinetica table. Uniform for remote and local; the resulting table behaves like any other (no external-table quirks) and feeds the builder's `INPUT_TABLES` identically.
2. **Remote access: reuse an existing named DATA SOURCE** — the user supplies the name of a `DATA SOURCE` already configured in Kinetica (admin-managed). The gateway references it in the import and **never handles secrets** (no `CREATE CREDENTIAL` / `CREATE DATA SOURCE`).
3. **Local files: same `LOAD DATA INTO`, no DATA SOURCE clause** — "local" means a path Kinetica can already read (a KiFS path `kifs://…` or a server-accessible path). The `DATA SOURCE` clause is emitted only when a name is given.
4. **No session registry for Kinetica** — the import creates a real table, and `KineticaAdapter.list_tables()` already enumerates real tables, so `/tables` surfaces it automatically after a refresh.
5. **Deferred:** re-registering the same target table **appends** (LOAD DATA semantics) — documented, no dedup/replace in v1; **KiFS byte-upload** of a browser-local file's contents stays out of scope.

## Architecture

### Backend

- **Pure SQL builder** (module scope in `kinetica_adapter.py`, mirroring the existing pure builders like `create_table_sql`):

  ```python
  def load_data_sql(table: str, path: str, fmt: str, data_source: str | None = None) -> str
  ```

  Produces:
  ```
  LOAD DATA INTO <table>
  FROM FILE PATHS '<path>'
  FORMAT <PARQUET|CSV|JSON>
  [WITH OPTIONS (DATA SOURCE = '<name>')]
  ```
  - `table` sanitized via `graph_loader.mapper.safe_ident` (schema-qualified allowed: sanitize each dotted segment).
  - `path` and `data_source` are single-quote-guarded (reject values containing `'`) — they are interpolated into DDL.
  - `fmt` validated against `{"parquet", "csv", "json", "shapefile", "avro"}` (default `parquet`).
  - `WITH OPTIONS (DATA SOURCE = …)` emitted only when `data_source` is truthy.

- **Adapter method** `KineticaAdapter.register_file(self, path, table=None, fmt=None, data_source=None) -> dict`:
  - Derive `fmt` from the file extension when not given (`.parquet`→parquet, `.csv`/`.tsv`→csv, `.json`/`.jsonl`→json), default parquet.
  - Derive `table` from the filename stem when not given, sanitized; caller may pass a schema-qualified name.
  - Run `load_data_sql(...)` via `self._db.execute_sql(...)`, check `resp.is_ok()`, raise `RuntimeError(<kinetica message>)` on failure.
  - Return `{"name": table, "type": "table", "columns": self._current_columns(table)}`.

- **Base adapter default** `register_file(self, *a, **k)` → `raise NotImplementedError("register_file not supported for this engine")` (only Kinetica overrides; DuckDB/FalkorDB are handled at the endpoint by the session-registry path, not the adapter).

- **Endpoint** `POST /register_file` branches by resolved engine:
  - **Non-Kinetica** (existing B3 behavior): validate via `describe_source`, store path in session, return `{name, type:"file", columns}`.
  - **Kinetica:** call `adapter.register_file(path, table, fmt, data_source)` from payload `{path, table?, format?, data_source?}` and return its result. No session registry write (the table is real).

### Frontend

- The existing **＋ File** affordance in `CreateHelperPanel` becomes engine-aware:
  - **Non-Kinetica:** today's single path prompt (unchanged).
  - **Kinetica:** collect `path`, optional `DATA SOURCE name` (blank = local/KiFS/server path), and optional `target table` (blank = derived) — via sequential prompts, matching the current minimal UI pattern. Call `gwClient.registerFile({path, data_source, table, format})`, then refetch `/tables` so the imported table appears, and set it as the section's default table.
- `gwClient.registerFile` generalizes to accept either a string path (non-Kinetica, back-compat) or an options object `{path, data_source, table, format}` (Kinetica).

## Data flow

- **Remote:** builder ＋ File → `POST /register_file {engine:kinetica, session, path:'s3://bucket/f.parquet', data_source:'my_s3', format:'parquet'}` → `LOAD DATA INTO f FROM FILE PATHS 's3://bucket/f.parquet' FORMAT PARQUET WITH OPTIONS (DATA SOURCE = 'my_s3')` → table `f` → `/tables` lists it → builder picks it.
- **Local (KiFS/server path):** same, `data_source` omitted → `LOAD DATA INTO … FROM FILE PATHS 'kifs://…' FORMAT …`.

## Error handling

- Uniform `{"error":{…}}` envelope. LOAD DATA failures (bad path, unknown DATA SOURCE, format mismatch, no read access) surface Kinetica's own message via `_err`. `register_file` raising `NotImplementedError` for an unsupported engine → 400.
- Injection guard failures (`'` in path/data_source) raise `ValueError` → 400.

## Testing

- **Pure unit** (`test_kinetica_file_import.py`, no live DB): `load_data_sql` — remote (with DATA SOURCE), local (no DATA SOURCE), each format, `safe_ident` sanitization, and quote-injection rejection.
- **Gateway routing** (Fake): a `FakeAdapter.register_file` returning a canned table so `POST /register_file {engine:"fake-kinetica"}`-style routing is covered; assert the Kinetica branch calls the adapter (not the session-registry path).
- **Live-skip integration:** `_kinetica_or_skip()`; the LOAD DATA test additionally skips unless a test DATA SOURCE or a Kinetica-readable path is configured (env-gated), then asserts the table appears in `list_tables()` and drops it in teardown (throwaway name).
- Regression: existing `/register_file` DuckDB/FalkorDB tests keep passing (the endpoint's non-Kinetica branch is unchanged).

## Files (indicative)

- **Backend:** `adapters/kinetica_adapter.py` (`load_data_sql` + `register_file`), `adapters/base.py` (default `register_file`), `adapters/fake.py` (canned), `app.py` (`/register_file` engine branch), `tests/test_kinetica_file_import.py`.
- **Frontend:** `frontend/gateway.js` (`registerFile` accepts an options object), `frontend/XGraph.html` (engine-aware ＋ File prompts), version bump.

## Deferred / out of scope

- `CREATE CREDENTIAL` / `CREATE DATA SOURCE` provisioning (gateway stays secret-free; admin pre-creates the DATA SOURCE).
- KiFS byte-upload of a browser-local file.
- Replace/dedup on re-import (v1 appends).
- External-table (query-in-place) mode.
