# Explain/Ask LLM-Knowledge Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a graph query can't answer the question, answer it from the model's own general knowledge in a clearly-separated block — gated by a default-ON Setup toggle.

**Architecture:** The grounded `nlcypher.synthesize()` call self-reports whether the results actually answered the question (`answered_from_results`). `/ask` and `/explain` read a per-request `fallback` flag (default true); when it's on and the grounded answer wasn't from the results, they make a second fast-tier `general_knowledge_answer()` call and return it as a separate `fallback_answer` field. The frontend adds a Setup toggle that drives that flag and renders the fallback in a distinct "💡 General knowledge:" block.

**Tech Stack:** Python/FastAPI backend (`backend/xgraph_gateway/`), pytest; single-file React 18 UMD + Babel frontend (`frontend/XGraph.html`), `gateway.js` UMD client, Node test files.

**Spec:** `docs/superpowers/specs/2026-07-28-explain-llm-knowledge-fallback-design.md`

## Global Constraints

- **No commits under `xgraph/` are pushed without the user's say-so, but committing locally per task is expected** (this repo is developed locally; the user drives push). Commit style: **concise one-line message, NO co-authorship footer**.
- **`synthesize`'s default return type must stay `str`** — `return_meta=False` (default) returns the grounded string exactly as today. Only `return_meta=True` returns a dict. Callers that don't opt in (the `/synthesize` endpoint and its tests) must remain byte-for-byte behavior-compatible.
- **Fallback default is ON**: `payload.get("fallback", True)` on the backend; `answerFallback` App state defaults `true`; `gateway.js` defaults the arg to `true` when omitted. Absent flag ⇒ fallback enabled.
- **Fast Haiku tier** for both the grounded and the parametric calls (they already use `_get_llm()` which pins `fast_model()`). Do not introduce a new model tier.
- **The parametric call is best-effort**: a failure in `general_knowledge_answer` must never break or change the grounded answer — `fallback_answer` stays `null` on error.
- **Provenance separation**: the fallback answer is always a separate field/block labeled "💡 General knowledge:" — never blended into the grounded `answer`.
- **Bump `EXPLORER_VERSION`** (one patch level above the current `0.22.3`) when `XGraph.html` changes.
- Frontend validation gate for `XGraph.html`: the Babel/esbuild transpile check AND a `curl` 200 from the gateway, plus the Node tests. Real behavior is browser-verified by the user.

---

### Task 1: Backend `nlcypher` — grounding flag + parametric answer

**Files:**
- Modify: `backend/xgraph_gateway/nlcypher.py` (`_ANSWER_SCHEMA` at 58-65; `synthesize` at 335-370; add a new schema + function after `synthesize`)
- Test: `backend/tests/test_nlcypher.py`

**Interfaces:**
- Consumes: existing `_get_llm()`, the injected `llm` callable convention (`call(prompt, schema=...)`), `_compact_rows`.
- Produces (later tasks rely on these exact signatures):
  - `synthesize(question, columns, rows, llm=None, cypher=None, return_meta=False)` → `str` when `return_meta` is false (unchanged), else `{"answer": str, "answered_from_results": bool}`.
  - `general_knowledge_answer(question: str, llm=None) -> str`.

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_nlcypher.py` (it already defines a module-level `fake_llm(prompt, *, schema=None)` returning a dict — reuse the same style; define local fakes that echo the flag):

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_nlcypher.py -k "synthesize_default or return_meta or general_knowledge" -v`
Expected: FAIL — `general_knowledge_answer` undefined; `synthesize` has no `return_meta` kwarg (TypeError).

- [ ] **Step 3: Add the grounding flag to `_ANSWER_SCHEMA`**

Replace `_ANSWER_SCHEMA` (lines 58-65) with:

```python
_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "description": "Plain-English answer to the question."},
        "answered_from_results": {
            "type": "boolean",
            "description": (
                "true only if the answer genuinely comes from the result rows. "
                "false when the rows do not contain what the question asked "
                "(including when the results are empty)."
            ),
        },
    },
    "required": ["answer", "answered_from_results"],
    "additionalProperties": False,
}

_GENERAL_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "description": "Plain-English answer from general knowledge."},
    },
    "required": ["answer"],
    "additionalProperties": False,
}
```

- [ ] **Step 4: Extend `synthesize` — instruct the flag, add `return_meta`**

