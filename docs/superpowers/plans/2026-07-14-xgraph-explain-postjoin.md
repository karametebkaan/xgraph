# xGraph — Explain with post-join hydration (NL → hydrated SQL → English)

> subagent-driven, **no-commit (local)**. Wires the DuckDB late-hydration + OLAP route into the Explain panel.

**Goal:** When the Explain focus asks about attributes not in the graph result (e.g. `party_name`, which lives in the wide hydrate source, not the skinny graph), post-join those attributes onto the returned NODE ids in DuckDB, aggregate as the focus requires (e.g. count paths per `party_name`), and synthesize a domain-English answer over the aggregated table.

**Architecture:** Parallel to the existing NL→Cypher round-trip. A new NL→SQL step (`generate_join_sql`) produces a read-only DuckDB SELECT over two relations — `cypher` (the already-fetched result rows) and `wide` (the hydrate file, exposed as a view over the client-supplied `source`). A new compute method `run_join(rows, source, join_sql)` registers the rows + wide view and runs the SQL (reusing falkor `graph_loader.hydrate`'s `_register_rows` + relation-name/source guards). A new `POST /explain` endpoint orchestrates: probe wide columns → generate join SQL → validate (read-only) → run → synthesize over the aggregated result. If the focus needs no wide column (or no focus/source), it falls back to the plain semantic `synthesize`.

**Tech stack:** FastAPI, DuckDB (in-process), kgr `_llm` (claude CLI), single-file React/Babel frontend.

## Global Constraints
- **No git commit under `xgraph/`** — local only. Do NOT modify committed `falkor/` code; reuse it by import only.
- Backend runs on falkor's venv: `/home/kkaramete/github-graph/graph/falkor/.venv/bin/python`. Gateway on `:8090` (the controller restarts it after backend edits).
- Read-only guard on all generated SQL. `source` (wide file) is supplied by the client (like `/hydrate`), never interpolated from an untrusted server path; relation names + source path pass falkor's existing guards (`_REL_RE`, no `'` in source).
- Tests need NO live services: unit tests inject a fake `llm` and write small Parquet to `tmp_path` in-process (match the existing duckdb/nlcypher test style). Run the FULL backend suite (currently 134) — must stay green.
- Frontend edits validate via `@babel/standalone` transpile of the single `<script type="text/babel">` block — must PASS. Branding stays xGraph; keep the file's inline-style vocabulary.
- The plain-explanation behavior added earlier MUST remain the fallback (empty focus, no source, or no wide column needed → same answer as today).

---

### Task 1: NL→SQL generator + read-only SQL guard (nlcypher.py)

**Files:** modify `backend/xgraph_gateway/nlcypher.py`; test `backend/tests/test_nlcypher.py`.

**Interfaces (produce):**
- `generate_join_sql(focus, cypher, result_columns, wide_columns, llm=None) -> str`
  - Returns a single read-only DuckDB SELECT over relation `cypher` (alias suggest `c`, columns = `result_columns`) joined to view `wide` (alias `w`, columns = `wide_columns`, keyed by a `NODE` id column). Returns **empty string** if the focus can be answered from the graph result alone (no wide column needed).
  - Uses a new `_JOIN_SQL_SCHEMA = {type:object, properties:{sql:{type:string}}, required:[sql], additionalProperties:false}`.
  - Prompt MUST include (verbatim substrings the test will assert): the `focus`, the `cypher`, the `result_columns`, the `wide_columns`, and instructions that: (a) `cypher` holds the graph result — node columns hold NODE ids, `*_LABEL`/label columns hold relationship-type strings; (b) `wide` is the attribute file keyed by `NODE`; (c) use the **cypher** to map each result column to its node type (e.g. `RETURN c.NODE AS c_node` with `(c:party)` ⇒ `c_node` holds party ids), then join the matching `*_node` column to `wide.NODE` to pull an attribute; (d) "number of paths" = `COUNT(*)` over the cypher rows grouped by the attribute; (e) read-only SELECT only, no trailing semicolon, no markdown fences; (f) return `sql` = "" (empty) if no `wide` attribute is needed.
  - Mirror `generate_cypher`'s output handling: `out = call(prompt, schema=_JOIN_SQL_SCHEMA)`; if `isinstance(out, str): out = json.loads(out)`; `sql = (out.get("sql") or "").strip().rstrip(";").strip()`; return it.
- `validate_sql(sql) -> tuple[bool, str]`
  - Reject empty. Reject anything that is not a single read-only statement: after stripping, the first keyword MUST be `SELECT` or `WITH` (case-insensitive); reject if it contains a `;` followed by more non-whitespace (multi-statement); reject if a write/DDL/side-effect keyword appears: `\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|ATTACH|DETACH|COPY|INSTALL|LOAD|PRAGMA|EXPORT|IMPORT|CALL)\b` (case-insensitive). Return `(True, "")` otherwise. Add module-level compiled `_SQL_WRITE_KW`.

- [ ] Step 1 — Write failing unit tests in `test_nlcypher.py` using a fake `llm` that records the prompt and returns a canned `{"sql": "..."}`:
  - `generate_join_sql` returns the fake's sql (stripped of trailing `;`); the recorded prompt CONTAINS the focus text, the cypher text, a result column name (e.g. `c_node`), and a wide column name (e.g. `party_name`).
  - fake returning `{"sql": ""}` ⇒ `generate_join_sql` returns `""`.
  - `validate_sql("SELECT w.party_name, COUNT(*) FROM cypher c JOIN wide w ON c.c_node=w.NODE GROUP BY 1")` ⇒ `(True, "")`.
  - `validate_sql("DROP TABLE wide")`, `validate_sql("SELECT 1; DELETE FROM wide")`, `validate_sql("")` ⇒ each `(False, <nonempty reason>)`.
- [ ] Step 2 — Run: `cd backend && /home/kkaramete/github-graph/graph/falkor/.venv/bin/python -m pytest tests/test_nlcypher.py -v` → new tests FAIL.
- [ ] Step 3 — Implement `generate_join_sql`, `_JOIN_SQL_SCHEMA`, `validate_sql`, `_SQL_WRITE_KW`.
- [ ] Step 4 — Run the same pytest → PASS; then FULL suite green.
- [ ] Step 5 — Commit-equivalent checkpoint (local; no git).

### Task 2: rows-first post-join + source probe (compute) + /explain endpoint + gateway.js

**Files:** modify `backend/xgraph_gateway/compute/duckdb_engine.py`, `backend/xgraph_gateway/app.py`, `frontend/gateway.js`; test `backend/tests/test_explain.py` (new).

**Interfaces (consume):** `nlcypher.generate_join_sql`, `nlcypher.validate_sql`, `nlcypher.synthesize(question, columns, rows, cypher=...)`.
**Interfaces (produce):**
- `DuckDBComputeEngine.describe_source(source) -> list[str]` — column names of the wide file. Guard: `if "'" in str(source): raise ValueError`. `con=duckdb.connect(); con.execute(f"DESCRIBE SELECT * FROM '{source}'")`; return first field of each row; close in `finally`.
- `DuckDBComputeEngine.run_join(rows, source, join_sql, cypher_relation="cypher", wide_relation="wide") -> list[dict]` — `rows` is a list of dicts (one key per result column). Reuse falkor guards + register:
  ```python
  from graph_loader.hydrate import _register_rows, _REL_RE   # module import at top
  ```
  Validate both relation names via `_REL_RE.fullmatch` (raise ValueError on miss) and `if "'" in str(source): raise`. If `not rows: return []`. `con=duckdb.connect()`; `_register_rows(con, cypher_relation, rows)`; `con.execute(f"CREATE OR REPLACE VIEW {wide_relation} AS SELECT * FROM '{source}'")`; `cur=con.execute(join_sql)`; `cols=[d[0] for d in cur.description]`; `return [coerce_row(cols,r) for r in cur.fetchall()]`; close in `finally`. (`coerce_row` already imported in this module.)
- `POST /explain` body `{question, columns, rows, cypher?, source?, session?, engine?}` → `{answer, join_sql, columns, rows, hydrated}`. Orchestration:
  ```
  focus  = (payload.get("question") or "").strip()
  source = payload.get("source")
  cols, rows, cypher = payload["columns"], payload["rows"], payload.get("cypher")
  compute = _resolve_compute(session)
  join_sql, hydrated = None, False
  out_cols, out_rows = cols, rows
  if focus and source:
      wide_cols = compute.describe_source(source)
      join_sql = nlcypher.generate_join_sql(focus, cypher, cols, wide_cols) or None
      if join_sql:
          ok, reason = nlcypher.validate_sql(join_sql)
          if not ok: return _err("duckdb", ValueError(reason))
          dict_rows = [dict(zip(cols, r)) for r in rows]
          agg = compute.run_join(dict_rows, source, join_sql)
          out_cols = list(agg[0].keys()) if agg else []
          out_rows = [[d.get(c) for c in out_cols] for d in agg]
          hydrated = True
  q = focus or "Explain these results"
  answer = nlcypher.synthesize(q, out_cols, out_rows,
                               cypher=(join_sql if hydrated else cypher))
  return {"answer": answer, "join_sql": join_sql, "columns": out_cols,
          "rows": out_rows, "hydrated": hydrated}
  ```
  Wrap in `try/except Exception as e: return _err(payload.get("engine",""), e)`.
- `gateway.js`: `explain(question, columns, rows, cypher, source)` → `POST /explain` with `{question, columns, rows, cypher, source, session}` (session added like other POSTs, using the same pattern the file already uses for `synthesize`/`ask`). Keep `synthesize()` as-is.

- [ ] Step 1 — Write failing tests in `test_explain.py` (TestClient + fake compute/llm; no services). Monkeypatch `nlcypher._llm`/`_get_llm` per the existing test pattern, or inject via a fake adapter/compute in `create_app`.
  - `run_join`: write `wide.parquet` in `tmp_path` with rows `{"NODE":"party-A","party_name":"Acme"}`,`{"NODE":"party-B","party_name":"Beta"}`; `rows=[{"c_node":"party-A"},{"c_node":"party-A"},{"c_node":"party-B"}]`; `join_sql="SELECT w.party_name, COUNT(*) AS sar_paths FROM cypher c JOIN wide w ON c.c_node=w.NODE GROUP BY w.party_name ORDER BY sar_paths DESC"` ⇒ first row `party_name=="Acme"`, `sar_paths==2`.
  - `describe_source(wide.parquet)` ⇒ contains `"NODE"` and `"party_name"`.
  - `POST /explain` with focus + source, fake `generate_join_sql`→the agg SQL and fake `synthesize`→canned answer ⇒ response `hydrated==True`, `join_sql` set, `columns==["party_name","sar_paths"]`, `rows[0]==["Acme",2]`, `answer` = canned.
  - `POST /explain` with focus but `generate_join_sql`→"" ⇒ `hydrated==False`, `join_sql==None`, original columns/rows echoed, `synthesize` still called.
  - `POST /explain` with no focus ⇒ `hydrated==False`, plain synthesize over original columns/rows.
  - `validate_sql` rejection path: fake `generate_join_sql`→`"DROP TABLE wide"` ⇒ 400 error envelope (`error.code`).
- [ ] Step 2 — Run: `cd backend && .../python -m pytest tests/test_explain.py -v` → FAIL.
- [ ] Step 3 — Implement `describe_source`, `run_join`, `/explain`, `gateway.js explain()`.
- [ ] Step 4 — Run `tests/test_explain.py` → PASS; then FULL suite green.
- [ ] Step 5 — Checkpoint.

### Task 3: Explain panel — post-join focus (frontend)

**Files:** modify `frontend/XGraph.html`.
**Interfaces (consume):** `gwClient.explain(question, columns, rows, cypher, source)`, the existing `HYDRATE_SOURCE` const, `queryTabSql[activeQueryTab]`.

- [ ] Step 1 — In `InteractExplainPanel.handleExplain`, replace the `gwClient.synthesize(...)` call with `gwClient.explain(focusText || 'Explain these results', res.columns, res.rows, queryTabSql[activeQueryTab], HYDRATE_SOURCE)`. (`res` = the active query tab's result; `focusText` = the existing optional-focus input.)
- [ ] Step 2 — Render the response: keep the existing English-answer block (now `resp.answer`). When `resp.hydrated`, add ABOVE the answer (matching the file's inline-style vocabulary):
  - a collapsible/labeled "Post-join SQL" block showing `resp.join_sql` in a monospace `<pre>` (small font, same treatment as where the panel shows the generated Cypher elsewhere), and
  - a compact results table rendered from `resp.columns` + `resp.rows` (reuse the panel's existing table renderer if one is in scope; otherwise a minimal `<table>` in the same style).
  When `resp.hydrated` is false, render exactly as today (answer only) — no SQL block, no extra table.
- [ ] Step 3 — Validate: `@babel/standalone` transpile of the `<script type="text/babel">` block → PASS; grep confirms `gwClient.explain(` present and the old `gwClient.synthesize(` call in this panel is gone.
- [ ] Step 4 — Checkpoint. (Browser acceptance by the user.)

## Self-Review
- Post-join extracts wide attributes (party_name) not in the graph → Task 1 (`generate_join_sql`) + Task 2 (`run_join`). ✓
- Focus "who has most SAR activity (number of paths) by party_name" → COUNT(*) group-by via NL→SQL, aggregated in DuckDB (not LLM eyeballing) → Task 1/2. ✓
- Auto-trigger when focus present + source configured; falls back to plain semantic answer otherwise → Task 2 `/explain` orchestration. ✓
- Reuse falkor hydrate machinery (`_register_rows`, guards) by import; no falkor edits → Global + Task 2. ✓
- Read-only guard on generated SQL; client-supplied source; existing relation/source guards → `validate_sql` + Task 2. ✓
- Transparency: panel surfaces join SQL + aggregated table → Task 3. ✓
- No placeholders: exact signatures, prompt-content assertions, and the `/explain` orchestration body are spelled out.
