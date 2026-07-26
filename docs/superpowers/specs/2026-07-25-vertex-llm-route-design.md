# Design: UI-selectable LLM route (Vertex / API-key / CLI) + robust defaults

**Date:** 2026-07-25
**Status:** Approved (design), pending implementation plan
**Worktree/branch:** `vertex-llm-support`

## Problem

The "Ask" action (and every other LLM-backed path: Explain, NL→Cypher, synthesize,
extract) silently fails when the gateway process was launched from a shell that
does not carry the GCP Vertex environment. Root cause confirmed during diagnosis:

- `xgraph_gateway/llm.py::_llm()` resolves the backend as: `ANTHROPIC_API_KEY` set
  → anthropic SDK; else `claude` CLI on PATH; else error.
- With no API key it uses the `claude` CLI as a **subprocess that inherits the
  gateway's environment**.
- The running gateway (observed pid 337452) had **none** of
  `CLAUDE_CODE_USE_VERTEX / ANTHROPIC_VERTEX_PROJECT_ID / CLOUD_ML_REGION`, so the
  spawned `claude -p` had no auth route and failed.

A live `claude -p --output-format json` from a shell that *does* carry the vars
succeeds (`is_error:false`, model `claude-opus-4-8`), proving the mechanism. GCP
ADC is present at `~/.config/gcloud/application_default_credentials.json`
(`type: authorized_user`, `bkaramete@babelstreet.com`, `cloud-platform` scope,
quota project `team-warehouse-workhorses-dev`).

## Key architectural facts (settled during brainstorming)

- **The browser never calls the LLM and never touches credentials.** All LLM calls
  run server-side in the gateway. The browser only sends non-secret config (which
  route, project, region, model) and, for the API-key route, an API key over
  localhost.
- **Vertex auth is server-side ADC.** For Vertex the browser supplies only
  project/region; the `claude -p` (or SDK) process reads the ADC file on the server
  and authenticates itself. The browser's own Google login is irrelevant.
- **Two orthogonal axes**, not three fixed routes: *Mechanism* (how we call Claude)
  is independent of *Auth* (where it authenticates). "API key + CLI" is a valid
  combination.

## Config model

Two axes:

- **Mechanism**: `cli` (default) | `sdk`
- **Auth**: `vertex` | `apikey` | `cli-login`

Fields (only the relevant subset applies per combo):

| Field     | Applies to        | Default source                                   |
|-----------|-------------------|--------------------------------------------------|
| `project` | vertex            | `ANTHROPIC_VERTEX_PROJECT_ID` (`team-warehouse-workhorses-dev`) |
| `region`  | vertex            | `CLOUD_ML_REGION` (`global`)                      |
| `api_key` | apikey            | `ANTHROPIC_API_KEY` (usually unset)              |
| `model`   | all               | `ANTHROPIC_DEFAULT_OPUS_MODEL` / `XGRAPH_LLM_MODEL` (`claude-opus-4-8`) |

Valid combinations (others disabled in the UI):

- `cli` + `vertex`  — works today; the default
- `cli` + `apikey`  — CLI with `ANTHROPIC_API_KEY` set for the subprocess
- `cli` + `cli-login` — CLI using its own stored login/subscription creds
- `sdk` + `apikey`  — `anthropic.Anthropic(api_key=...)`
- `sdk` + `vertex`  — `anthropic.AnthropicVertex(project, region)`

**Scope:** the runtime config is **gateway-global** (single-user localhost). No
per-request/per-session threading through `nlcypher`. Approved.

## Components

### 1. `xgraph_gateway/llm.py` — config holder + resolver (core change)

- Module-level override store:
  - `set_llm_config(cfg: dict) -> dict` — replace/merge the runtime override; returns
    the effective config.
  - `get_llm_config() -> dict` — the raw override (may be empty).
  - `resolve_llm_config() -> dict` — merge **override > environment > defaults** into
    one effective config, plus a `sources` map (`{field: "override"|"env"|"default"}`)
    for diagnostics. Never includes the raw api_key in any diagnostic output.
- `_llm(prompt, *, schema=None, model=None)` — unchanged signature — consults
  `resolve_llm_config()`:
  - **CLI**: build an explicit subprocess `env` (copy of `os.environ` plus the
    resolved auth vars) and pass `env=` to `subprocess.run` — no longer relies on
    ambient env. vertex → set `CLAUDE_CODE_USE_VERTEX=1` + project + region; apikey
    → set `ANTHROPIC_API_KEY`; cli-login → add nothing.
  - **SDK**: vertex → `anthropic.AnthropicVertex(project_id=..., region=...)`; apikey
    → `anthropic.Anthropic(api_key=...)`.