In `synthesize` (335-370): (a) change the signature to add `return_meta=False`; (b) add one sentence to the prompt telling the model to set the flag; (c) change the return handling to optionally return the meta dict. Concretely:

Change the `def` line (335-336) to:

```python
def synthesize(question: str, columns: list, rows: list, llm: Optional[LLMFunc] = None,
               cypher: Optional[str] = None, return_meta: bool = False):
    """Turn query results into a domain-relevant, plain-English explanation.

    Returns the grounded answer string by default. With ``return_meta=True`` returns
    ``{"answer": str, "answered_from_results": bool}`` — the flag is the model's
    self-report of whether the result rows actually answered the question (forced
    False when there are no rows).
    """
```

Change the final instruction line of the prompt (line 362) from:

```python
        "Return JSON with a single field `answer` containing the plain-English explanation."
```

to:

```python
        "Return JSON with `answer` (the plain-English explanation) and "
        "`answered_from_results`: set it to true only if `answer` genuinely comes from "
        "the result rows; set it to false when the rows do not contain what the question "
        "asked (including when the results are empty)."
```

Replace the return block (365-370) with:

```python
    if isinstance(out, str):
        try:
            out = json.loads(out)
        except json.JSONDecodeError:
            answer, flag = out.strip(), True
            return {"answer": answer, "answered_from_results": bool(rows) and flag} if return_meta else answer
    answer = str(out.get("answer", "")).strip()
    if not return_meta:
        return answer
    flag = bool(rows) and bool(out.get("answered_from_results", True))
    return {"answer": answer, "answered_from_results": flag}
```

- [ ] **Step 5: Add `general_knowledge_answer`**

Immediately after `synthesize` (after line 370), add:

```python
def general_knowledge_answer(question: str, llm: Optional[LLMFunc] = None) -> str:
    """Answer a question from the model's own general (parametric) knowledge.

    Used as a fallback when the graph query results did not answer the question.
    Fast-tier call; the answer is labeled as general knowledge by the caller and is
    never presented as graph-derived.
    """
    call = llm or _get_llm()
    prompt = (
        "The user asked a question that their graph query results did not answer. "
        "Using your own general knowledge, answer the question directly and concisely. "
        "If you do not know, say so plainly. Do NOT claim this information came from "
        "their data or graph.\n\n"
        f"Question: {question}\n\n"
        "Return JSON with a single field `answer` containing the plain-English answer."
    )
    out = call(prompt, schema=_GENERAL_ANSWER_SCHEMA)
    if isinstance(out, str):
        try:
            out = json.loads(out)
        except json.JSONDecodeError:
            return out.strip()
    return str(out.get("answer", "")).strip()
```

- [ ] **Step 6: Run the new tests + the full nlcypher suite**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_nlcypher.py -v`
Expected: all PASS, including the pre-existing `test_synthesize_*` tests (they call `synthesize` without `return_meta` and still get a string). If a pre-existing synth test's fake returns `{"answer": ...}` without `answered_from_results`, it still passes because the default path reads only `answer`.

- [ ] **Step 7: Commit**

```bash
git add backend/xgraph_gateway/nlcypher.py backend/tests/test_nlcypher.py
git commit -m "feat(explain): synthesize grounding flag + general_knowledge_answer (fast tier)"
```

---

### Task 2: Backend `/ask` + `/explain` — wire the fallback

**Files:**
- Modify: `backend/xgraph_gateway/app.py` (`/ask` at 521-540; `/explain` at 611-670)
- Test: `backend/tests/test_explain.py` (update existing `fake_synthesize` fakes; add fallback cases), `backend/tests/test_app.py` (Ask fallback case)

**Interfaces:**
- Consumes: `nlcypher.synthesize(..., return_meta=True)` and `nlcypher.general_knowledge_answer(question)` from Task 1.
- Produces: `/ask` and `/explain` JSON responses each gain `answered_from_results: bool` and `fallback_answer: str | null`; both read an optional request field `fallback` (default true).

> **Critical integration note:** `test_explain.py` currently monkeypatches `nlcypher.synthesize` with fakes whose signature is `(question, cols, rows_, llm=None, cypher=None)`. Once the endpoint calls `synthesize(..., return_meta=True)`, those fakes raise `TypeError: unexpected keyword argument 'return_meta'`. Every such fake in `test_explain.py` MUST gain `return_meta=False` and return the meta dict when it's true. This is required, not optional.

- [ ] **Step 1: Write/adjust the failing tests**

First, update EVERY `fake_synthesize` in `test_explain.py` to the new signature. The canonical shape (apply to each occurrence — lines ~70, ~101, ~132, ~198, and any other `fake_synthesize`/inline `synthesize` fake):

```python
    def fake_synthesize(question, cols, rows_, llm=None, cypher=None, return_meta=False):
        ans = "explained: " + question
        if return_meta:
            return {"answer": ans, "answered_from_results": True}
        return ans
