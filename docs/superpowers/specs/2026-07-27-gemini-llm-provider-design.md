# Gemini LLM provider — Design

**Date:** 2026-07-27
**Status:** Approved (design); ready for implementation plan
**Scope:** `backend/xgraph_gateway/llm.py` (+ `requirements.txt`, tests) and the
frontend Setup picker (`frontend/XGraph.html`, `frontend/gateway.js`). No changes
to `extract`, `extract_fold`, `nlcypher`, or any consumer of the LLM callable.

## Goal

Add **Google Gemini** as a selectable LLM provider alongside Claude, including the
*freely available* Google AI Studio API-key path and the Vertex Gemini path. A
single **global provider** applies to all tiers (no per-tier provider mixing). The
choice is cached so subsequent runs don't have to reconfigure.

## Why this is cheap

Every LLM consumer (`extract`, `extract_fold`, `nlcypher`) goes through one
callable — `_llm(prompt, schema, model)` — obtained via a lazy `_get_llm()`.
Nothing outside `llm.py` knows the vendor. Adding Gemini is a new dispatch branch
in `_llm()` plus config plumbing; consumers are untouched. The Setup picker
already renders a mechanism × auth route with model + API-key/Vertex fields and
applies it on Connect, so the UI change is additive.

## Provider axis

A new **`provider`** axis sits above the existing mechanism × auth:

- `provider ∈ {anthropic, gemini}`; **default `anthropic`** (backward-compatible —
  existing behavior, env, `.env`, and all current tests are unaffected).
- Resolution mirrors the other fields: override > `XGRAPH_LLM_PROVIDER` env >
  default `anthropic`.

### Per-provider valid routes

| provider | mechanism | auth | notes |
|---|---|---|---|
| anthropic | cli | vertex **(default)**, apikey, cli-login | unchanged |
| anthropic | sdk | vertex, apikey | unchanged |
| gemini | sdk **(only)** | apikey **(default)**, vertex | Gemini has no CLI |

`gemini` is **SDK-only** — there is no `claude`-CLI equivalent, so the mechanism is
fixed to `sdk` and any `(gemini, cli)` route is rejected by validation.

### Per-provider default models (both editable/overridable)

| provider | Build (`model`) | Ask/Explain (`fast_model`) |
|---|---|---|
| anthropic | `claude-opus-4-8` | `claude-haiku-4-5-20251001` |
| gemini | `gemini-2.5-pro` | `gemini-2.5-flash` |

- `model` (Build tier: extract/fold) resolves override > `XGRAPH_LLM_MODEL` env >
  provider default.
- `fast_model` (Ask/Explain: nl2cypher, synthesize, join-SQL) becomes
  **overridable** (currently env-only): override > `XGRAPH_LLM_FAST_MODEL` env >
  provider default. This lets the picker edit both tiers.

## Backend changes (`llm.py`)

1. **Config shape.** Add `provider` and `fast_model` to `_ALLOWED_KEYS`. Replace the
   flat `_DEFAULT_MODEL`/`_FAST_MODEL` with a provider-keyed table:
   ```python
   _PROVIDER_DEFAULTS = {
       "anthropic": {"model": "claude-opus-4-8",  "fast": "claude-haiku-4-5-20251001"},
       "gemini":    {"model": "gemini-2.5-pro",   "fast": "gemini-2.5-flash"},
   }
   _DEFAULT_PROVIDER = "anthropic"
   ```
2. **`resolve_llm_config()`** resolves `provider` first, then derives model/fast
   defaults from `_PROVIDER_DEFAULTS[provider]`. Auth + api-key inference is
   provider-aware:
   - anthropic: `CLAUDE_CODE_USE_VERTEX` / `ANTHROPIC_API_KEY` (as today).
   - gemini: `GOOGLE_GENAI_USE_VERTEXAI` → vertex; else `GEMINI_API_KEY` /
     `GOOGLE_API_KEY` → apikey; default apikey.
   Returned dict gains `provider` and `fast_model`.
3. **`fast_model()`** becomes provider-aware (reads resolved provider) instead of a
   bare constant.
4. **`validate_llm_config()`** enforces the per-provider route table above and the
   existing field requirements (api_key for apikey, project for vertex).
5. **Dispatch.** In `_llm()`, branch on `cfg["provider"]`: `gemini` →
   `_llm_gemini(...)`; else the existing cli/sdk Claude dispatch.
