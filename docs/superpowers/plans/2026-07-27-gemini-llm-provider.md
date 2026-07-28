# Gemini LLM provider — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add **Google Gemini** as a selectable, global LLM provider alongside Claude — including the free Google AI Studio API-key path and Vertex Gemini — with the choice cached client-side so it survives reloads.

**Architecture:** A new `provider ∈ {anthropic, gemini}` axis sits above the existing mechanism × auth route in `backend/xgraph_gateway/llm.py`. Every LLM consumer already routes through one callable (`_llm(prompt, schema, model)`), so Gemini is a new dispatch branch (`_llm_gemini`) plus config plumbing — consumers (`extract`, `extract_fold`, `nlcypher`) are untouched. The Setup picker gains a Provider select that switches the axes below it and auto-loads that provider's default route; the picker selection is persisted to `localStorage`.

**Tech Stack:** Python/FastAPI backend (`google-genai` SDK for Gemini), single-file React 18 UMD + Babel-standalone frontend (`frontend/XGraph.html`, no build step), `frontend/node_modules/.bin/esbuild` for the transpile gate.

**Design spec (source of truth):** `docs/superpowers/specs/2026-07-27-gemini-llm-provider-design.md`.

## Global Constraints

- **No `git commit` anywhere under `xgraph/`** (repo rule, `CLAUDE.md`). Write files; never stage or commit. Each task ends with a test/validation checkpoint, not a commit.
- **Backward-compatible:** default provider is `anthropic`; existing behavior, env, `backend/.env`, and **all current tests stay green**. `fast_model()` still returns `claude-haiku-4-5-20251001` for the anthropic default (asserted by `test_nlcypher.py:295`, `test_extract.py:299`).
- **Consumers untouched:** no edits to `extract.py`, `extract_fold.py`, `nlcypher.py`, or `app.py`. The `/llm_config` endpoint already forwards its payload to `set_llm_config`, which filters by `_ALLOWED_KEYS` — adding keys there is the only backend gate needed.
- **Single global provider — no per-tier mixing.** One provider applies to both the Build and Ask/Explain tiers.
- **Per-provider default models** (both tiers editable/overridable): anthropic `claude-opus-4-8` / `claude-haiku-4-5-20251001`; gemini `gemini-2.5-pro` / `gemini-2.5-flash`.
- **Per-provider valid routes:** anthropic = `{(cli,vertex),(cli,apikey),(cli,cli-login),(sdk,apikey),(sdk,vertex)}` (all five kept); gemini = `{(sdk,apikey),(sdk,vertex)}` (SDK-only — any `(gemini, cli)` route is rejected).
- **Resolution precedence** for every field: override > env > provider default.
- Frontend edits to the ~10,000-line `XGraph.html` are **anchored search-and-replace against verbatim code strings** (line numbers shift as edits land — match on the string, not the number). Follow the file's style: `var` locals in components, inline style objects, `function(){}` (not arrow) JSX callbacks.
- The React app cannot be runtime-verified headlessly. The automated frontend gate is the Babel/JSX transpile check below; real behavior is browser-verified by the user.
- **API-key caching tradeoff (accepted):** the key is cached in `localStorage` for dev convenience; it is never written server-side beyond the in-memory override, and `/llm_status` never returns it.

### Transpile check (the frontend "run the test" step)

Run from `frontend/`:

```bash
awk '/type="text\/babel"/{f=1;next} f&&/^<\/script>/{f=0} f' XGraph.html \
  | ./node_modules/.bin/esbuild --loader=jsx --jsx=transform --log-level=warning >/dev/null \
  && echo "TRANSPILE OK"
```

Expected on success: prints `TRANSPILE OK`, exits 0. On a syntax error esbuild prints the error with line/column and exits 1.

### Backend test command

Run from `backend/`:

```bash
./.venv/bin/python -m pytest tests/ -q
```

---

## File Structure

- **Modify** `backend/xgraph_gateway/llm.py` — the provider axis lives here: `_PROVIDER_DEFAULTS` + `_DEFAULT_PROVIDER`, `_VALID_COMBOS` (provider-keyed), `_ALLOWED_KEYS` (+`provider`,`fast_model`), provider-aware `resolve_llm_config` / `fast_model` / `validate_llm_config` / `llm_status`, the `_llm` dispatch branch, and the new `_llm_gemini`.
- **Modify** `backend/requirements.txt` — add `google-genai`.
- **Modify** `backend/tests/test_llm_config.py` — provider resolution/validation/status tests; extend the env-reset fixture with the Gemini/provider env vars.
- **Modify** `backend/tests/test_llm_dispatch.py` — mocked-client Gemini dispatch test; extend the env-reset fixture.
- **Modify** `frontend/gateway.js` — no code change (already forwards arbitrary cfg); a regression test guards the pass-through.
- **Modify** `frontend/tests/test_client.mjs` — assert `setLlmConfig` forwards `provider` + `fast_model` verbatim.
- **Modify** `frontend/XGraph.html` — module consts (`LLM_PROVIDER_DEFAULTS`, `loadLlmRoute` helper), `App` `llmConn` init + `/llm_status` seed + `handleConnectAxes` (send `provider`/`fast_model`, persist to `localStorage`), the SetupPanel LLM picker (Provider select, gemini-aware mechanism/auth/model fields, `selectLlmProvider`), and both footer status lines (Setup panel + App status bar).

---

## Task 1: Backend provider axis — config, resolution, validation, status

Introduces the `provider` axis and per-provider defaults in `llm.py`, makes resolution/validation/status provider-aware, and makes `fast_model` overridable. No dispatch changes yet (Task 2). After this task, `provider=gemini` resolves and validates correctly and `llm_status()` reports it, while every existing anthropic test stays green.

**Files:**
- Modify: `backend/xgraph_gateway/llm.py`
- Modify: `backend/tests/test_llm_config.py`

**Interfaces:**
- Produces (used by Task 2 dispatch + frontend): resolved config dict now contains `provider` (str) and `fast_model` (str) alongside existing `mechanism, auth, project, region, model, api_key, sources`. `_PROVIDER_DEFAULTS: dict[str, {"model":str,"fast":str}]`, `_DEFAULT_PROVIDER: str = "anthropic"`, `_VALID_COMBOS: dict[str, set[tuple[str,str]]]`.
- Consumes (existing): `_OVERRIDE`, `_env_truthy`, `set_llm_config`, `resolve_llm_config`, `validate_llm_config`, `llm_status`, `fast_model`.

- [ ] **Step 1: Write the failing tests**

In `backend/tests/test_llm_config.py`, first extend the autouse `_reset` fixture so the new provider/Gemini env vars can't leak in from a developer's shell. Find this verbatim block:

```python
    for k in ("CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_API_KEY", "ANTHROPIC_VERTEX_PROJECT_ID",
              "CLOUD_ML_REGION", "XGRAPH_LLM_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
              "XGRAPH_LLM_MECHANISM"):
```

Replace it with:

```python
    for k in ("CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_API_KEY", "ANTHROPIC_VERTEX_PROJECT_ID",
              "CLOUD_ML_REGION", "XGRAPH_LLM_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
              "XGRAPH_LLM_MECHANISM", "XGRAPH_LLM_PROVIDER", "XGRAPH_LLM_FAST_MODEL",
              "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI",
              "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"):
```