```

(Keep each fake's existing answer text; only add the `return_meta` param and the dict branch.)

Then add these new tests to `test_explain.py`:

```python
def test_explain_falls_back_when_not_answered(monkeypatch):
    from xgraph_gateway import nlcypher
    def fake_synthesize(question, cols, rows_, llm=None, cypher=None, return_meta=False):
        if return_meta:
            return {"answer": "cannot be determined", "answered_from_results": False}
        return "cannot be determined"
    def fake_general(question, llm=None):
        return "Lindsey Graham is 70."
    monkeypatch.setattr(nlcypher, "generate_join_sql", lambda *a, **k: None)
    monkeypatch.setattr(nlcypher, "synthesize", fake_synthesize)
    monkeypatch.setattr(nlcypher, "general_knowledge_answer", fake_general)
    c = _client()  # reuse this file's existing client builder / TestClient(create_app(...))
    r = c.post("/explain", json={"question": "How old is Lindsay Graham",
                                 "columns": ["NODE"], "rows": [["p1"]], "graph": "g"})
    body = r.json()
    assert body["answered_from_results"] is False
    assert body["fallback_answer"] == "Lindsey Graham is 70."


def test_explain_no_fallback_when_answered(monkeypatch):
    from xgraph_gateway import nlcypher
    def fake_synthesize(question, cols, rows_, llm=None, cypher=None, return_meta=False):
        if return_meta:
            return {"answer": "Two parties are linked.", "answered_from_results": True}
        return "Two parties are linked."
    def boom_general(question, llm=None):
        raise AssertionError("general_knowledge_answer must not be called when answered")
    monkeypatch.setattr(nlcypher, "generate_join_sql", lambda *a, **k: None)
    monkeypatch.setattr(nlcypher, "synthesize", fake_synthesize)
    monkeypatch.setattr(nlcypher, "general_knowledge_answer", boom_general)
    c = _client()
    r = c.post("/explain", json={"question": "who is linked", "columns": ["NODE"],
                                 "rows": [["p1"]], "graph": "g"})
    body = r.json()
    assert body["answered_from_results"] is True
    assert body["fallback_answer"] is None


def test_explain_toggle_off_skips_fallback(monkeypatch):
    from xgraph_gateway import nlcypher
    def fake_synthesize(question, cols, rows_, llm=None, cypher=None, return_meta=False):
        if return_meta:
            return {"answer": "cannot be determined", "answered_from_results": False}
        return "cannot be determined"
    def boom_general(question, llm=None):
        raise AssertionError("fallback disabled — general_knowledge_answer must not run")
    monkeypatch.setattr(nlcypher, "generate_join_sql", lambda *a, **k: None)
    monkeypatch.setattr(nlcypher, "synthesize", fake_synthesize)
    monkeypatch.setattr(nlcypher, "general_knowledge_answer", boom_general)
    c = _client()
    r = c.post("/explain", json={"question": "How old is X", "columns": ["NODE"],
                                 "rows": [["p1"]], "graph": "g", "fallback": False})
    body = r.json()
    assert body["answered_from_results"] is False
    assert body["fallback_answer"] is None


def test_explain_fallback_best_effort_on_error(monkeypatch):
    from xgraph_gateway import nlcypher
    def fake_synthesize(question, cols, rows_, llm=None, cypher=None, return_meta=False):
        if return_meta:
            return {"answer": "cannot be determined", "answered_from_results": False}
        return "cannot be determined"
    def boom_general(question, llm=None):
        raise RuntimeError("LLM down")
    monkeypatch.setattr(nlcypher, "generate_join_sql", lambda *a, **k: None)
    monkeypatch.setattr(nlcypher, "synthesize", fake_synthesize)
    monkeypatch.setattr(nlcypher, "general_knowledge_answer", boom_general)
    c = _client()
    r = c.post("/explain", json={"question": "How old is X", "columns": ["NODE"],
                                 "rows": [["p1"]], "graph": "g"})
    body = r.json()
    # grounded answer survives; fallback silently null
    assert body["answer"] == "cannot be determined"
    assert body["fallback_answer"] is None