6. **`_llm_gemini(prompt, schema, model, cfg)`** using the `google-genai` SDK:
   ```python
   from google import genai
   from google.genai import types
   if cfg["auth"] == "vertex":
       client = genai.Client(vertexai=True, project=cfg["project"], location=cfg["region"])
   else:
       client = genai.Client(api_key=cfg["api_key"])
   config = types.GenerateContentConfig(response_mime_type="application/json") if schema else None
   resp = client.models.generate_content(model=m, contents=prompt, config=config)
   ```
   Structured output uses `response_mime_type="application/json"` plus the **same
   regex/JSON-extract fallback** the Claude SDK path already uses — this sidesteps
   Gemini's `response_schema` dialect differences (no `$ref`, restricted keywords)
   while still returning a parsed dict. Gemini model IDs are plain (no `@date`
   translation — that stays Anthropic-Vertex-only).
7. **`llm_status()`** gains `provider` in its safe projection (still never returns
   `api_key`).
8. **`warmup()`** needs no change — it fires `_llm` per resolved model tier, which
   is now the Gemini pair when provider=gemini.
9. **`requirements.txt`** gains `google-genai` (a light dependency; the Vertex path
   reuses the `google-auth` already pulled by `anthropic[vertex]`).

## Frontend changes

### Picker (Provider-first, axes kept)

Add a **Provider** `<select>` (Claude | Gemini) at the top of the existing LLM
route block in the Setup panel. Below it:

- **Claude selected:** the current mechanism × auth UI is kept verbatim — all five
  combos (cli×{vertex,apikey,cli-login}, sdk×{vertex,apikey}) remain. Nothing is
  removed.
- **Gemini selected:** the mechanism control is hidden (SDK is implied/fixed);
  Auth offers **Google API key | Vertex**. API key → a key field; Vertex →
  project/region fields.
- **Both tiers editable:** show a **Build** model field and an **Ask/Explain**
  (fast) model field, prefilled from the active provider's defaults.

**Default provider** is `anthropic` (matches backend). **Switching provider
auto-loads that provider's default route** (mechanism/auth) and default model
tiers, so the user never configures from an empty state. The route is applied by
**Connect** (folded into `handleConnectAxes`, as today — no separate Apply).

Footer status line extends to show provider, e.g.
`LLM: gemini · apikey · Build=gemini-2.5-pro · Ask/Explain=gemini-2.5-flash`.

### `gateway.js`

`setLlmConfig` already passes an arbitrary config object through to `/llm_config`;
`handleConnectAxes` extends the sent object with `provider` and `fast_model`.
`getLlmStatus` needs no change (returns whatever the backend projects, now
including `provider`).

### Caching the choice

Persist the picker selection to browser **`localStorage`** under a dedicated key
(e.g. `xgraph.llmRoute`): `provider, mechanism, auth, project, region, model,
fast_model, api_key`. On app load, hydrate the picker from it; on Connect, apply
it. This covers page reloads *and* gateway restarts (the browser re-applies on
reconnect, re-seeding the in-memory backend override).

**API-key tradeoff (explicit):** the key is cached in `localStorage` so the user
doesn't re-enter it. This is acceptable for a localhost dev workbench — the key is
never written server-side beyond the in-memory override, and `/llm_status` still
never returns it. Documented here so it is a conscious choice, not an accident.

## Testing

- **Backend (pytest, fake-injection — no live calls):**
  - `resolve_llm_config` / `validate_llm_config`: default provider is anthropic;
    provider=gemini forces sdk (cli rejected); per-provider default models resolve
    correctly; override > env > default precedence for `provider` and `fast_model`;
    vertex requires project, apikey requires key.
  - `_llm_gemini`: with a **mocked `google-genai` client**, the schema path returns
    a parsed dict (via the JSON-extract fallback) and the text path returns the
    string; apikey vs vertex client construction is exercised.
  - `llm_status` includes `provider` and still omits `api_key`.
  - Existing Claude tests remain green (backward-compat guard).
- **Frontend:** Babel/JSX transpile gate; `gateway.js` Node test that the config
  object sent to `/llm_config` includes `provider` and `fast_model`.
- The React picker itself is browser-verified by the user (no headless runtime).

## Out of scope

- **Per-tier provider mixing** (e.g. Gemini fast + Claude build) — explicitly a
  single global provider.
- **Local/open Google models** (Gemma via Ollama etc.) — a separate, larger lift;
  this task covers the Gemini API (AI Studio + Vertex) only.
- Server-side persistence of the route to `.env`/disk — caching is client-side.
- Any change to consumers or to the Connect flow beyond adding the two fields.