- The lazily-bound `_get_llm()` bindings in `nlcypher.py`, `extract.py`,
  `extract_fold.py` are **untouched**.

### 2. `xgraph_gateway/app.py` — startup `.env` + endpoints

- **Startup**: load `backend/.env` via `python-dotenv` (already a dependency) so
  Vertex is the default out of the box regardless of launching shell — the permanent
  fix for the silent failure. `.env` stays git-ignored; add `backend/.env.example`
  documenting the Vertex vars.
- `POST /llm_config` — body is the two-axis config; calls `set_llm_config`; returns
  the effective status (same shape as `/llm_status`).
- `GET /llm_status` — returns `resolve_llm_config()` as: `mechanism`, `auth`,
  `project`, `region`, `model`, `has_api_key` (bool — **never the key**), and
  `sources`. Powers the UI's current-state display and pre-fill.

### 3. `frontend/XGraph.html` + `frontend/gateway.js` — LLM box

- A small **"LLM" settings box in the Setup panel**, near the engine picker:
  - Two dropdowns (Mechanism, Auth), conditional fields (project/region for vertex;
    api key for apikey; model always), an **Apply** button, and a status line.
  - On load and after Apply, call `GET /llm_status` and render e.g.
    "claude CLI · Vertex · project team-warehouse-… · claude-opus-4-8".
- `gateway.js`: add `getLlmStatus()` and `setLlmConfig(cfg)` client methods.
- Editing discipline: anchored search-and-replace against verbatim strings; validate
  with the Babel transpile + a `curl` 200; real behavior deferred to the browser.

## Data flow (Vertex route)

```
Browser (Setup > LLM box)                Gateway (server, :8090)
  Apply { mechanism:cli, auth:vertex, ── POST /llm_config ──▶ set_llm_config(...)
          project, region, model }                            └ stored global override
  status line  ◀──────────────────────── GET /llm_status ──── resolve_llm_config()

  Ask question ───────── POST /ask ─────▶ nlcypher → _get_llm() → _llm()
                                            └ resolve_llm_config() → build subprocess
                                              env (CLAUDE_CODE_USE_VERTEX=1, project,
                                              region) → `claude -p` reads server ADC
  answer JSON  ◀──────────────────────────────────────────────── Vertex response
```

## Error handling

- Invalid combo posted to `/llm_config` → 400 via the existing error envelope
  `{"error":{code,message,...}}`.
- apikey auth selected with no key provided and none in env → 400 with a clear
  message ("API key required for auth=apikey").
- LLM call failures continue to surface through the existing `_err(...)` envelope on
  `/ask` etc.; the message already includes the `claude -p` stderr tail.

## Testing

- `llm.py` (unit, no real LLM): `resolve_llm_config()` precedence
  (override > env > default) and `sources` map; `_llm` builds the correct subprocess
  `env` per combo (fake `subprocess.run`) and selects the correct SDK client (fake
  `anthropic` module). Assert api_key never leaks into diagnostics.
- `app.py` (FakeAdapter harness): `/llm_config` + `/llm_status` round-trip; assert
  `has_api_key` bool and that the raw key is never returned; invalid-combo → 400.
- `gateway.js` (Node, injected fake `fetch`): `getLlmStatus()` / `setLlmConfig()`.
- Frontend React: Babel transpile syntax check + `curl` 200; behavior verified in
  browser by the user.

## Security notes

- API keys entered in the browser travel to the gateway in plaintext — acceptable on
  localhost only; flagged as not-for-remote deployment.
- `/llm_status` never returns the API key (bool presence only). Logs never print it.
- Vertex uses server-side ADC (currently the user credential
  `bkaramete@babelstreet.com`). For a shared/remote deployment a service account is
  more durable (user ADC can expire if the refresh token is revoked); out of scope
  for this localhost change.

## Out of scope / deferred

- Per-request or per-session LLM config (kept gateway-global).
- Remote-deployment credential story (service account, secret storage).
- Persisting UI-set config across gateway restarts (restart falls back to `.env`
  defaults; UI override is in-memory only).

## Constraints honored

- No `git commit` under `xgraph/` (per CLAUDE.md) — this spec is written, not
  committed.
- Self-contained backend; no new imports from sibling projects. `python-dotenv` and
  `anthropic` are already dependencies (`anthropic[vertex]` extra may be needed for
  `AnthropicVertex` — verified in the plan).