```

> `_client()` above is illustrative — use whatever this file already uses to build the TestClient (grep the file: it does `TestClient(create_app(adapter_factory=lambda e: FakeAdapter(), compute=compute))`). Reuse that exact construction.

Add one Ask-side test to `test_app.py` (grep it for its existing `/ask` test + fake-LLM injection pattern and mirror it):

```python
def test_ask_fallback_when_not_answered(monkeypatch):
    from xgraph_gateway import nlcypher
    monkeypatch.setattr(nlcypher, "generate_cypher", lambda *a, **k: "MATCH (n) RETURN n LIMIT 1")
    monkeypatch.setattr(nlcypher, "validate_cypher", lambda *a, **k: (True, ""))
    def fake_synthesize(question, cols, rows_, llm=None, cypher=None, return_meta=False):
        if return_meta:
            return {"answer": "no age here", "answered_from_results": False}
        return "no age here"
    monkeypatch.setattr(nlcypher, "synthesize", fake_synthesize)
    monkeypatch.setattr(nlcypher, "general_knowledge_answer", lambda q, llm=None: "70 years old.")
    c = _client()  # this file's TestClient builder with a FakeAdapter that returns rows
    r = c.post("/ask", json={"engine": "fake", "graph": "g", "question": "how old is X?"})
    body = r.json()
    assert body["answered_from_results"] is False
    assert body["fallback_answer"] == "70 years old."
```

> Confirm the FakeAdapter used by `test_app.py` returns at least one row from `run_query` so `answered_from_results` isn't force-false by the empty-rows rule; if it returns no rows, adjust the test's adapter or assert the empty-rows path instead. Match the file's existing fake-adapter construction.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_explain.py tests/test_app.py -k "fallback or explain" -v`
Expected: the new fallback tests FAIL (response has no `fallback_answer`/`answered_from_results`), and — before you touch app.py — the pre-existing explain tests still pass because you already fixed their fakes' signatures in Step 1.

- [ ] **Step 3: Add a shared fallback helper in `app.py`**

Just above the `/ask` route (before line 521), add a small helper so `/ask` and `/explain` stay DRY:

```python
    def _maybe_fallback(question: str, answered_from_results: bool, fallback_enabled: bool):
        """Best-effort general-knowledge answer when the graph didn't answer.
        Returns a string, or None (disabled, answered, or the parametric call failed)."""
        if not fallback_enabled or answered_from_results:
            return None
        try:
            ans = nlcypher.general_knowledge_answer(question)
            return ans or None
        except Exception:
            return None
```

- [ ] **Step 4: Wire `/ask`**

Replace the body of `/ask` from the `res = adapter.run_query(...)` line (535) through the `return {...}` (537-538) with:

```python
            res = adapter.run_query(graph, cypher)
            fallback_enabled = payload.get("fallback", True)
            meta = nlcypher.synthesize(question, res["columns"], res["rows"],
                                       cypher=cypher, return_meta=True)
            answer = meta["answer"]
            answered = meta["answered_from_results"]
            fallback_answer = _maybe_fallback(question, answered, fallback_enabled)
            return {"question": question, "cypher": cypher, "columns": res["columns"],
                    "rows": res["rows"], "graph": res.get("graph", {}), "answer": answer,
                    "answered_from_results": answered, "fallback_answer": fallback_answer}
```

- [ ] **Step 5: Wire `/explain`**

In `/explain`, replace the final synth + return block (664-668) with:

```python
            q = focus or "Explain these results"
            fallback_enabled = payload.get("fallback", True)
            meta = nlcypher.synthesize(q, out_cols, out_rows,
                                       cypher=(join_sql if hydrated else cypher),
                                       return_meta=True)
            answer = meta["answer"]
            answered = meta["answered_from_results"]
            fallback_answer = _maybe_fallback(q, answered, fallback_enabled)
            return {"answer": answer, "join_sql": join_sql, "columns": out_cols,
                    "rows": out_rows, "hydrated": hydrated, "hydrate_from": hydrate_from,
                    "answered_from_results": answered, "fallback_answer": fallback_answer}
```