Then append these tests to the end of `backend/tests/test_llm_config.py`:

```python
def test_default_provider_is_anthropic():
    cfg = llm.resolve_llm_config()
    assert cfg["provider"] == "anthropic"
    assert cfg["sources"]["provider"] == "default"
    # backward-compat: anthropic tiers unchanged
    assert cfg["model"] == "claude-opus-4-8"
    assert cfg["fast_model"] == "claude-haiku-4-5-20251001"


def test_gemini_provider_defaults_and_forces_sdk():
    eff = llm.set_llm_config({"provider": "gemini", "auth": "apikey", "api_key": "AIza-x"})
    assert eff["provider"] == "gemini"
    assert eff["mechanism"] == "sdk"                  # gemini is SDK-only by default
    assert eff["model"] == "gemini-2.5-pro"
    assert eff["fast_model"] == "gemini-2.5-flash"
    assert llm.fast_model() == "gemini-2.5-flash"     # fast tier is provider-aware


def test_gemini_cli_route_rejected():
    with pytest.raises(ValueError):
        llm.set_llm_config({"provider": "gemini", "mechanism": "cli", "auth": "apikey", "api_key": "k"})


def test_unknown_provider_rejected():
    with pytest.raises(ValueError):
        llm.set_llm_config({"provider": "bogus", "auth": "apikey", "api_key": "k"})


def test_provider_env_and_override_precedence(monkeypatch):
    monkeypatch.setenv("XGRAPH_LLM_PROVIDER", "gemini")
    cfg = llm.resolve_llm_config()
    assert cfg["provider"] == "gemini"
    assert cfg["sources"]["provider"] == "env"
    # override beats env
    eff = llm.set_llm_config({"provider": "anthropic"})
    assert eff["provider"] == "anthropic"
    assert llm.resolve_llm_config()["sources"]["provider"] == "override"


def test_gemini_env_auth_inference(monkeypatch):
    monkeypatch.setenv("XGRAPH_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-env")
    cfg = llm.resolve_llm_config()
    assert cfg["auth"] == "apikey"
    assert cfg["api_key"] == "AIza-env"
    assert cfg["sources"]["auth"] == "env"


def test_gemini_vertex_requires_project():
    with pytest.raises(ValueError):
        llm.set_llm_config({"provider": "gemini", "auth": "vertex"})  # no project
    eff = llm.set_llm_config({"provider": "gemini", "auth": "vertex", "project": "proj-g", "region": "us-central1"})
    assert eff["auth"] == "vertex" and eff["project"] == "proj-g"


def test_fast_model_override_beats_provider_default():
    llm.set_llm_config({"provider": "gemini", "auth": "apikey", "api_key": "k", "fast_model": "gemini-2.5-flash-lite"})
    assert llm.fast_model() == "gemini-2.5-flash-lite"


def test_status_includes_provider_and_hides_key():
    llm.set_llm_config({"provider": "gemini", "auth": "apikey", "api_key": "AIza-secret"})
    st = llm.llm_status()
    assert st["provider"] == "gemini"
    assert st["has_api_key"] is True
    assert "api_key" not in st
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_llm_config.py -q` (from `backend/`)
Expected: the new tests FAIL (e.g. `KeyError: 'provider'` / `ValueError` not raised), existing tests still pass.

- [ ] **Step 3: Add the provider defaults + valid-combo table**

In `backend/xgraph_gateway/llm.py`, find this verbatim block:

```python
_ALLOWED_KEYS = ("mechanism", "auth", "project", "region", "model", "api_key")
VALID_COMBOS = {
    ("cli", "vertex"), ("cli", "apikey"), ("cli", "cli-login"),
    ("sdk", "apikey"), ("sdk", "vertex"),
}
# The general/default model — used for the heavier work (extraction/building,
# and any call that doesn't ask for the fast tier). Override with XGRAPH_LLM_MODEL.
_DEFAULT_MODEL = "claude-opus-4-8"
# The fast tier — for the light interactive Query/Explain calls (nl2cypher,
# synthesize, join-SQL). Override with XGRAPH_LLM_FAST_MODEL.
_FAST_MODEL = "claude-haiku-4-5-20251001"


def fast_model() -> str:
    """Model for light, latency-sensitive interactive calls (ask/explain)."""
    return os.environ.get("XGRAPH_LLM_FAST_MODEL") or _FAST_MODEL
```

Replace it with:

```python
_ALLOWED_KEYS = ("provider", "mechanism", "auth", "project", "region", "model", "fast_model", "api_key")
# Per-provider valid (mechanism, auth) routes. Anthropic keeps all five combos;
# Gemini is SDK-only (no CLI equivalent), so only sdk×{apikey,vertex}.
_VALID_COMBOS = {
    "anthropic": {
        ("cli", "vertex"), ("cli", "apikey"), ("cli", "cli-login"),
        ("sdk", "apikey"), ("sdk", "vertex"),
    },
    "gemini": {
        ("sdk", "apikey"), ("sdk", "vertex"),
    },
}
# A single GLOBAL provider applies to BOTH model tiers (no per-tier mixing).
_DEFAULT_PROVIDER = "anthropic"
# Per-provider default models: "model" = Build tier (extract/fold, quality),
# "fast" = Ask/Explain tier (nl2cypher/synthesize/join-SQL, latency-sensitive).
# Both are editable/overridable (override > env > this default).
_PROVIDER_DEFAULTS = {
    "anthropic": {"model": "claude-opus-4-8", "fast": "claude-haiku-4-5-20251001"},
    "gemini":    {"model": "gemini-2.5-pro",  "fast": "gemini-2.5-flash"},
}
# Kept for the SDK fallback in _llm_claude_sdk (anthropic Build-tier default).
_DEFAULT_MODEL = _PROVIDER_DEFAULTS["anthropic"]["model"]


def _resolve_provider() -> str:
    """Effective provider: override > XGRAPH_LLM_PROVIDER env > default (anthropic)."""
    if _OVERRIDE.get("provider"):
        return _OVERRIDE["provider"]
    if os.environ.get("XGRAPH_LLM_PROVIDER"):
        return os.environ["XGRAPH_LLM_PROVIDER"]
    return _DEFAULT_PROVIDER


def fast_model() -> str:
    """Model for light, latency-sensitive interactive calls (ask/explain).

    Provider-aware and overridable: override > XGRAPH_LLM_FAST_MODEL env >
    the resolved provider's fast-tier default."""
    if _OVERRIDE.get("fast_model"):
        return _OVERRIDE["fast_model"]
    if os.environ.get("XGRAPH_LLM_FAST_MODEL"):
        return os.environ["XGRAPH_LLM_FAST_MODEL"]
    provider = _resolve_provider()
    defaults = _PROVIDER_DEFAULTS.get(provider, _PROVIDER_DEFAULTS[_DEFAULT_PROVIDER])
    return defaults["fast"]
```

- [ ] **Step 4: Make `resolve_llm_config` provider-first**

Find this verbatim block (the whole body of `resolve_llm_config`):

