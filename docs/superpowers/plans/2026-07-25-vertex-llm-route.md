# Vertex LLM Route Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user pick the LLM route (Vertex / API-key / CLI-login × CLI/SDK) from a Setup-panel box, and make Vertex the robust default so "Ask" stops failing silently.

**Architecture:** A gateway-global, in-memory LLM config holder in `llm.py` (override > env > default), consumed by the existing `_llm()` with no change to its `nlcypher`/`extract` call sites. `app.py` loads `backend/.env` at startup (permanent Vertex default) and exposes `POST /llm_config` + `GET /llm_status`. The `XGraph.html` Setup panel gets a two-axis picker (Mechanism × Auth) that POSTs config and displays live status via `gateway.js` methods.

**Tech Stack:** Python 3 / FastAPI / pytest (backend), the `claude` CLI + anthropic SDK (`AnthropicVertex` for Vertex), single-file React-UMD + Babel-standalone (frontend), Node for `gateway.js` tests.

## Global Constraints

- **NO `git commit` anywhere under `xgraph/`** (CLAUDE.md). Write files freely; never stage/commit. Each task's final step is a test checkpoint, NOT a commit.
- **Self-contained backend** — no imports from `../falkor` / `../graphrag`; no `sys.path` hacks.
- **Never log or return the API key** — `/llm_status` exposes `has_api_key: bool` only.
- **Two-axis config** — Mechanism ∈ {`cli`,`sdk`}, Auth ∈ {`vertex`,`apikey`,`cli-login`}. Valid combos: `cli+vertex`, `cli+apikey`, `cli+cli-login`, `sdk+apikey`, `sdk+vertex`.
- **Config scope is gateway-global, in-memory** — restart falls back to `.env` defaults; UI overrides are not persisted.
- **Model default** = `XGRAPH_LLM_MODEL` → `ANTHROPIC_DEFAULT_OPUS_MODEL` → `"claude-opus-4-8"`.
- Backend tests run from `backend/` via the worktree venv: `./.venv/bin/python -m pytest tests/ -v`.
- Frontend React is NOT headlessly testable — validate with the Babel transpile check + a `curl` 200; real behavior deferred to the browser (user-driven).

---

### Task 1: Worktree venv + dependency verification