- [ ] **Step 6: Run the targeted tests, then the full backend suite**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_explain.py tests/test_app.py tests/test_explain_postjoin_banking.py -v`
Expected: all PASS (including the updated pre-existing explain tests).

Run: `cd backend && ./.venv/bin/python -m pytest tests/ -v`
Expected: all pass except pre-existing live-engine SKIPs. No NEW failures. (Note: `test_extract_ask_who_works_at_kinetica` is a known live Kinetica+LLM flake — if it fails in the full run, re-run it in isolation to confirm it passes.)

- [ ] **Step 7: Commit**

```bash
git add backend/xgraph_gateway/app.py backend/tests/test_explain.py backend/tests/test_app.py
git commit -m "feat(explain): /ask + /explain fallback to general knowledge (default on, per-request flag)"
```

---

### Task 3: Frontend `gateway.js` — `fallback` arg on `ask`/`explain`

**Files:**
- Modify: `frontend/gateway.js` (`ask` at 314, `explain` at 317)
- Test: `frontend/tests/test_client.mjs`

**Interfaces:**
- Consumes: nothing new.
- Produces: `client.ask(graph, question, fallback)` and `client.explain(question, columns, rows, cypher, source, graph, fallback)` — the trailing `fallback` is optional and defaults to `true` when omitted; it is added to the POST body as `fallback`.

- [ ] **Step 1: Write the failing test**

Add to `frontend/tests/test_client.mjs` (mirror the existing `fakeFetch`/body-capture style, e.g. the promote test at line 198):

```javascript
// --- ask/explain carry the fallback flag (default true) -------------------
{
  let askBody = null, explainBody = null;
  const fbClient = g.makeClient("http://gw", "falkordb", (url, opts) => {
    const body = opts && opts.body ? JSON.parse(opts.body) : null;
    if (url.indexOf("/ask") !== -1) askBody = body;
    if (url.indexOf("/explain") !== -1) explainBody = body;
    return { ok: true, json: async () => ({}) };
  });
  await fbClient.ask("g", "q");                       // omitted → default true
  assert(askBody.fallback === true, "ask defaults fallback to true");
  await fbClient.ask("g", "q", false);               // explicit false
  assert(askBody.fallback === false, "ask passes explicit fallback=false");
  await fbClient.explain("focus", ["NODE"], [["p1"]], "MATCH…", "src.parquet", "g");
  assert(explainBody.fallback === true, "explain defaults fallback to true");
  await fbClient.explain("focus", ["NODE"], [["p1"]], "MATCH…", "src.parquet", "g", false);
  assert(explainBody.fallback === false, "explain passes explicit fallback=false");
  console.log("ok: ask/explain fallback flag");
}
```

> Match the file's actual `makeClient` call shape and its `assert`/fake-fetch helpers — grep `test_client.mjs` for how `fakeFetch` returns (`{ json: async () => ... }`) and copy it. The signature order for `makeClient` (engine positional vs. session) also varies in this file — reuse the exact form the promote/other falkordb tests use.

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd frontend && node tests/test_client.mjs`
Expected: FAIL — `fallback` is `undefined` in the captured bodies.

- [ ] **Step 3: Add the `fallback` arg to both methods**

Replace line 314:

```javascript
      ask: function (graph, question, fallback) { return postJSONWithAuth("/ask", { graph: graph, question: question, fallback: fallback !== false }); },
```

Replace line 317:

```javascript
      explain: function (question, columns, rows, cypher, source, graph, fallback) { return postJSONWithAuth("/explain", { question: question, columns: columns, rows: rows, cypher: cypher, source: source, graph: graph, fallback: fallback !== false }); },
```

(`fallback !== false` maps `undefined`/`true` → `true` and only an explicit `false` → `false`, matching the backend default-on contract.)

- [ ] **Step 4: Run the client + transforms tests**