```python
def resolve_llm_config() -> dict:
    """Effective config = override > env > default, with a per-field `sources` map."""
    o = _OVERRIDE
    src: dict = {}

    def pick(field, env_key=None, default=None):
        if o.get(field):
            src[field] = "override"; return o[field]
        if env_key and os.environ.get(env_key):
            src[field] = "env"; return os.environ[env_key]
        src[field] = "default"; return default

    mechanism = pick("mechanism", "XGRAPH_LLM_MECHANISM", default="cli")

    if o.get("auth"):
        auth, src["auth"] = o["auth"], "override"
    elif _env_truthy("CLAUDE_CODE_USE_VERTEX"):
        auth, src["auth"] = "vertex", "env"
    elif os.environ.get("ANTHROPIC_API_KEY"):
        auth, src["auth"] = "apikey", "env"
    else:
        auth, src["auth"] = "cli-login", "default"

    project = pick("project", "ANTHROPIC_VERTEX_PROJECT_ID")
    region = pick("region", "CLOUD_ML_REGION")
    api_key = pick("api_key", "ANTHROPIC_API_KEY")

    if o.get("model"):
        model, src["model"] = o["model"], "override"
    elif os.environ.get("XGRAPH_LLM_MODEL"):
        model, src["model"] = os.environ["XGRAPH_LLM_MODEL"], "env"
    else:
        model, src["model"] = _DEFAULT_MODEL, "default"

    return {"mechanism": mechanism, "auth": auth, "project": project,
            "region": region, "model": model, "api_key": api_key, "sources": src}
```

Replace it with:

```python
def resolve_llm_config() -> dict:
    """Effective config = override > env > default, with a per-field `sources` map.

    Resolves `provider` first, then derives per-provider defaults and does
    provider-aware auth/api-key inference."""
    o = _OVERRIDE
    src: dict = {}

    def pick(field, env_key=None, default=None):
        if o.get(field):
            src[field] = "override"; return o[field]
        if env_key and os.environ.get(env_key):
            src[field] = "env"; return os.environ[env_key]
        src[field] = "default"; return default

    # provider first — everything below keys off it
    if o.get("provider"):
        provider, src["provider"] = o["provider"], "override"
    elif os.environ.get("XGRAPH_LLM_PROVIDER"):
        provider, src["provider"] = os.environ["XGRAPH_LLM_PROVIDER"], "env"
    else:
        provider, src["provider"] = _DEFAULT_PROVIDER, "default"
    defaults = _PROVIDER_DEFAULTS.get(provider, _PROVIDER_DEFAULTS[_DEFAULT_PROVIDER])
    is_gemini = provider == "gemini"

    # mechanism: gemini is SDK-only, so its default is sdk (anthropic stays cli)
    mechanism = pick("mechanism", "XGRAPH_LLM_MECHANISM",
                     default=("sdk" if is_gemini else "cli"))

    # auth: provider-aware env inference + default
    if o.get("auth"):
        auth, src["auth"] = o["auth"], "override"
    elif is_gemini:
        if _env_truthy("GOOGLE_GENAI_USE_VERTEXAI"):
            auth, src["auth"] = "vertex", "env"
        elif os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
            auth, src["auth"] = "apikey", "env"
        else:
            auth, src["auth"] = "apikey", "default"
    else:
        if _env_truthy("CLAUDE_CODE_USE_VERTEX"):
            auth, src["auth"] = "vertex", "env"
        elif os.environ.get("ANTHROPIC_API_KEY"):
            auth, src["auth"] = "apikey", "env"
        else:
            auth, src["auth"] = "cli-login", "default"

    # vertex project/region env names differ by provider
    project = pick("project", "GOOGLE_CLOUD_PROJECT" if is_gemini else "ANTHROPIC_VERTEX_PROJECT_ID")
    region = pick("region", "GOOGLE_CLOUD_LOCATION" if is_gemini else "CLOUD_ML_REGION")

    # api_key: provider-aware env source
    if o.get("api_key"):
        api_key, src["api_key"] = o["api_key"], "override"
    elif is_gemini and (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        api_key, src["api_key"] = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")), "env"
    elif not is_gemini and os.environ.get("ANTHROPIC_API_KEY"):
        api_key, src["api_key"] = os.environ["ANTHROPIC_API_KEY"], "env"
    else:
        api_key, src["api_key"] = None, "default"

    # model tiers: override > env > provider default
    if o.get("model"):
        model, src["model"] = o["model"], "override"
    elif os.environ.get("XGRAPH_LLM_MODEL"):
        model, src["model"] = os.environ["XGRAPH_LLM_MODEL"], "env"
    else:
        model, src["model"] = defaults["model"], "default"

    if o.get("fast_model"):
        fast, src["fast_model"] = o["fast_model"], "override"
    elif os.environ.get("XGRAPH_LLM_FAST_MODEL"):
        fast, src["fast_model"] = os.environ["XGRAPH_LLM_FAST_MODEL"], "env"
    else:
        fast, src["fast_model"] = defaults["fast"], "default"

    return {"provider": provider, "mechanism": mechanism, "auth": auth, "project": project,
            "region": region, "model": model, "fast_model": fast, "api_key": api_key, "sources": src}
```

- [ ] **Step 5: Make `validate_llm_config` provider-aware**

Find this verbatim block:

```python
def validate_llm_config(cfg: dict) -> dict:
    if (cfg["mechanism"], cfg["auth"]) not in VALID_COMBOS:
        raise ValueError(f"invalid LLM route: mechanism={cfg['mechanism']} auth={cfg['auth']}")
    if cfg["auth"] == "apikey" and not cfg.get("api_key"):
        raise ValueError("API key required for auth=apikey")
    if cfg["auth"] == "vertex" and not cfg.get("project"):
        raise ValueError("project required for auth=vertex")
    return cfg
```

Replace it with:

```python
def validate_llm_config(cfg: dict) -> dict:
    provider = cfg.get("provider", _DEFAULT_PROVIDER)
    combos = _VALID_COMBOS.get(provider)
    if combos is None:
        raise ValueError(f"unknown LLM provider: {provider}")
    if (cfg["mechanism"], cfg["auth"]) not in combos:
        raise ValueError(
            f"invalid LLM route: provider={provider} mechanism={cfg['mechanism']} auth={cfg['auth']}")
    if cfg["auth"] == "apikey" and not cfg.get("api_key"):
        raise ValueError("API key required for auth=apikey")
    if cfg["auth"] == "vertex" and not cfg.get("project"):
        raise ValueError("project required for auth=vertex")
    return cfg
```

- [ ] **Step 6: Add `provider` to `llm_status`**

Find this verbatim block:

```python
def llm_status() -> dict:
    """Safe projection for the UI — never includes the raw api_key."""
    eff = resolve_llm_config()
    return {"mechanism": eff["mechanism"], "auth": eff["auth"], "project": eff["project"],
            "region": eff["region"], "model": eff["model"], "fast_model": fast_model(),
            "sources": eff["sources"], "has_api_key": bool(eff.get("api_key"))}
```

Replace it with:

```python
def llm_status() -> dict:
    """Safe projection for the UI — never includes the raw api_key."""
    eff = resolve_llm_config()
    return {"provider": eff["provider"], "mechanism": eff["mechanism"], "auth": eff["auth"],
            "project": eff["project"], "region": eff["region"], "model": eff["model"],
            "fast_model": fast_model(), "sources": eff["sources"],
            "has_api_key": bool(eff.get("api_key"))}
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_llm_config.py -q` (from `backend/`)
Expected: PASS (new + existing).

- [ ] **Step 8: Run the full backend suite (backward-compat guard)**

Run: `./.venv/bin/python -m pytest tests/ -q` (from `backend/`)
Expected: all pass or SKIP (live-engine tests skip when unreachable). In particular `test_nlcypher.py`, `test_extract.py`, `test_llm_dispatch.py`, and `test_llm_endpoints.py` stay green — `fast_model()` still returns Haiku for the anthropic default, and `set_llm_config({"mechanism":"cli","auth":"vertex",...})` (no `provider`) still validates because `validate_llm_config` defaults `provider` to `anthropic`.

- [ ] **Step 9: Validation checkpoint (no commit — repo rule)**

Confirm `provider`/`fast_model` are in `_ALLOWED_KEYS`, `_VALID_COMBOS` is provider-keyed, and `resolve_llm_config`/`llm_status` return `provider`. Do NOT `git commit`.

---

## Task 2: Backend Gemini dispatch (`_llm_gemini`) + dependency

Adds the actual Gemini call path: a dispatch branch in `_llm()` and `_llm_gemini()` using the `google-genai` SDK, plus the dependency. Structured output uses `response_mime_type="application/json"` and the same regex/JSON-extract fallback the Claude SDK path uses.

**Files:**
- Modify: `backend/xgraph_gateway/llm.py`
- Modify: `backend/requirements.txt`
- Modify: `backend/tests/test_llm_dispatch.py`

**Interfaces:**
- Consumes (from Task 1): resolved cfg with `provider`, `_PROVIDER_DEFAULTS`.
- Produces: `_llm_gemini(prompt, schema, model, cfg) -> dict | str`; `_llm()` dispatches to it when `cfg["provider"] == "gemini"`.

- [ ] **Step 1: Write the failing dispatch test**

In `backend/tests/test_llm_dispatch.py`, first extend the autouse `_reset` fixture. Find this verbatim block:

```python
    for k in ("CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_API_KEY", "ANTHROPIC_VERTEX_PROJECT_ID",
              "CLOUD_ML_REGION", "XGRAPH_LLM_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "XGRAPH_LLM"):
```

Replace it with:

```python
    for k in ("CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_API_KEY", "ANTHROPIC_VERTEX_PROJECT_ID",
              "CLOUD_ML_REGION", "XGRAPH_LLM_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "XGRAPH_LLM",
              "XGRAPH_LLM_PROVIDER", "XGRAPH_LLM_FAST_MODEL", "GEMINI_API_KEY", "GOOGLE_API_KEY",
              "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"):
```

Then append these tests to the end of `backend/tests/test_llm_dispatch.py`:

```python
def _fake_genai(calls, text):
    """Build a fake `google.genai` module whose Client records init/gen kwargs and
    returns a fixed text. Mirrors the real `from google import genai` surface."""
    class FakeModels:
        def generate_content(self, **kw):
            calls["gen"] = kw
            return types.SimpleNamespace(text=text)

    class FakeClient:
        def __init__(self, **kw):
            calls["init"] = kw
            self.models = FakeModels()

    fake_types = types.SimpleNamespace(
        GenerateContentConfig=lambda **kw: ("gcc", kw))
    fake_genai = types.SimpleNamespace(Client=FakeClient, types=fake_types)
    return fake_genai, fake_types


def test_gemini_apikey_text_path(monkeypatch):
    calls = {}
    fake_genai, fake_types = _fake_genai(calls, "gemini-hi")
    sys = __import__("sys")
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)
    llm.set_llm_config({"provider": "gemini", "auth": "apikey", "api_key": "AIza-k"})
    assert llm._llm("hello") == "gemini-hi"
    assert calls["init"] == {"api_key": "AIza-k"}       # apikey client construction
    assert calls["gen"]["model"] == "gemini-2.5-pro"    # Build-tier default model


def test_gemini_schema_path_extracts_json(monkeypatch):
    calls = {}
    # response has prose around the JSON — the regex fallback must still parse it
    fake_genai, fake_types = _fake_genai(calls, 'here you go: {"answer": 42} done')
    sys = __import__("sys")
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)
    llm.set_llm_config({"provider": "gemini", "auth": "apikey", "api_key": "AIza-k"})
    out = llm._llm("q", schema={"type": "object"})
    assert out == {"answer": 42}


def test_gemini_vertex_client_construction(monkeypatch):
    calls = {}
    fake_genai, fake_types = _fake_genai(calls, "ok")
    sys = __import__("sys")
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)
    llm.set_llm_config({"provider": "gemini", "auth": "vertex", "project": "proj-g", "region": "us-central1"})
    llm._llm("hello")
    assert calls["init"]["vertexai"] is True
    assert calls["init"]["project"] == "proj-g"
    assert calls["init"]["location"] == "us-central1"
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/bin/python -m pytest tests/test_llm_dispatch.py -q` (from `backend/`)
Expected: the three new Gemini tests FAIL (dispatch still routes gemini through the Claude SDK / `_llm_gemini` undefined); existing dispatch tests pass.

- [ ] **Step 3: Add the dispatch branch**

In `backend/xgraph_gateway/llm.py`, find this verbatim block inside `_llm`:

```python
    cfg = validate_llm_config(resolve_llm_config())
    if cfg["mechanism"] == "sdk":
        return _llm_claude_sdk(prompt, schema, model, cfg)
    if not shutil.which("claude"):
        raise RuntimeError("mechanism=cli but no `claude` CLI on PATH — pick SDK or install the CLI")
    return _llm_claude_cli(prompt, schema, model, cfg)
```

Replace it with:

```python
    cfg = validate_llm_config(resolve_llm_config())
    if cfg["provider"] == "gemini":
        return _llm_gemini(prompt, schema, model, cfg)
    if cfg["mechanism"] == "sdk":
        return _llm_claude_sdk(prompt, schema, model, cfg)
    if not shutil.which("claude"):
        raise RuntimeError("mechanism=cli but no `claude` CLI on PATH — pick SDK or install the CLI")
    return _llm_claude_cli(prompt, schema, model, cfg)
```

- [ ] **Step 4: Add `_llm_gemini`**

Find this verbatim block at the end of the file (the whole `_llm_claude_sdk` body ending the module):

```python
def _llm_claude_sdk(prompt: str, schema: Optional[dict], model: Optional[str], cfg: dict) -> Any:
    import anthropic
    m = model or cfg.get("model") or _DEFAULT_MODEL
    if cfg["auth"] == "vertex":
        client = anthropic.AnthropicVertex(project_id=cfg["project"], region=cfg["region"])
        m = _vertex_model_id(m)
    else:
        client = anthropic.Anthropic(api_key=cfg.get("api_key") or None)
    resp = client.messages.create(model=m, max_tokens=2048,
                                  messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in resp.content if b.type == "text")
    if schema is None:
        return text
    mobj = re.search(r"\{.*\}", text, re.DOTALL)
    return json.loads(mobj.group(0)) if mobj else {}
```