This worktree is a fresh checkout with **no `.venv`**. Establish a working backend before any TDD, and confirm the Vertex SDK extra is installable (needed by Task 3's `sdk+vertex` path).

**Files:**
- Create: `backend/.venv/` (git-ignored), `backend/requirements-vertex.txt` (one line) if the extra is missing from `requirements.txt`.
- Modify: none (source).

- [ ] **Step 1: Create the venv and install deps**

```bash
cd backend
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

- [ ] **Step 2: Confirm the anthropic Vertex extra is importable**

```bash
./.venv/bin/python -c "from anthropic import AnthropicVertex; print('AnthropicVertex OK')"
```

If this fails with ImportError, install the extra and record it:

```bash
./.venv/bin/pip install "anthropic[vertex]"
echo "anthropic[vertex]   # AnthropicVertex client for the sdk+vertex LLM route" > requirements-vertex.txt
```

- [ ] **Step 3: Baseline the existing suite (must be green before we start)**

Run: `cd backend && ./.venv/bin/python -m pytest tests/ -q`
Expected: all pass (live FalkorDB/Kinetica tests SKIP if services are down — that's fine).

- [ ] **Step 4: Confirm `dotenv` and `claude` are available (used by later tasks)**

```bash
./.venv/bin/python -c "import dotenv; print('dotenv OK')"
which claude && claude --version
```
Expected: `dotenv OK`, and a `claude` path + version.

- [ ] **Step 5: Checkpoint (no commit)** — venv exists, suite green, `AnthropicVertex` imports. Do NOT `git commit`.

---

### Task 2: `llm.py` — config store + resolver + status

Add the gateway-global config holder and pure resolution logic. No `_llm` behavior change yet (that's Task 3).

**Files:**
- Modify: `backend/xgraph_gateway/llm.py` (add module state + functions above `_llm`)
- Test: `backend/tests/test_llm_config.py` (create)

**Interfaces:**
- Produces:
  - `set_llm_config(cfg: dict) -> dict` — replace the override with the whitelisted keys of `cfg`, validate the resulting effective config, return it (the full effective dict incl. `api_key`). Raises `ValueError` on invalid combo / missing required field (restores the previous override on failure).
  - `get_llm_config() -> dict` — the raw override (may be `{}`).
  - `resolve_llm_config() -> dict` — effective config: `{mechanism, auth, project, region, model, api_key, sources}` where `sources[field] ∈ {"override","env","default"}`. Precedence override > env > default.
  - `validate_llm_config(cfg: dict) -> dict` — raise `ValueError` unless `(mechanism,auth)` is a valid combo, and (apikey→api_key present), (vertex→project present). Returns `cfg`.
  - `llm_status() -> dict` — safe projection of `resolve_llm_config()`: `{mechanism, auth, project, region, model, sources, has_api_key}` — **never `api_key`**.
  - Module constant `VALID_COMBOS: set[tuple[str,str]]`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_llm_config.py`:

```python
import os
import pytest
from xgraph_gateway import llm


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    # Start each test from a clean override + a known-empty LLM env.
    llm._OVERRIDE = {}
    for k in ("CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_API_KEY", "ANTHROPIC_VERTEX_PROJECT_ID",
              "CLOUD_ML_REGION", "XGRAPH_LLM_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL"):
        monkeypatch.delenv(k, raising=False)
    yield
    llm._OVERRIDE = {}


def test_default_is_cli_login_when_nothing_set():
    cfg = llm.resolve_llm_config()
    assert cfg["mechanism"] == "cli"
    assert cfg["auth"] == "cli-login"
    assert cfg["model"] == "claude-opus-4-8"
    assert cfg["sources"]["auth"] == "default"


def test_env_vertex_becomes_default_auth(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "proj-x")
    monkeypatch.setenv("CLOUD_ML_REGION", "global")
    cfg = llm.resolve_llm_config()
    assert cfg["auth"] == "vertex"
    assert cfg["project"] == "proj-x"
    assert cfg["region"] == "global"
    assert cfg["sources"]["auth"] == "env"


def test_override_beats_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "proj-x")
    eff = llm.set_llm_config({"mechanism": "cli", "auth": "cli-login"})
    assert eff["auth"] == "cli-login"
    assert llm.resolve_llm_config()["sources"]["auth"] == "override"


def test_invalid_combo_rejected():
    with pytest.raises(ValueError):
        llm.set_llm_config({"mechanism": "sdk", "auth": "cli-login"})


def test_apikey_requires_key():
    with pytest.raises(ValueError):
        llm.set_llm_config({"mechanism": "cli", "auth": "apikey"})
    eff = llm.set_llm_config({"mechanism": "cli", "auth": "apikey", "api_key": "sk-test"})
    assert eff["api_key"] == "sk-test"


def test_vertex_requires_project():
    with pytest.raises(ValueError):
        llm.set_llm_config({"mechanism": "cli", "auth": "vertex"})  # no project anywhere


def test_status_hides_key():
    llm.set_llm_config({"mechanism": "cli", "auth": "apikey", "api_key": "sk-secret"})
    st = llm.llm_status()
    assert st["has_api_key"] is True
    assert "api_key" not in st
    assert st["auth"] == "apikey"


def test_failed_set_restores_previous_override():
    llm.set_llm_config({"mechanism": "cli", "auth": "cli-login"})
    with pytest.raises(ValueError):
        llm.set_llm_config({"mechanism": "sdk", "auth": "cli-login"})
    assert llm.resolve_llm_config()["auth"] == "cli-login"  # unchanged
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_llm_config.py -v`
Expected: FAIL (AttributeError: module has no `_OVERRIDE` / `resolve_llm_config`).

- [ ] **Step 3: Implement the config layer**

In `backend/xgraph_gateway/llm.py`, insert after the imports (before `def _llm`):

```python
# ── Gateway-global LLM route config (override > env > default) ────────────────
# In-memory only: a gateway restart falls back to env/.env defaults.
_OVERRIDE: dict = {}
_ALLOWED_KEYS = ("mechanism", "auth", "project", "region", "model", "api_key")
VALID_COMBOS = {
    ("cli", "vertex"), ("cli", "apikey"), ("cli", "cli-login"),
    ("sdk", "apikey"), ("sdk", "vertex"),
}
_DEFAULT_MODEL = "claude-opus-4-8"


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes")


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

    mechanism = pick("mechanism", default="cli")

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
    elif os.environ.get("ANTHROPIC_DEFAULT_OPUS_MODEL"):
        model, src["model"] = os.environ["ANTHROPIC_DEFAULT_OPUS_MODEL"], "env"
    else:
        model, src["model"] = _DEFAULT_MODEL, "default"

    return {"mechanism": mechanism, "auth": auth, "project": project,
            "region": region, "model": model, "api_key": api_key, "sources": src}


def validate_llm_config(cfg: dict) -> dict:
    if (cfg["mechanism"], cfg["auth"]) not in VALID_COMBOS:
        raise ValueError(f"invalid LLM route: mechanism={cfg['mechanism']} auth={cfg['auth']}")
    if cfg["auth"] == "apikey" and not cfg.get("api_key"):
        raise ValueError("API key required for auth=apikey")
    if cfg["auth"] == "vertex" and not cfg.get("project"):
        raise ValueError("project required for auth=vertex")
    return cfg


def get_llm_config() -> dict:
    return dict(_OVERRIDE)


def set_llm_config(cfg: dict) -> dict:
    """Replace the override with the whitelisted keys of `cfg`; validate; return
    the effective config. Restores the previous override on failure."""
    global _OVERRIDE
    prev = dict(_OVERRIDE)
    _OVERRIDE = {k: v for k, v in (cfg or {}).items() if k in _ALLOWED_KEYS and v not in (None, "")}
    try:
        return validate_llm_config(resolve_llm_config())
    except Exception:
        _OVERRIDE = prev
        raise


def llm_status() -> dict:
    """Safe projection for the UI — never includes the raw api_key."""
    eff = resolve_llm_config()
    return {"mechanism": eff["mechanism"], "auth": eff["auth"], "project": eff["project"],
            "region": eff["region"], "model": eff["model"], "sources": eff["sources"],
            "has_api_key": bool(eff.get("api_key"))}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_llm_config.py -v`
Expected: PASS (all 8).

- [ ] **Step 5: Checkpoint (no commit)** — run the full suite `./.venv/bin/python -m pytest tests/ -q`; confirm no regressions. Do NOT `git commit`.

---

### Task 3: `llm.py` — `_llm` consumes the effective config

Rewrite `_llm` and the two helpers to select mechanism/auth from `resolve_llm_config()` and build an explicit subprocess env (CLI) or client (SDK). This is the change that actually makes Vertex work regardless of ambient env.

**Files:**
- Modify: `backend/xgraph_gateway/llm.py` (`_llm`, `_llm_claude_cli`, `_llm_claude_sdk`)
- Test: `backend/tests/test_llm_dispatch.py` (create)

**Interfaces:**
- Consumes: `resolve_llm_config()`, `validate_llm_config()` (Task 2).
- Produces (unchanged public signature): `_llm(prompt, *, schema=None, model=None)`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_llm_dispatch.py`:

```python
import json
import types
import pytest
from xgraph_gateway import llm


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    llm._OVERRIDE = {}
    for k in ("CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_API_KEY", "ANTHROPIC_VERTEX_PROJECT_ID",
              "CLOUD_ML_REGION", "XGRAPH_LLM_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "XGRAPH_LLM"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(llm.shutil, "which", lambda _: "/usr/bin/claude")
    yield
    llm._OVERRIDE = {}


def _fake_run(captured):
    def run(cmd, **kw):
        captured["cmd"] = cmd
        captured["env"] = kw.get("env")
        return types.SimpleNamespace(returncode=0, stdout=json.dumps({"is_error": False, "result": "hi"}), stderr="")
    return run


def test_cli_vertex_sets_vertex_env(monkeypatch):
    cap = {}
    monkeypatch.setattr(llm.subprocess, "run", _fake_run(cap))
    llm.set_llm_config({"mechanism": "cli", "auth": "vertex", "project": "proj-x", "region": "global"})
    assert llm._llm("hello") == "hi"
    assert cap["env"]["CLAUDE_CODE_USE_VERTEX"] == "1"
    assert cap["env"]["ANTHROPIC_VERTEX_PROJECT_ID"] == "proj-x"
    assert cap["env"]["CLOUD_ML_REGION"] == "global"
    assert "ANTHROPIC_API_KEY" not in cap["env"]


def test_cli_apikey_sets_key_and_drops_vertex(monkeypatch):
    cap = {}
    monkeypatch.setattr(llm.subprocess, "run", _fake_run(cap))
    llm.set_llm_config({"mechanism": "cli", "auth": "apikey", "api_key": "sk-test"})
    llm._llm("hello")
    assert cap["env"]["ANTHROPIC_API_KEY"] == "sk-test"
    assert "CLAUDE_CODE_USE_VERTEX" not in cap["env"]


def test_cli_login_drops_both(monkeypatch):
    cap = {}
    monkeypatch.setattr(llm.subprocess, "run", _fake_run(cap))
    llm.set_llm_config({"mechanism": "cli", "auth": "cli-login"})
    llm._llm("hello")
    assert "ANTHROPIC_API_KEY" not in cap["env"]
    assert "CLAUDE_CODE_USE_VERTEX" not in cap["env"]


def test_sdk_vertex_builds_anthropic_vertex(monkeypatch):
    calls = {}

    class FakeMsg:
        def __init__(self): self.content = [types.SimpleNamespace(type="text", text="sdk-hi")]

    class FakeClient:
        def __init__(self, **kw): calls["init"] = kw
        @property
        def messages(self):
            outer = self
            return types.SimpleNamespace(create=lambda **kw: (calls.__setitem__("create", kw), FakeMsg())[1])

    fake_anthropic = types.SimpleNamespace(Anthropic=FakeClient, AnthropicVertex=FakeClient)
    monkeypatch.setitem(__import__("sys").modules, "anthropic", fake_anthropic)
    llm.set_llm_config({"mechanism": "sdk", "auth": "vertex", "project": "proj-x", "region": "global"})
    assert llm._llm("hello") == "sdk-hi"
    assert calls["init"] == {"project_id": "proj-x", "region": "global"}


def test_stub_still_raises(monkeypatch):
    monkeypatch.setenv("XGRAPH_LLM", "stub")
    with pytest.raises(RuntimeError):
        llm._llm("x")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_llm_dispatch.py -v`
Expected: FAIL (`_llm` doesn't pass `env=` / doesn't branch on config yet).

- [ ] **Step 3: Rewrite `_llm` and helpers**

Replace `_llm`, `_llm_claude_cli`, `_llm_claude_sdk` in `backend/xgraph_gateway/llm.py` with:

```python
def _llm(prompt: str, *, schema: Optional[dict] = None, model: Optional[str] = None) -> Any:
    """Return a dict (when schema given) or str. Honors XGRAPH_LLM=stub.

    Route (mechanism × auth) comes from resolve_llm_config() — override > env >
    default. `model` overrides the per-call model."""
    if os.environ.get("XGRAPH_LLM") == "stub":
        raise RuntimeError(
            "XGRAPH_LLM=stub: ask/explain need a real LLM backend "
            "(claude CLI or ANTHROPIC_API_KEY)")
    cfg = validate_llm_config(resolve_llm_config())
    if cfg["mechanism"] == "sdk":
        return _llm_claude_sdk(prompt, schema, model, cfg)
    if not shutil.which("claude"):
        raise RuntimeError("mechanism=cli but no `claude` CLI on PATH — pick SDK or install the CLI")
    return _llm_claude_cli(prompt, schema, model, cfg)


def _cli_env(cfg: dict) -> dict:
    """A copy of os.environ with exactly the auth vars the chosen route needs —
    so the `claude` subprocess authenticates the same way regardless of how the
    gateway itself was launched."""
    env = dict(os.environ)
    if cfg["auth"] == "vertex":
        env["CLAUDE_CODE_USE_VERTEX"] = "1"
        if cfg.get("project"):
            env["ANTHROPIC_VERTEX_PROJECT_ID"] = cfg["project"]
        if cfg.get("region"):
            env["CLOUD_ML_REGION"] = cfg["region"]
        env.pop("ANTHROPIC_API_KEY", None)
    elif cfg["auth"] == "apikey":
        env["ANTHROPIC_API_KEY"] = cfg["api_key"]
        env.pop("CLAUDE_CODE_USE_VERTEX", None)
    else:  # cli-login: use the CLI's own stored credentials
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("CLAUDE_CODE_USE_VERTEX", None)
    return env


def _llm_claude_cli(prompt: str, schema: Optional[dict], model: Optional[str], cfg: dict) -> Any:
    cmd = ["claude", "-p", "--output-format", "json"]
    if schema is not None:
        cmd += ["--json-schema", json.dumps(schema)]
    m = model or cfg.get("model")
    if m:
        cmd += ["--model", m]
    cmd.append(prompt)
    proc = subprocess.run(cmd, capture_output=True, text=True, env=_cli_env(cfg),
                          timeout=int(os.environ.get("XGRAPH_LLM_TIMEOUT", "180")))
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p failed (rc={proc.returncode}): {proc.stderr.strip()[:400]}")
    wrapper = json.loads(proc.stdout)
    if wrapper.get("is_error"):
        raise RuntimeError(f"claude -p returned error: {wrapper.get('result') or wrapper}")
    if schema is not None:
        out = wrapper.get("structured_output")
        return out if out is not None else json.loads(wrapper.get("result", "{}"))
    return wrapper.get("result", "")


def _llm_claude_sdk(prompt: str, schema: Optional[dict], model: Optional[str], cfg: dict) -> Any:
    import anthropic
    if cfg["auth"] == "vertex":
        client = anthropic.AnthropicVertex(project_id=cfg["project"], region=cfg["region"])
    else:
        client = anthropic.Anthropic(api_key=cfg.get("api_key") or None)
    m = model or cfg.get("model") or _DEFAULT_MODEL
    resp = client.messages.create(model=m, max_tokens=2048,
                                  messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in resp.content if b.type == "text")
    if schema is None:
        return text
    mobj = re.search(r"\{.*\}", text, re.DOTALL)
    return json.loads(mobj.group(0)) if mobj else {}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_llm_dispatch.py -v`
Expected: PASS (all 5).

- [ ] **Step 5: Checkpoint (no commit)** — full suite `./.venv/bin/python -m pytest tests/ -q` green. Do NOT `git commit`.

---

### Task 4: `app.py` — load `backend/.env` at startup + `.env.example`

Make Vertex the default out of the box so the gateway no longer depends on the launching shell.

**Files:**
- Modify: `backend/xgraph_gateway/app.py` (add `_load_backend_env` + a module-level call, near the top after imports)
- Create: `backend/.env.example`
- Test: `backend/tests/test_env_loading.py` (create)

**Interfaces:**
- Produces: `_load_backend_env(path: str | None = None) -> pathlib.Path` — loads a dotenv file (defaults to `backend/.env`) with `override=False`; returns the resolved path (loads nothing if it doesn't exist).

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_env_loading.py`:

```python
import os


def test_load_backend_env_reads_file(tmp_path, monkeypatch):
    monkeypatch.delenv("XG_TEST_ENV_VAR", raising=False)
    f = tmp_path / ".env"
    f.write_text("XG_TEST_ENV_VAR=from_dotenv\n")
    from xgraph_gateway.app import _load_backend_env
    _load_backend_env(str(f))
    assert os.environ["XG_TEST_ENV_VAR"] == "from_dotenv"


def test_load_backend_env_missing_is_noop(tmp_path):
    from xgraph_gateway.app import _load_backend_env
    p = _load_backend_env(str(tmp_path / "nope.env"))
    assert str(p).endswith("nope.env")  # returns path, does not raise


def test_load_backend_env_does_not_override_existing(tmp_path, monkeypatch):
    monkeypatch.setenv("XG_TEST_ENV_VAR2", "already_set")
    f = tmp_path / ".env"
    f.write_text("XG_TEST_ENV_VAR2=from_dotenv\n")
    from xgraph_gateway.app import _load_backend_env
    _load_backend_env(str(f))
    assert os.environ["XG_TEST_ENV_VAR2"] == "already_set"  # override=False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_env_loading.py -v`
Expected: FAIL (ImportError: cannot import `_load_backend_env`).

- [ ] **Step 3: Implement env loading**

In `backend/xgraph_gateway/app.py`, add after the existing imports (after line 13, `from .sessions import SessionStore`):

```python
from pathlib import Path


def _load_backend_env(path: "str | None" = None) -> Path:
    """Load backend/.env into os.environ (override=False) so the LLM route
    defaults (Vertex project/region, etc.) are present no matter which shell
    launched the gateway. No-op if the file is absent."""
    p = Path(path) if path else Path(__file__).resolve().parent.parent / ".env"
    if p.exists():
        from dotenv import load_dotenv
        load_dotenv(p, override=False)
    return p


_load_backend_env()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_env_loading.py -v`
Expected: PASS (all 3).

- [ ] **Step 5: Create `backend/.env.example`**

```bash
cat > backend/.env.example <<'EOF'
# xGraph gateway environment — copy to backend/.env (git-ignored) and edit.
# The gateway loads this at startup so the LLM route works regardless of the
# shell it was launched from.

# ── Claude via GCP Vertex (default route) ────────────────────────────────────
CLAUDE_CODE_USE_VERTEX=1
ANTHROPIC_VERTEX_PROJECT_ID=team-warehouse-workhorses-dev
CLOUD_ML_REGION=global
# Vertex auth uses server-side GCP ADC:
#   gcloud auth application-default login
ANTHROPIC_DEFAULT_OPUS_MODEL=claude-opus-4-8

# ── Alternative: direct Anthropic API key (leave Vertex vars unset if using) ──
# ANTHROPIC_API_KEY=sk-ant-...
EOF
```

- [ ] **Step 6: Verify `.env` is git-ignored (do NOT create a real `backend/.env` that could be committed)**

Run: `git check-ignore backend/.env || echo "WARN: backend/.env not ignored — add it to .gitignore"`
If not ignored, add `backend/.env` to the repo `.gitignore`.

- [ ] **Step 7: Checkpoint (no commit)** — full suite green. Do NOT `git commit`.

---

### Task 5: `app.py` — `/llm_config` + `/llm_status` endpoints

Expose the config layer over HTTP for the UI.

**Files:**
- Modify: `backend/xgraph_gateway/app.py` (add two routes inside `create_app`, near the `/synthesize` route ~line 456)
- Test: `backend/tests/test_llm_endpoints.py` (create)

**Interfaces:**
- Consumes: `llm.set_llm_config`, `llm.llm_status` (Task 2), `_err` (existing).
- Produces: `GET /llm_status` → `llm_status()` dict; `POST /llm_config` (body = config dict) → `llm_status()` on success, error envelope with status 400 on invalid config.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_llm_endpoints.py`:

```python
import pytest
from fastapi.testclient import TestClient
from xgraph_gateway.app import create_app
from xgraph_gateway.adapters.fake import FakeAdapter
from xgraph_gateway import llm


@pytest.fixture
def client(monkeypatch):
    llm._OVERRIDE = {}
    for k in ("CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_API_KEY", "ANTHROPIC_VERTEX_PROJECT_ID",
              "CLOUD_ML_REGION", "XGRAPH_LLM_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL"):
        monkeypatch.delenv(k, raising=False)
    yield TestClient(create_app(adapter_factory=lambda e: FakeAdapter()))
    llm._OVERRIDE = {}


def test_status_shape_and_no_key(client):
    r = client.get("/llm_status")
    assert r.status_code == 200
    body = r.json()
    assert set(["mechanism", "auth", "model", "has_api_key", "sources"]).issubset(body)
    assert "api_key" not in body


def test_config_roundtrip_vertex(client):
    r = client.post("/llm_config", json={"mechanism": "cli", "auth": "vertex",
                                         "project": "proj-x", "region": "global"})
    assert r.status_code == 200
    assert r.json()["auth"] == "vertex"
    # persists for the next status read (gateway-global)
    assert client.get("/llm_status").json()["project"] == "proj-x"


def test_config_apikey_not_echoed(client):
    r = client.post("/llm_config", json={"mechanism": "sdk", "auth": "apikey", "api_key": "sk-secret"})
    assert r.status_code == 200
    assert r.json()["has_api_key"] is True
    assert "sk-secret" not in r.text


def test_invalid_combo_400(client):
    r = client.post("/llm_config", json={"mechanism": "sdk", "auth": "cli-login"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "ValueError"


def test_apikey_without_key_400(client):
    r = client.post("/llm_config", json={"mechanism": "cli", "auth": "apikey"})
    assert r.status_code == 400
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_llm_endpoints.py -v`
Expected: FAIL (404 — routes don't exist).

- [ ] **Step 3: Add the routes**

In `backend/xgraph_gateway/app.py`, add `from . import llm` to the top-level imports (after `from . import extract_fold` on line 11), then insert inside `create_app` right after the `/synthesize` route (after its closing at ~line 464):

```python
    @app.get("/llm_status")
    def llm_status_ep():
        return llm.llm_status()

    @app.post("/llm_config")
    def llm_config_ep(payload: dict = Body(...)):
        try:
            llm.set_llm_config(payload)
            return llm.llm_status()
        except Exception as e:
            return _err("", e)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_llm_endpoints.py -v`
Expected: PASS (all 5).

- [ ] **Step 5: Checkpoint (no commit)** — full suite green. Do NOT `git commit`.

---

### Task 6: `gateway.js` — `getLlmStatus()` / `setLlmConfig()`

Client methods for the two new (session-independent, global) endpoints.

**Files:**
- Modify: `frontend/gateway.js` (add two methods to the object returned by `makeClient`, after `extract:` ~line 164)
- Test: `frontend/tests/test_client.mjs` (append two cases)

**Interfaces:**
- Consumes: `getJSON`, `postJSON` (existing internal helpers). These endpoints are gateway-global → use plain `base + path` (NOT `q()` / `withSessionOrEngine`).
- Produces: `client.getLlmStatus() -> Promise<status>`; `client.setLlmConfig(cfg) -> Promise<status>`.

- [ ] **Step 1: Write the failing tests**

Append to `frontend/tests/test_client.mjs` (follow the file's existing fake-`fetch` + assertion style; adapt the harness names to those already used in the file):

```javascript
// ── /llm_status + /llm_config (gateway-global, no session/engine params) ──────
{
  const calls = [];
  const fakeFetch = async (url, opts) => {
    calls.push({ url, opts });
    return { json: async () => ({ mechanism: "cli", auth: "vertex", has_api_key: false }) };
  };
  const client = gw.makeClient("http://gw", fakeFetch);

  const st = await client.getLlmStatus();
  assert.equal(st.auth, "vertex");
  assert.ok(calls[0].url.endsWith("/llm_status"));
  assert.ok(!calls[0].url.includes("session="), "llm_status must not carry a session param");

  await client.setLlmConfig({ mechanism: "cli", auth: "vertex", project: "p" });
  assert.ok(calls[1].url.endsWith("/llm_config"));
  assert.equal(calls[1].opts.method, "POST");
  const sent = JSON.parse(calls[1].opts.body);
  assert.equal(sent.project, "p");
  assert.ok(!("session" in sent), "llm_config body must not carry a session");
  console.log("ok - llm status/config client");
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && node tests/test_client.mjs`
Expected: FAIL (`client.getLlmStatus is not a function`).

- [ ] **Step 3: Add the client methods**

In `frontend/gateway.js`, inside the object returned by `makeClient`, add after the `extract:` method (line ~164, before the closing `};`):

```javascript
      getLlmStatus: function () { return getJSON(base + "/llm_status"); },
      setLlmConfig: function (cfg) { return postJSON("/llm_config", cfg || {}); },
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && node tests/test_client.mjs`
Expected: PASS (existing cases + the new one).

- [ ] **Step 5: Checkpoint (no commit)** — also run `node tests/test_transforms.mjs` (should be unaffected). Do NOT `git commit`.

---

### Task 7: `XGraph.html` — LLM route box in the Setup panel

Replace the current single-option LLM block with a two-axis picker + Apply + live status. Editing discipline: anchored search-and-replace against verbatim strings; validate with the Babel transpile check + `curl` 200; real behavior is browser-verified by the user.

**Files:**
- Modify: `frontend/XGraph.html` — SetupPanel props (~6522-6529), the LLM `<div style={group}>` block (~6599-6619), App state/handlers (~7887-7960), and the `<SetupPanel .../>` usage (~9344).

**Interfaces:**
- Consumes: `client.getLlmStatus()`, `client.setLlmConfig(cfg)` (Task 6); existing `llmConn`/`setLlmConn` state and `gwClient`.
- Produces: new `llmConn` fields `mechanism`, `auth`, `project`, `region`, `model`; App state `llmStatus`; App handler `applyLlm`.

- [ ] **Step 1: Add App state + handler (near the other Setup state)**

Find (App, ~line 7951):

```javascript
    const [gatewayBase, setGatewayBase] = useState(GATEWAY_BASE);
```

Insert immediately after it:

```javascript
    const [llmStatus, setLlmStatus] = useState(null);
    const applyLlm = React.useCallback(async function () {
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
    }, [gwClient, llmConn]);
    React.useEffect(function () {
        gwClient.getLlmStatus().then(setLlmStatus).catch(function () { setLlmStatus(null); });
    }, [gwClient]);
```

> NOTE: `llmConn`/`setLlmConn` and `gwClient` already exist in App (search to confirm the exact declarations; `gwClient` is the `useMemo` at ~7958, `llmConn` is passed to SetupPanel at ~9344). If `llmConn` is not declared in App with `useState`, add `const [llmConn, setLlmConn] = useState({});` alongside the other Setup state and thread it — but per the current SetupPanel props it already exists upstream.

- [ ] **Step 2: Pass the new props to SetupPanel**

Find the `<SetupPanel` usage (~line 9344) and add these props to it (alongside the existing `llmConn`/`setLlmConn`):

```jsx
                        llmStatus={llmStatus} onApplyLlm={applyLlm}
```

- [ ] **Step 3: Destructure the new props in SetupPanel**

Find (line ~6527-6528):

```javascript
    var llmConn = props.llmConn, setLlmConn = props.setLlmConn;
    var gatewayBase = props.gatewayBase, setGatewayBase = props.setGatewayBase;
```

Replace with:

```javascript
    var llmConn = props.llmConn, setLlmConn = props.setLlmConn;
    var llmStatus = props.llmStatus, onApplyLlm = props.onApplyLlm;
    var gatewayBase = props.gatewayBase, setGatewayBase = props.setGatewayBase;
```

- [ ] **Step 4: Replace the LLM block with the two-axis picker**

Replace the entire LLM `<div style={group}>` block (from the comment `{/* LLM — single option for now ...`  through its closing `</div>` at ~line 6619) with:

```jsx
            {/* LLM route — Mechanism × Auth. Config is gateway-global; Apply
                POSTs /llm_config and the status line reflects /llm_status. */}
            <div style={group}>
                <Label>LLM route</Label>
                <div style={{ display:'flex', gap:12, marginBottom:10 }}>
                    <div style={{ flex:1 }}>
                        <Label>Mechanism</Label>
                        <select style={inp} value={llmConn.mechanism || 'cli'} onChange={function(e){ setLlmField('mechanism', e.target.value); }}>
                            <option value="cli">claude CLI</option>
                            <option value="sdk">Anthropic SDK</option>
                        </select>
                    </div>
                    <div style={{ flex:1 }}>
                        <Label>Auth</Label>
                        <select style={inp} value={llmConn.auth || 'vertex'} onChange={function(e){ setLlmField('auth', e.target.value); }}>
                            <option value="vertex">GCP Vertex</option>
                            <option value="apikey">API key</option>
                            {(llmConn.mechanism || 'cli') === 'cli' ? <option value="cli-login">CLI login</option> : null}
                        </select>
                    </div>
                </div>
                {(llmConn.auth || 'vertex') === 'vertex' ? (
                    <div>
                        <Label>Vertex project</Label>
                        <input style={inp} value={llmConn.project || ''} onChange={function(e){ setLlmField('project', e.target.value); }} placeholder="team-warehouse-workhorses-dev" />
                        <Label>Region</Label>
                        <input style={inp} value={llmConn.region || ''} onChange={function(e){ setLlmField('region', e.target.value); }} placeholder="global" />
                        <p style={{ fontSize:11, color:'#b2bec3', margin:'0 0 8px' }}>Auth uses server-side GCP ADC (gcloud application-default). The browser sends only project/region.</p>
                    </div>
                ) : (llmConn.auth || 'vertex') === 'apikey' ? (
                    <div>
                        <Label>API key</Label>
                        <input type={pwType} autoComplete="new-password" style={inp} value={llmConn.apiKey || ''} onChange={function(e){ setLlmField('apiKey', e.target.value); }} placeholder="sk-ant-…" />
                        <p style={{ fontSize:11, color:'#b2bec3', margin:'0 0 8px' }}>Sent to the gateway over localhost. Not stored in /llm_status.</p>
                    </div>
                ) : (
                    <p style={{ fontSize:12, color:'#b2bec3', margin:'0 0 8px' }}>Uses the claude CLI's own stored login.</p>
                )}
                <Label>Model (optional)</Label>
                <input style={inp} value={llmConn.model || ''} onChange={function(e){ setLlmField('model', e.target.value); }} placeholder="claude-opus-4-8" />
                <button type="button" onClick={onApplyLlm} style={{
                    padding:'8px 16px', border:'none', borderRadius:6, cursor:'pointer',
                    fontWeight:700, color:'#fff', fontSize:12, fontFamily:'inherit',
                    background:'linear-gradient(135deg,#0984e3,#0872c4)',
                }}>Apply LLM route</button>
                {llmStatus && (llmStatus.error ? (
                    <p style={{ fontSize:11, color:'#d63031', margin:'8px 0 0', fontWeight:600 }}>LLM: {llmStatus.error}</p>
                ) : (
                    <p style={{ fontSize:11, color:'#00b894', margin:'8px 0 0', fontWeight:600 }}>
                        LLM: {llmStatus.mechanism} · {llmStatus.auth}
                        {llmStatus.project ? ' · ' + llmStatus.project : ''}
                        {llmStatus.model ? ' · ' + llmStatus.model : ''}
                        {llmStatus.auth === 'apikey' ? (llmStatus.has_api_key ? ' · key set' : ' · NO key') : ''}
                    </p>
                ))}
                <Label>Extraction mode</Label>
                <div style={{ marginTop:4 }}>
                    <label style={radioLbl}><input type="radio" checked={(llmConn.extractMode || 'sequential') === 'sequential'} onChange={function(){ setLlmField('extractMode', 'sequential'); }}/> Sequential</label>
                    <label style={radioLbl}><input type="radio" checked={llmConn.extractMode === 'parallel'} onChange={function(){ setLlmField('extractMode', 'parallel'); }}/> Parallel</label>
                    <label style={radioLbl}><input type="radio" checked={llmConn.extractMode === 'whole'} onChange={function(){ setLlmField('extractMode', 'whole'); }}/> Whole-doc</label>
                </div>
                <p style={{ fontSize:11, color:'#b2bec3', margin:'4px 0 0' }}>Sequential: one paragraph per LLM call (default). Parallel: paragraphs concurrent. Whole-doc: one call — keeps relations that span paragraphs.</p>
            </div>
```

- [ ] **Step 5: Validate the Babel transpile (syntax check)**

Run the project's transpile check (the CLAUDE.md-documented method — run the `<script type="text/babel">` block through `@babel/standalone` in Node). Use the existing frontend verification approach in `frontend/tests/VERIFY.md`.
Expected: transpiles with no syntax error.

- [ ] **Step 6: Validate the gateway serves the page (200)**

```bash
./xgraph restart && sleep 2 && curl -fsS -o /dev/null -w "%{http_code}\n" http://localhost:8090/
```
Expected: `200`. (Restart is from THIS shell so the gateway also picks up `.env` / Vertex — the immediate unblock.)

- [ ] **Step 7: Browser acceptance (user-driven)**

Ask the user to open http://localhost:8090/, go to Setup → LLM route, confirm the status line shows `cli · vertex · …`, run an **Ask**, and confirm an answer returns. Then try switching Auth → API key + Apply and confirm the status line updates.

- [ ] **Step 8: Checkpoint (no commit)** — backend + frontend suites green, page 200. Do NOT `git commit`.

---

## Self-Review

**Spec coverage:**
- Two-axis config model → Tasks 2, 7. ✅
- `llm.py` config holder, no `nlcypher`/`extract` change → Tasks 2, 3. ✅
- `.env` startup loading + `.env.example` → Task 4. ✅
- `POST /llm_config` + `GET /llm_status` (has_api_key bool, never key) → Task 5. ✅
- Setup-panel LLM box + `gateway.js` methods → Tasks 6, 7. ✅
- Testing (llm unit, endpoint, gateway.js, transpile) → Tasks 2,3,5,6,7. ✅
- Security notes (localhost key, no key in status, server-side ADC) → enforced in Tasks 2/5, surfaced in Task 7 UI copy. ✅
- Deferred items (per-request config, remote creds, persistence) → not implemented, matches spec. ✅

**Placeholder scan:** No TBD/TODO; every code step has concrete content. Task 7 Step 1 carries a NOTE to confirm `llmConn` is declared in App — this is a verification instruction, not a placeholder (the code to add is fully specified).

**Type consistency:** `set_llm_config`/`resolve_llm_config`/`llm_status`/`validate_llm_config` names and shapes match across Tasks 2/3/5. `getLlmStatus`/`setLlmConfig` consistent across Tasks 6/7. Config field names (`mechanism`,`auth`,`project`,`region`,`model`,`api_key`) consistent; UI uses `llmConn.apiKey` mapped to payload `api_key` in Task 7 Step 1. ✅