Run: `cd frontend && node tests/test_transforms.mjs && node tests/test_client.mjs`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/gateway.js frontend/tests/test_client.mjs
git commit -m "feat(explain): gateway.js ask/explain carry the fallback flag (default true)"
```

---

### Task 4: Frontend `XGraph.html` — Setup toggle + fallback blocks

**Files:**
- Modify: `frontend/XGraph.html` (App state near 8330; SetupPanel props at 6711-6717; LLM card header at 6829-6830; SetupPanel usage at 9961-9970; QueryPanel usage at 10072-10085; Ask render at 3661-3666; Explain render at 3998-4000; Ask handler at 1699-1714; Explain handler at 1721-1744; `EXPLORER_VERSION` at 50)

**Interfaces:**
- Consumes: `client.ask(graph, question, fallback)` and `client.explain(..., graph, fallback)` from Task 3; the existing `col`/`colTitle`/`radioLbl` styles and `Label` component in SetupPanel.
- Produces: an App-level `answerFallback` boolean (default `true`) threaded into `SetupPanel` (with its setter) and `QueryPanel` (read-only); a Setup toggle at the top of the LLM card; distinct "💡 General knowledge:" blocks under the Ask bubble and the Explain answer.

- [ ] **Step 1: Add the App-level state**

After line 8330 (the `llmConn` state), add:

```javascript
    // Answer-fallback toggle (Setup › LLM). When ON, /ask and /explain answer
    // from the model's own knowledge if the graph results don't — rendered as a
    // separate "💡 General knowledge" block. Sent per-request as `fallback`.
    const [answerFallback, setAnswerFallback] = useState(true);
```

- [ ] **Step 2: Thread the state into SetupPanel and QueryPanel**

In the `<SetupPanel ... />` usage (9961-9970), add a prop line (e.g. after `llmStatus={llmStatus}`):

```javascript
                        answerFallback={answerFallback} setAnswerFallback={setAnswerFallback}
```

In the `<QueryPanel ... />` usage (10072-10085), add after `engine={engine}` (line 10079):

```javascript
                                            answerFallback={answerFallback}
```

- [ ] **Step 3: Render the toggle at the top of the LLM card**

In `SetupPanel`, right after the LLM `colTitle` line (6830, `<div style={colTitle}>LLM</div>`), insert the toggle so it sits above the Provider picker:

```javascript
                <label style={{ display:'flex', alignItems:'flex-start', gap:8, cursor:'pointer', margin:'0 0 14px', fontSize:12, color:'#2d3436' }}>
                    <input type="checkbox" checked={props.answerFallback !== false}
                           onChange={function(e){ props.setAnswerFallback(e.target.checked); }}
                           style={{ marginTop:2 }} />
                    <span>Answer from model knowledge when the graph can't <span title="Ask/Explain will add a separate 💡 General knowledge block when the query results don't answer the question">💡</span></span>
                </label>
```

- [ ] **Step 4: Pass the flag from the Ask + Explain handlers**

In `handleAsk` (line 1704), pass the flag:

```javascript
            var res = await props.gwClient.ask(props.graphName, q, props.answerFallback);
```

Also extend `handleAsk` to keep the fallback text. Add a state hook next to `askAnswer` (after line 1697):

```javascript
    const [askFallback, setAskFallback] = useState(null);
```

Set it in the `try` (right after `setAskAnswer(...)` at 1711):

```javascript
            setAskFallback((res && res.fallback_answer) || null);
```

And clear it alongside `setAskAnswer(null)` at line 1702:

```javascript
        setAskBusy(true); setAskError(null); setAskAnswer(null); setAskFallback(null);
```

In `handleExplain` (1738-1740), pass the flag as the trailing arg:

```javascript
            var r = await props.gwClient.explain(
                explainFocus.trim(),
                cols, rws, sql, hydrateSource, props.graphName, props.answerFallback);
```

- [ ] **Step 5: Render the fallback blocks**

Ask side — after the `askAnswer` block (after line 3666, still inside the same parent that holds `{askAnswer && (...)}`), add:

```javascript
                        {askFallback && (
                            <div style={{ padding:'8px 12px', background:'#fffbea', border:'1px solid #f5e6a8', borderRadius:6, fontSize:12, color:'#2d3436', position:'relative' }}>
                                <div style={{ fontWeight:700, color:'#a67c00', marginBottom:2 }}>{'💡 General knowledge:'}</div>
                                {askFallback}
                            </div>
                        )}