Replace it with (keeps `_llm_claude_sdk` unchanged, appends `_llm_gemini`):

```python
def _llm_claude_sdk(prompt: str, schema: Optional[dict], model: Optional[str], cfg: dict) -> Any:
    import anthropic
    m = model or cfg.get("model") or _DEFAULT_MODEL
    if cfg["auth"] == "vertex":
        client = anthropic.AnthropicVertex(project_id=cfg["project"], region=cfg["region"])
        m = _vertex_model_id(m)
    else:
        client = anthropic.Anthropic(api_key=cfg.get("api_key") or None)
    resp = client.messages.create(model=m, max_tokens=2048,
                                  messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in resp.content if b.type == "text")
    if schema is None:
        return text
    mobj = re.search(r"\{.*\}", text, re.DOTALL)
    return json.loads(mobj.group(0)) if mobj else {}


def _llm_gemini(prompt: str, schema: Optional[dict], model: Optional[str], cfg: dict) -> Any:
    """Gemini via the google-genai SDK (Google AI Studio api-key OR Vertex).

    Structured output asks for a JSON mime-type and then reuses the SAME
    regex/JSON-extract fallback as the Claude SDK path — this sidesteps Gemini's
    response_schema dialect quirks (no $ref, restricted keywords) while still
    returning a parsed dict. Gemini model IDs are plain (no @date translation —
    that stays Anthropic-Vertex-only)."""
    from google import genai
    from google.genai import types
    m = model or cfg.get("model") or _PROVIDER_DEFAULTS["gemini"]["model"]
    if cfg["auth"] == "vertex":
        client = genai.Client(vertexai=True, project=cfg["project"],
                              location=cfg.get("region") or "global")
    else:
        client = genai.Client(api_key=cfg.get("api_key") or None)
    config = types.GenerateContentConfig(response_mime_type="application/json") if schema is not None else None
    resp = client.models.generate_content(model=m, contents=prompt, config=config)
    text = resp.text or ""
    if schema is None:
        return text
    mobj = re.search(r"\{.*\}", text, re.DOTALL)
    return json.loads(mobj.group(0)) if mobj else {}
```

- [ ] **Step 5: Add the dependency**

In `backend/requirements.txt`, find this verbatim final block:

```
# Only needed for the SDK LLM mechanism (the `claude` CLI is the default backend
# and needs nothing here). The [vertex] extra pulls google-auth so the SDK route
# can authenticate to Vertex via ADC. The route is user-selectable at runtime, so
# this is safe to have installed even when running on the default CLI route.
anthropic[vertex]
```

Replace it with:

```
# Only needed for the SDK LLM mechanism (the `claude` CLI is the default backend
# and needs nothing here). The [vertex] extra pulls google-auth so the SDK route
# can authenticate to Vertex via ADC. The route is user-selectable at runtime, so
# this is safe to have installed even when running on the default CLI route.
anthropic[vertex]
# Google Gemini provider (AI Studio api-key + Vertex Gemini). Light dep; the
# Vertex path reuses the google-auth already pulled by anthropic[vertex].
google-genai
```

- [ ] **Step 6: Install the new dependency**

Run: `./.venv/bin/pip install google-genai` (from `backend/`)
Expected: installs successfully (the dispatch tests mock the SDK, but the dependency must be present for real use and for `pip install -r requirements.txt` on a fresh checkout).

