# Explain/Ask LLM-Knowledge Fallback — Design

**Date:** 2026-07-28
**Status:** Approved (design); ready for implementation plan.
**Scope:** Backend `nlcypher.synthesize` path (shared by `/ask` and `/explain`) + frontend
Ask bubble and Explain panel.

## Problem

The `/explain` and `/ask` answers are produced by `nlcypher.synthesize()`, whose prompt is
**deliberately grounded**: "explain WHAT THE RESULTS MEAN … do not invent facts." So when a
question asks something the graph results don't contain — e.g. "How old is Lindsay Graham" against
a graph that only records a memorial-service relationship — the answer is a dead end: "the age
cannot be determined from these results."

The user wants a **fallback**: when the graph results cannot answer the question, answer it from the
model's own general knowledge instead — clearly separated from the grounded, data-derived answer so
provenance is never blurred.

## Goal

Add a two-tier answer: (1) the existing grounded answer from the graph results, and (2) when the
results don't answer the question, a **clearly-separated** answer drawn from the model's general
(parametric) knowledge. Applies to both the Ask avenue and the Explain avenue.

## Non-goals (deferred)

- **Blending** graph facts and model knowledge into one answer. The two are always separate blocks
  (rejected during brainstorming as too risky for a provenance tool).
- **Citations / web lookup** for the parametric answer. It is the model's own knowledge, labeled as
  such; no external retrieval.
- **Changing `synthesize`'s default return type.** Existing callers keep the string return.

## Key decisions (locked during brainstorming)

| Decision | Choice |
|---|---|
| Fallback UX | Automatic, but rendered as a clearly-separated second block |
| Enable toggle | A Setup switch at the **top of the LLM card**, **default ON**; a frontend preference sent per-request (not gateway-global) |
| Detection | The grounded LLM call self-reports `answered_from_results: bool` (not text-matching) |
| Scope | Both Ask and Explain (both call `synthesize`) |
| Empty results | Treated as "not answered" → triggers the fallback |
| Model tier | Fast Haiku (same tier Ask/Explain already use) |
| Disclaimer copy | Light label: **"💡 General knowledge:"** in a visually-distinct block |
| Parametric "don't know" | If the model doesn't know, it says so plainly (no invention) |

## Design

### Detection: a self-reported grounding flag

`synthesize()`'s answer schema (`_ANSWER_SCHEMA`) gains a boolean `answered_from_results`. The
prompt instructs the model: set it **false** when the result rows do not contain what the question
asked (including when the results are empty); **true** when the grounded answer genuinely comes from
the results. This is robust to phrasing — no scraping the answer text for "cannot be determined".

### `synthesize` return shape (backward-compatible)

`synthesize(question, columns, rows, llm=None, cypher=None, return_meta=False)`:

- `return_meta=False` (default) → returns the grounded answer **string**, exactly as today. Existing
  callers (`/ask` line ~536, `/synthesize` endpoint, `/explain`) and their tests are unaffected
  unless they opt in.
- `return_meta=True` → returns `{"answer": str, "answered_from_results": bool}`.

### New parametric answer function

`general_knowledge_answer(question: str, llm=None) -> str` in `nlcypher.py`. Fast-tier LLM call.
Prompt (essence):

> The user asked a question that their graph query results did not answer. Using your own general
> knowledge, answer the question directly and concisely. If you do not know, say so plainly. Do NOT
> claim this information came from their data or graph.

Returns the plain-text answer. Its own JSON schema with a single `answer` field (mirrors
`_ANSWER_SCHEMA`), parsed the same way `synthesize` parses its output.

### Endpoint changes

Both `/explain` and `/ask` (in `app.py`):

1. Read the enable flag: `fallback_enabled = payload.get("fallback", True)` — **default True** when the
   field is absent, so any API caller (and older frontends) get the fallback unless it is explicitly
   turned off.
2. Call `synthesize(..., return_meta=True)` to get `{answer, answered_from_results}`.
3. If `fallback_enabled` **and** `answered_from_results` is false, call
   `general_knowledge_answer(question)` → `fallback_answer`. Otherwise `fallback_answer = None`.
4. Add two fields to the JSON response, keeping all existing fields:
   - `answered_from_results: bool`
   - `fallback_answer: str | null`

`/explain` response today: `{answer, join_sql, columns, rows, hydrated, hydrate_from}` → gains
`answered_from_results`, `fallback_answer`.
`/ask` response today includes `answer`, `cypher`, and result fields → gains the same two.

The parametric call is wrapped so a failure never breaks the grounded answer (best-effort:
`fallback_answer` stays null on error).