```

Explain side — after the `explainResp.answer` block (after line 4000), add:

```javascript
                    {explainResp && explainResp.fallback_answer && (
                        <div style={{ padding:'8px 12px', background:'#fffbea', border:'1px solid #f5e6a8', borderRadius:6, fontSize:12, color:'#2d3436', marginTop:8 }}>
                            <div style={{ fontWeight:700, color:'#a67c00', marginBottom:2 }}>{'💡 General knowledge:'}</div>
                            {explainResp.fallback_answer}
                        </div>
                    )}
```

- [ ] **Step 6: Bump the version**

Change line 50 `const EXPLORER_VERSION = "0.22.3";` to `"0.23.0"` (a new user-facing feature).

- [ ] **Step 7: Validate the transpile + Node tests + server**

Run the transpile check:

```bash
cd frontend && awk '/type="text\/babel"/{f=1;next} f&&/^<\/script>/{f=0} f' XGraph.html | ./node_modules/.bin/esbuild --loader=jsx --jsx=transform --log-level=warning >/dev/null && echo "TRANSPILE OK"
```
Expected: `TRANSPILE OK`.

```bash
cd frontend && node tests/test_transforms.mjs && node tests/test_client.mjs
```
Expected: all PASS.

Then confirm the gateway serves the page (start it if needed per CLAUDE.md, `./xgraph restart`):

```bash
curl -s -o /dev/null -w '%{http_code}\n' localhost:8090/
```
Expected: `200`.

- [ ] **Step 8: Commit**

```bash
git add frontend/XGraph.html
git commit -m "feat(explain): Setup fallback toggle + 💡 General knowledge blocks (Ask/Explain); v0.23.0"
```

---

## Manual Acceptance (user-driven, the headline check)

Not automated — the real acceptance, run in the browser:

1. `./xgraph restart`, open `http://localhost:8090/`, Connect (any engine with an LLM route). Confirm the **Setup › LLM** card shows the new toggle at the top, checked (ON) by default.
2. Load a graph, run a query whose results do **not** contain an age (e.g. the national-cathedral events graph). In **Explain**, ask "How old is Lindsay Graham". Confirm: the grounded answer still says it can't be determined from the results, AND a separate **💡 General knowledge:** block gives the age.
3. In **Ask**, ask the same age question. Confirm the same separate block appears beneath the grounded bubble.
4. Turn the Setup toggle **OFF**, re-run either. Confirm NO 💡 block appears (grounded answer only).

---

## Self-Review

- **Spec coverage:** grounding flag detection (Task 1 `_ANSWER_SCHEMA` + prompt); `synthesize` backward-compat via `return_meta` default False (Task 1 Step 4 + regression test); `general_knowledge_answer` fast-tier parametric (Task 1 Step 5); empty-results ⇒ not answered (Task 1 empty-rows rule + test); endpoint flag `payload.get("fallback", True)` + response fields + best-effort (Task 2); disable toggle default-on frontend per-request flag (Task 3 `!== false` + Task 4 `answerFallback`); Setup toggle at top of LLM card (Task 4 Step 3); 💡 separate blocks in both Ask + Explain (Task 4 Step 5); version bump (Task 4 Step 6); testing incl. toggle-off (Task 2 `test_explain_toggle_off_skips_fallback`, Task 3 explicit-false).
- **Placeholder scan:** every code step carries real code. `_client()` (Task 2) and the `makeClient`/`assert`/`fakeFetch` shapes (Task 3) are flagged with explicit "use the file's actual X" notes because the exact local test-harness symbol must be copied verbatim from the neighboring tests — integration seams, not placeholders.
- **Type consistency:** `synthesize(..., return_meta=False)` → `str | {"answer": str, "answered_from_results": bool}` used identically in Tasks 1 & 2; `general_knowledge_answer(question, llm=None) -> str` (Task 1) called as `general_knowledge_answer(question)` (Task 2 helper); `fallback_answer: str | null` produced in Task 2 and consumed as `res.fallback_answer` / `explainResp.fallback_answer` (Task 4); `ask(graph, question, fallback)` / `explain(..., graph, fallback)` defined in Task 3 and called with `props.answerFallback` in Task 4.
- **Global-constraint carry:** the "no default-type change to `synthesize`" and "fallback default ON" constraints are enforced at three layers (backend default, gateway.js `!== false`, App state `true`) and each is independently tested.