- [ ] **Step 7: Run the tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_llm_dispatch.py -q` (from `backend/`)
Expected: PASS (new + existing).

- [ ] **Step 8: Run the full backend suite**

Run: `./.venv/bin/python -m pytest tests/ -q` (from `backend/`)
Expected: all pass or SKIP.

- [ ] **Step 9: Validation checkpoint (no commit — repo rule)**

Confirm `_llm` dispatches gemini before the mechanism branch, `_llm_gemini` exists, and `google-genai` is in `requirements.txt`. Do NOT `git commit`.

---

## Task 3: Frontend state, connect wiring, and localStorage caching

Threads `provider` and `fast_model` through the `App` LLM state and the Connect flow, seeds the picker from `/llm_status`, and caches the picker selection in `localStorage` so it survives reloads and gateway restarts. Also adds a `gateway.js` pass-through regression test. No picker UI yet (Task 4).

**Files:**
- Modify: `frontend/XGraph.html`
- Modify: `frontend/tests/test_client.mjs`

**Interfaces:**
- Consumes (existing in `App`): `llmConn` / `setLlmConn`, `gwClient.getLlmStatus()`, `gwClient.setLlmConfig(cfg)`, `handleConnectAxes`.
- Produces (used by Task 4): `llmConn` gains `provider` (str), `fastModel` (str); module consts `LLM_PROVIDER_DEFAULTS` and helper `loadLlmRoute()`; `handleConnectAxes` sends `provider` + `fast_model` and persists to `localStorage['xgraph.llmRoute']`.

- [ ] **Step 1: Add the gateway.js pass-through regression test**

`gateway.js` already forwards an arbitrary cfg to `/llm_config`, so no code change — this test guards it. In `frontend/tests/test_client.mjs`, find this verbatim line:

```js
  const connectResult = await sessionClient.connect(
```

Insert immediately BEFORE it:

```js
  // setLlmConfig forwards the cfg object verbatim (provider + fast_model included)
  await sessionClient.setLlmConfig({ provider: "gemini", mechanism: "sdk", auth: "apikey",
    api_key: "AIza-k", model: "gemini-2.5-pro", fast_model: "gemini-2.5-flash" });
  const llmBody = seenBodies[seenBodies.length - 1];
  assert.equal(llmBody.provider, "gemini");
  assert.equal(llmBody.fast_model, "gemini-2.5-flash");
  assert.equal(llmBody.api_key, "AIza-k");

```

- [ ] **Step 2: Run to verify it passes (pass-through already works)**

Run: `node tests/test_client.mjs` (from `frontend/`)
Expected: exits 0 (the fake fetch in this file returns `{}` for `/llm_config`, and `setLlmConfig` records the body into `seenBodies`). If it fails because the fake `sessionClient` routes don't record the body, that's the guard doing its job — but `postJSON` calls the same `fakeFetch`, so the body is captured. On success, no code change to `gateway.js` is needed.

- [ ] **Step 3: Add module consts (`LLM_PROVIDER_DEFAULTS`, `loadLlmRoute`)**

In `frontend/XGraph.html`, find this verbatim line (module consts near the top, ~line 64):

```js
const VISUALIZE_PAGE_SIZE = 10000;
```

Insert immediately AFTER it:

```js
// Per-provider default LLM route + model tiers for the Setup picker. A single
// GLOBAL provider applies to both tiers (no mixing). Switching provider auto-loads
// that provider's defaults (see selectLlmProvider in SetupPanel). Mirrors the
// backend _PROVIDER_DEFAULTS.
const LLM_PROVIDER_DEFAULTS = {
    anthropic: { mechanism:'cli', auth:'vertex', model:'claude-opus-4-8', fastModel:'claude-haiku-4-5-20251001' },
    gemini:    { mechanism:'sdk', auth:'apikey', model:'gemini-2.5-pro',  fastModel:'gemini-2.5-flash' },
};
// Cached picker selection (survives reload + gateway restart). Stored in the
// llmConn shape (camelCase apiKey/fastModel). The api_key is cached too — an
// accepted dev-only tradeoff (never persisted server-side; /llm_status never
// returns it).
const LLM_ROUTE_STORAGE_KEY = 'xgraph.llmRoute';
function loadLlmRoute() {
    try {
        var raw = (typeof window !== 'undefined' && window.localStorage)
            ? window.localStorage.getItem(LLM_ROUTE_STORAGE_KEY) : null;
        return raw ? (JSON.parse(raw) || {}) : {};
    } catch (e) { return {}; }
}
```

- [ ] **Step 4: Seed `llmConn` initial state from cache + provider default**

Find this verbatim line (~8210):

```js
    const [llmConn, setLlmConn]         = useState({ apiKey:'', extractMode:'sequential' });
```

Replace it with:

```js
    const [llmConn, setLlmConn]         = useState(Object.assign({ provider:'anthropic', apiKey:'', fastModel:'', extractMode:'sequential' }, loadLlmRoute()));
```

- [ ] **Step 5: Seed provider + fast_model from `/llm_status`**

Find this verbatim block (the seed `forEach` inside the `getLlmStatus` effect, ~8227-8235):

```js
                setLlmConn(function (prev) {
                    var next = Object.assign({}, prev);
                    ['mechanism', 'auth', 'project', 'region', 'model'].forEach(function (k) {
                        if (st[k] && (next[k] === undefined || next[k] === null || next[k] === '')) {
                            next[k] = st[k];
                        }
                    });
                    return next;
                });
```

Replace it with (adds `provider` to the same-key list, and handles `fast_model` → `fastModel` explicitly since the key names differ):

```js
                setLlmConn(function (prev) {
                    var next = Object.assign({}, prev);
                    ['provider', 'mechanism', 'auth', 'project', 'region', 'model'].forEach(function (k) {
                        if (st[k] && (next[k] === undefined || next[k] === null || next[k] === '')) {
                            next[k] = st[k];
                        }
                    });
                    // status uses snake_case fast_model; llmConn uses camelCase fastModel
                    if (st.fast_model && (next.fastModel === undefined || next.fastModel === null || next.fastModel === '')) {
                        next.fastModel = st.fast_model;
                    }
                    return next;
                });
```

- [ ] **Step 6: Send `provider` + `fast_model` on Connect and persist the route**

Find this verbatim block inside `handleConnectAxes` (~8306-8318):

```js
            try {
                await gwClient.setLlmConfig({
                    mechanism: llmConn.mechanism || 'cli',
                    auth: llmConn.auth || 'vertex',
                    project: llmConn.project || '',
                    region: llmConn.region || '',
                    model: llmConn.model || '',
                    api_key: llmConn.apiKey || '',
                });
                setLlmStatus(await gwClient.getLlmStatus());
            } catch (e) {
                setLlmStatus({ error: String(e && e.message || e) });
            }
```

Replace it with:

```js
            try {
                var _prov = llmConn.provider || 'anthropic';
                var _provDefaults = LLM_PROVIDER_DEFAULTS[_prov] || LLM_PROVIDER_DEFAULTS.anthropic;
                await gwClient.setLlmConfig({
                    provider: _prov,
                    mechanism: llmConn.mechanism || _provDefaults.mechanism,
                    auth: llmConn.auth || _provDefaults.auth,
                    project: llmConn.project || '',
                    region: llmConn.region || '',
                    model: llmConn.model || '',
                    fast_model: llmConn.fastModel || '',
                    api_key: llmConn.apiKey || '',
                });
                setLlmStatus(await gwClient.getLlmStatus());
                // Cache the picker selection so subsequent runs skip reconfiguring.
                try {
                    window.localStorage.setItem(LLM_ROUTE_STORAGE_KEY, JSON.stringify({
                        provider: _prov, mechanism: llmConn.mechanism || '', auth: llmConn.auth || '',
                        project: llmConn.project || '', region: llmConn.region || '',
                        model: llmConn.model || '', fastModel: llmConn.fastModel || '',
                        apiKey: llmConn.apiKey || '', extractMode: llmConn.extractMode || 'sequential',
                    }));
                } catch (e) { /* localStorage unavailable — non-fatal */ }
            } catch (e) {
                setLlmStatus({ error: String(e && e.message || e) });
            }
```

- [ ] **Step 7: Run the transpile check**

Run the Transpile check command (Global Constraints), from `frontend/`.
Expected: `TRANSPILE OK`.

- [ ] **Step 8: Run the frontend unit tests**

Run: `node tests/test_transforms.mjs && node tests/test_client.mjs` (from `frontend/`)
Expected: both exit 0.

- [ ] **Step 9: Validation checkpoint (no commit — repo rule)**

Confirm `llmConn` initializes with `provider` from cache/default, the status seed copies `provider`+`fastModel`, and `handleConnectAxes` sends `provider`+`fast_model` and writes `localStorage`. Do NOT `git commit`.

---

## Task 4: Frontend picker UI (Provider select, Gemini-aware fields) + footers

Adds the Provider `<select>` at the top of the Setup LLM card, makes the mechanism/auth/model controls provider-aware (Claude keeps all five combos; Gemini hides mechanism and offers Google API key | Vertex), adds the editable Ask/Explain (fast) model field, and surfaces `provider` in both the Setup status line and the App status bar.

**Files:**
- Modify: `frontend/XGraph.html`

**Interfaces:**
- Consumes (from Task 3): `LLM_PROVIDER_DEFAULTS`, `llmConn.provider` / `llmConn.fastModel`, `setLlmConn`, `setLlmField`, `llmStatus`.
- Produces: `selectLlmProvider(p)` in `SetupPanel`; provider-aware picker + status lines.

- [ ] **Step 1: Add the `selectLlmProvider` helper**

Find this verbatim line in `SetupPanel` (~6641):

```js
    var setLlmField = function(k, v) { setLlmConn(function(prev){ var n = Object.assign({}, prev); n[k] = v; return n; }); };
```

Insert immediately AFTER it:

```js
    // Switching provider auto-loads that provider's default route + model tiers
    // (single global provider — no per-tier mixing), so the user never starts from
    // an empty state. Preserves any api-key/project the user already typed.
    var selectLlmProvider = function(p) {
        var d = LLM_PROVIDER_DEFAULTS[p] || LLM_PROVIDER_DEFAULTS.anthropic;
        setLlmConn(function(prev){
            return Object.assign({}, prev, { provider:p, mechanism:d.mechanism, auth:d.auth, model:d.model, fastModel:d.fastModel });
        });
    };
```

- [ ] **Step 2: Add the Provider select above the Mechanism × Auth row**

Find this verbatim block (start of the mechanism/auth flex row, ~6723-6726):

```jsx
                <div style={{ display:'flex', gap:12, marginBottom:10 }}>
                    <div style={{ flex:1 }}>
                        <Label>Mechanism</Label>
                        <select style={inp} value={llmConn.mechanism || 'cli'} onChange={function(e){ setLlmField('mechanism', e.target.value); }}>
                            <option value="cli">claude CLI</option>
                            <option value="sdk">Anthropic SDK</option>
                        </select>
                    </div>
```

Replace it with (adds the Provider select before the row and makes the Mechanism column anthropic-only — Gemini is SDK-only):

```jsx
                <Label>Provider</Label>
                <select style={inp} value={llmConn.provider || 'anthropic'} onChange={function(e){ selectLlmProvider(e.target.value); }}>
                    <option value="anthropic">Claude (Anthropic)</option>
                    <option value="gemini">Gemini (Google)</option>
                </select>
                <div style={{ display:'flex', gap:12, marginBottom:10 }}>
                    {(llmConn.provider || 'anthropic') === 'anthropic' ? <div style={{ flex:1 }}>
                        <Label>Mechanism</Label>
                        <select style={inp} value={llmConn.mechanism || 'cli'} onChange={function(e){ setLlmField('mechanism', e.target.value); }}>
                            <option value="cli">claude CLI</option>
                            <option value="sdk">Anthropic SDK</option>
                        </select>
                    </div> : null}
```

- [ ] **Step 3: Make the Auth options provider-aware**

Find this verbatim block (the Auth `<select>`, ~6733-6737):

```jsx
                        <select style={inp} value={llmConn.auth || 'vertex'} onChange={function(e){ setLlmField('auth', e.target.value); }}>
                            <option value="vertex">GCP Vertex</option>
                            <option value="apikey">API key</option>
                            {(llmConn.mechanism || 'cli') === 'cli' ? <option value="cli-login">CLI login</option> : null}
                        </select>
```

Replace it with:

```jsx
                        <select style={inp} value={llmConn.auth || ((llmConn.provider || 'anthropic') === 'gemini' ? 'apikey' : 'vertex')} onChange={function(e){ setLlmField('auth', e.target.value); }}>
                            {(llmConn.provider || 'anthropic') === 'gemini' ? [
                                <option key="apikey" value="apikey">Google API key</option>,
                                <option key="vertex" value="vertex">Vertex AI</option>
                            ] : [
                                <option key="vertex" value="vertex">GCP Vertex</option>,
                                <option key="apikey" value="apikey">API key</option>,
                                ((llmConn.mechanism || 'cli') === 'cli' ? <option key="cli-login" value="cli-login">CLI login</option> : null)
                            ]}
                        </select>
```

- [ ] **Step 4: Provider-aware API-key placeholder/help text**

Find this verbatim block (the apikey branch, ~6748-6753):

```jsx
                ) : (llmConn.auth || 'vertex') === 'apikey' ? (
                    <div>
                        <Label>API key</Label>
                        <input type={pwType} autoComplete="new-password" style={inp} value={llmConn.apiKey || ''} onChange={function(e){ setLlmField('apiKey', e.target.value); }} placeholder="sk-ant-…" />
                        <p style={{ fontSize:11, color:'#b2bec3', margin:'0 0 8px' }}>Sent to the gateway over localhost. Not stored in /llm_status.</p>
                    </div>
```

Replace it with:

```jsx
                ) : (llmConn.auth || ((llmConn.provider || 'anthropic') === 'gemini' ? 'apikey' : 'vertex')) === 'apikey' ? (
                    <div>
                        <Label>{(llmConn.provider || 'anthropic') === 'gemini' ? 'Google AI Studio API key' : 'API key'}</Label>
                        <input type={pwType} autoComplete="new-password" style={inp} value={llmConn.apiKey || ''} onChange={function(e){ setLlmField('apiKey', e.target.value); }} placeholder={(llmConn.provider || 'anthropic') === 'gemini' ? 'AIza… (aistudio.google.com/apikey)' : 'sk-ant-…'} />
                        <p style={{ fontSize:11, color:'#b2bec3', margin:'0 0 8px' }}>Sent to the gateway over localhost. Cached in your browser; not stored in /llm_status.</p>
                    </div>
```

Note: the surrounding `(llmConn.auth || 'vertex') === 'vertex'` test above this branch stays as-is — for Gemini the default auth is `apikey`, so an unset `auth` falls through the vertex test (`undefined || 'vertex' === 'vertex'` is true only when the vertex branch renders). To keep the vertex-first `if` correct for Gemini's apikey default, also update the opening vertex test in the next step.

- [ ] **Step 5: Fix the vertex-branch default for Gemini**

Find this verbatim line (the opening of the auth-fields conditional, ~6740):

```jsx
                {(llmConn.auth || 'vertex') === 'vertex' ? (
```

Replace it with:

```jsx
                {(llmConn.auth || ((llmConn.provider || 'anthropic') === 'gemini' ? 'apikey' : 'vertex')) === 'vertex' ? (
```

- [ ] **Step 6: Add the Ask/Explain (fast) model field**

Find this verbatim block (the Build model field + apply note, ~6757-6759):

```jsx
                <Label>Build model (optional)</Label>
                <input style={inp} value={llmConn.model || ''} onChange={function(e){ setLlmField('model', e.target.value); }} placeholder="claude-opus-4-8" />
                <p style={{ fontSize:11, color:'#b2bec3', margin:'2px 0 0' }}>Applied when you click <strong>Connect</strong> below.</p>
```

Replace it with:

```jsx
                <Label>Build model (optional)</Label>
                <input style={inp} value={llmConn.model || ''} onChange={function(e){ setLlmField('model', e.target.value); }} placeholder={(llmConn.provider || 'anthropic') === 'gemini' ? 'gemini-2.5-pro' : 'claude-opus-4-8'} />
                <Label>Ask/Explain model (optional)</Label>
                <input style={inp} value={llmConn.fastModel || ''} onChange={function(e){ setLlmField('fastModel', e.target.value); }} placeholder={(llmConn.provider || 'anthropic') === 'gemini' ? 'gemini-2.5-flash' : 'claude-haiku-4-5-20251001'} />
                <p style={{ fontSize:11, color:'#b2bec3', margin:'2px 0 0' }}>Applied when you click <strong>Connect</strong> below.</p>
```

- [ ] **Step 7: Show provider in the Setup status line**

Find this verbatim block (~6763-6766):

```jsx
                    <p style={{ fontSize:11, color:'#00b894', margin:'8px 0 0', fontWeight:600 }}>
                        LLM: {llmStatus.mechanism} · {llmStatus.auth}
                        {llmStatus.project ? ' · ' + llmStatus.project : ''}
                        {llmStatus.auth === 'apikey' ? (llmStatus.has_api_key ? ' · key set' : ' · NO key') : ''}
```

Replace it with:

```jsx
                    <p style={{ fontSize:11, color:'#00b894', margin:'8px 0 0', fontWeight:600 }}>
                        LLM: {llmStatus.provider ? llmStatus.provider + ' · ' : ''}{llmStatus.mechanism} · {llmStatus.auth}
                        {llmStatus.project ? ' · ' + llmStatus.project : ''}
                        {llmStatus.auth === 'apikey' ? (llmStatus.has_api_key ? ' · key set' : ' · NO key') : ''}
```

- [ ] **Step 8: Show provider in the App status bar**

Find this verbatim block (the App footer LLM span, ~9790-9795):

```jsx
                    {llmStatus && !llmStatus.error && <>
                        <span style={{ margin:'0 6px', color:'#dfe6e9' }}>{'·'}</span>
                        <span title={'LLM: ' + llmStatus.mechanism + ' · ' + llmStatus.auth + ' · Ask/Explain=' + (llmStatus.fast_model || '?') + ' · Build=' + (llmStatus.model || '?') + ' — edit backend/.env to change'}>
                            {'LLM: ' + llmStatus.mechanism + '·' + llmStatus.auth + ((llmStatus.fast_model || llmStatus.model) ? '·' + (llmStatus.fast_model || llmStatus.model).replace('claude-','').split('-')[0] : '')}
                        </span>
                    </>}
```

Replace it with:

```jsx
                    {llmStatus && !llmStatus.error && <>
                        <span style={{ margin:'0 6px', color:'#dfe6e9' }}>{'·'}</span>
                        <span title={'LLM: ' + (llmStatus.provider ? llmStatus.provider + ' · ' : '') + llmStatus.mechanism + ' · ' + llmStatus.auth + ' · Ask/Explain=' + (llmStatus.fast_model || '?') + ' · Build=' + (llmStatus.model || '?') + ' — set in Setup'}>
                            {'LLM: ' + (llmStatus.provider ? llmStatus.provider + '·' : '') + llmStatus.mechanism + '·' + llmStatus.auth + ((llmStatus.fast_model || llmStatus.model) ? '·' + (llmStatus.fast_model || llmStatus.model).replace('claude-','').replace('gemini-','').split('-')[0] : '')}
                        </span>
                    </>}
```

- [ ] **Step 9: Run the transpile check**

Run the Transpile check command (Global Constraints), from `frontend/`.
Expected: `TRANSPILE OK`.

- [ ] **Step 10: Serve check (if the gateway is running)**

Run: `curl -sf -o /dev/null -w "%{http_code}\n" http://localhost:8090/`
Expected: `200`. (If the gateway isn't up, start it with `./xgraph start` from the repo root, or skip — smoke check only.)

- [ ] **Step 11: Validation checkpoint (no commit — repo rule)**

Confirm: the Provider select renders and switches the axes; Gemini hides Mechanism and offers Google API key | Vertex; the Ask/Explain model field is present; both status lines show provider. Do NOT `git commit`.

---

## Manual browser acceptance (user-driven, after all tasks)

Open `http://localhost:8090/` and reload.

1. **Default is Claude, unchanged:** Setup shows Provider = Claude; the mechanism × auth picker and both model tiers behave exactly as before; Connect works; Ask/Explain/Extract use Claude.
2. **Switch to Gemini:** Provider = Gemini hides the Mechanism control (SDK implied); Auth offers **Google API key | Vertex**; Build defaults to `gemini-2.5-pro`, Ask/Explain to `gemini-2.5-flash`. Enter a Google AI Studio key, Connect → status line reads `LLM: gemini · sdk · apikey`.
3. **Gemini Ask works:** With a valid key, run an Ask/Explain against a graph → a Gemini-generated answer returns.
4. **Vertex Gemini:** Switch Auth to Vertex, fill project/region, Connect → status reflects `vertex`; an Ask works if ADC/Vertex is configured.
5. **Caching:** After a successful Connect on Gemini, reload the page → the picker comes back on Gemini with the same route/models/key (from `localStorage`); Connect re-applies it. Restart the gateway (`./xgraph restart`) and reload → still Gemini after Connect.
6. **Switch back to Claude:** Provider = Claude restores all five Claude combos and Claude default models; Connect → Claude route active again.

---

## Self-Review

- **Spec coverage:**
  - Provider axis (`anthropic` default | `gemini`), override > env > default → Task 1 (`resolve_llm_config` provider block, `_resolve_provider`, `_DEFAULT_PROVIDER`).
  - Per-provider valid routes; gemini SDK-only, `(gemini,cli)` rejected → Task 1 (`_VALID_COMBOS`, `validate_llm_config`) + test `test_gemini_cli_route_rejected`.
  - Per-provider default models, both tiers overridable; `fast_model` becomes overridable → Task 1 (`_PROVIDER_DEFAULTS`, `fast_model`, `resolve_llm_config` model/fast blocks) + tests.
  - Provider-aware auth/api-key inference (Gemini env vars) → Task 1 `resolve_llm_config` + `test_gemini_env_auth_inference`.
  - `_llm` dispatch branch + `_llm_gemini` (google-genai, apikey vs vertex, JSON mime + regex fallback, plain model IDs) → Task 2 + three dispatch tests.
  - `llm_status` gains `provider`, still hides key → Task 1 Step 6 + `test_status_includes_provider_and_hides_key`.
  - `warmup` unchanged (provider-aware via `fast_model`/resolved model) → no task needed; noted here intentionally.
  - `requirements.txt` gains `google-genai` → Task 2 Step 5.
  - Picker: Provider-first, all five Claude combos kept, Gemini hides mechanism + Google API key|Vertex, both tiers editable, switch auto-loads defaults, applied by Connect → Task 4 Steps 1-6 + Task 3 (`LLM_PROVIDER_DEFAULTS`, `handleConnectAxes`).
  - Footer shows provider → Task 4 Steps 7-8.
  - `gateway.js` forwards provider + fast_model (no code change) + `getLlmStatus` needs none → Task 3 Step 1-2 (regression test).
  - localStorage caching (`xgraph.llmRoute`, incl. api_key tradeoff), hydrate on load + apply on Connect → Task 3 (`loadLlmRoute`, init state, `handleConnectAxes` persist).
  - Out-of-scope (per-tier mixing / local Gemma / server-side persistence / consumer changes) → not implemented; consumers and `app.py` untouched (Global Constraints).
- **Placeholder scan:** none — every code step has verbatim find/replace content and a concrete run command.
- **Type consistency:** backend cfg dict keys `provider`/`fast_model` (str) added consistently across `resolve_llm_config` return, `validate_llm_config`, `llm_status`, `_ALLOWED_KEYS`, and dispatch. Frontend: `llmConn.provider`/`llmConn.fastModel` (camelCase) used in init, seed, connect, and picker; the Connect payload maps `fastModel` → snake_case `fast_model` (matching backend `_ALLOWED_KEYS`), and the `/llm_status` seed maps `st.fast_model` → `next.fastModel`. `LLM_PROVIDER_DEFAULTS` keys (`mechanism`,`auth`,`model`,`fastModel`) identical in the const (Task 3 Step 3), `selectLlmProvider` (Task 4 Step 1), and `handleConnectAxes` fallback (Task 3 Step 6). `LLM_ROUTE_STORAGE_KEY` identical in `loadLlmRoute`, init state, and the persist call.
- **Cross-task ordering:** Task 1 (config) precedes Task 2 (dispatch reads `_PROVIDER_DEFAULTS`/`provider`); Task 3 (state/consts) precedes Task 4 (picker uses `LLM_PROVIDER_DEFAULTS`/`selectLlmProvider`/`llmConn.fastModel`). Backend and frontend halves are independent and could run in parallel, but the numbered order is safe sequential.