### Frontend

`gateway.js`: `ask()` and `explain()` gain a trailing optional `fallback` boolean that is added to the
request body (defaulting to `true` when the caller omits it, matching the backend default). The
response JSON already passes through raw, so the new response fields need no client change; add Node
coverage that the request body carries `fallback` and that the response fields survive.

`XGraph.html`:

- **Setup toggle:** an App-level state `answerFallback` (boolean, **default `true`**). `SetupPanel`
  renders it as a labeled on/off switch (checkbox) at the **top of the LLM card**, above the Mechanism
  picker — e.g. "Answer from model knowledge when the graph can't (💡 fallback)". Passed to `SetupPanel`
  via props (`answerFallback` + `setAnswerFallback`) and down to `QueryPanel` (`answerFallback`).
- **Explain panel:** after the grounded answer, if `explainResp.fallback_answer` is truthy, render a
  visually-distinct block (tinted background, its own border) labeled **"💡 General knowledge:"**
  followed by the fallback text. The Explain call passes `props.answerFallback` as the `fallback` arg.
- **Ask bubble:** the Ask answer state (`askAnswer`) is currently a string. Extend the Ask handler to
  keep the fallback too (a parallel `askFallback` state) and render the same distinct block beneath the
  grounded bubble. The Ask call passes `props.answerFallback` as the `fallback` arg.
- Bump `EXPLORER_VERSION`.

The block styling matches the file's existing inline-style conventions; the label is plain text with
the 💡 emoji — no new dependency. When `answerFallback` is off, the client sends `fallback:false`, the
backend skips the parametric call, and no block renders.

### Data flow

```
Setup: [💡 Answer from model knowledge] switch (default ON) → answerFallback (App state)
        └─ sent as `fallback` in each /ask and /explain request

Ask:  question → nl2cypher → run → synthesize(return_meta=True)
                                     │
                       fallback && !answered_from_results?
                        ├─ no  → grounded bubble only
                        └─ yes → grounded bubble + general_knowledge_answer() block

Explain: focus + results (+post-join hydrate) → synthesize(return_meta=True)
                                     │  (same branch as above)
```

## Endpoint contract (additions only)

`POST /explain` and `POST /ask` **requests** each gain an optional field:

```json
{ "fallback": true }
```

- `fallback` — enable the model-knowledge fallback. **Absent ⇒ true** (default on).

Their **responses** each gain:

```json
{
  "answered_from_results": false,
  "fallback_answer": "Lindsey Graham is 70 (born July 9, 1955)."
}
```

- `answered_from_results` — did the grounded answer come from the result rows?
- `fallback_answer` — the model-knowledge answer, present (non-null) only when `fallback` is on,
  `answered_from_results` is false, and the parametric call succeeded; otherwise `null`.

## Testing

- **Unit (nlcypher):** `synthesize(return_meta=True)` returns the `answered_from_results` flag from a
  fake LLM; `synthesize()` with default args still returns a string (regression). New
  `general_knowledge_answer()` returns the fake LLM's `answer` string.
- **Gateway (Fake adapter + fake LLM):** with a fake LLM whose grounded call reports
  `answered_from_results=false`, `/explain` and `/ask` return a non-null `fallback_answer`; with
  `true`, `fallback_answer` is null and no second call is made. With `fallback:false` in the request
  body and a grounded flag of false, `fallback_answer` is still null and the parametric call is
  skipped (toggle honored).
- **Frontend:** `gateway.js` Node test asserts `ask`/`explain` put `fallback` in the request body
  (and default it to `true` when omitted) and that the new response fields pass through. The React
  render + Setup toggle are validated by transpile + curl 200; the block behavior is browser-verified
  by the user.

## Alternatives considered

- **Text-match the grounded answer** for "cannot be determined" phrases → rejected: brittle, misses
  paraphrases and other languages.
- **Explicit second button** ("Answer from model knowledge") → considered; user chose automatic +
  separated for fewer clicks while preserving provenance.
- **Single blended answer** → rejected: blurs sourced vs. parametric facts, unacceptable for a
  data-provenance tool.

## Risks

- **Hallucination:** the parametric answer can be wrong. Mitigated by the separate labeled block
  ("💡 General knowledge:") and the "say so if you don't know" instruction. It is never presented as
  graph-derived.
- **Latency:** one extra fast-tier call, and only when the graph didn't answer. Acceptable.
- **False negatives/positives on the flag:** the model may occasionally misjudge whether the results
  answered the question. Acceptable — worst case is an unnecessary (clearly-labeled) fallback block or
  its absence; neither corrupts grounded data.
