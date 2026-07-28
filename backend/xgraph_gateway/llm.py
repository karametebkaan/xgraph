"""LLM backend for xGraph — a small, self-contained `_llm(prompt, schema=...)`.

Extracted (originally from the kgr/graphrag project) so xGraph has no runtime
dependency on that repo. Resolution: `XGRAPH_LLM=stub` → error (tests inject a
fake instead); else the `claude` CLI if on PATH; else the Anthropic SDK if
`ANTHROPIC_API_KEY` is set.

Returns a dict when a JSON `schema` is given, otherwise a plain string.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Any, Optional


# ── Gateway-global LLM route config (override > env > default) ────────────────
# In-memory only: a gateway restart falls back to env/.env defaults.
_OVERRIDE: dict = {}
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


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes")


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
    return {"provider": eff["provider"], "mechanism": eff["mechanism"], "auth": eff["auth"],
            "project": eff["project"], "region": eff["region"], "model": eff["model"],
            "fast_model": fast_model(), "sources": eff["sources"],
            "has_api_key": bool(eff.get("api_key"))}


def _llm(prompt: str, *, schema: Optional[dict] = None, model: Optional[str] = None) -> Any:
    """Return a dict (when schema given) or str. Honors XGRAPH_LLM=stub.

    Route (mechanism × auth) comes from resolve_llm_config() — override > env >
    default. `model` overrides the per-call model."""
    if os.environ.get("XGRAPH_LLM") == "stub":
        raise RuntimeError(
            "XGRAPH_LLM=stub: ask/explain need a real LLM backend "
            "(claude CLI or ANTHROPIC_API_KEY)")
    cfg = validate_llm_config(resolve_llm_config())
    if cfg["provider"] == "gemini":
        return _llm_gemini(prompt, schema, model, cfg)
    if cfg["mechanism"] == "sdk":
        return _llm_claude_sdk(prompt, schema, model, cfg)
    if not shutil.which("claude"):
        raise RuntimeError("mechanism=cli but no `claude` CLI on PATH — pick SDK or install the CLI")
    return _llm_claude_cli(prompt, schema, model, cfg)


def warmup() -> None:
    """Best-effort: fire one tiny LLM call PER model tier so the CLI spin-up +
    GCP ADC token exchange + Vertex cold start happen BEFORE the user's first
    call (otherwise that first call pays ~a minute of cold start).

    Warms the FAST (Haiku) tier FIRST because Ask/Explain — the latency-sensitive
    interactive path — runs there; a warmup on the Build/Opus model alone leaves
    the fast tier cold on a DIFFERENT Vertex model, so the first Explain still
    paid full cold start. Then warms the Build tier (extract/fold). No-ops on any
    failure, when the LLM is stubbed, or when XGRAPH_LLM_WARMUP is falsy."""
    if os.environ.get("XGRAPH_LLM") == "stub":
        return
    if os.environ.get("XGRAPH_LLM_WARMUP", "1").lower() in ("0", "false", "no"):
        return
    build_model = resolve_llm_config().get("model")
    seen: set = set()
    for m in (fast_model(), build_model):   # fast first: interactive priority
        if not m or m in seen:
            continue
        seen.add(m)
        try:
            _llm("ok", model=m)
        except Exception:
            pass


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


def _vertex_model_id(model: str) -> str:
    """Translate an Anthropic-native model id to its Vertex publisher form.

    Vertex expects a DATED model as `<name>@<YYYYMMDD>` (claude-haiku-4-5-20251001
    -> claude-haiku-4-5@20251001); undated aliases (claude-opus-4-8) are used
    verbatim. The `claude` CLI does this mapping internally; the SDK passes the
    string straight through, so the SDK-on-Vertex route must translate here or
    Vertex 404s the model."""
    return re.sub(r"-(\d{8})$", r"@\1", model or "")


def _extract_json(text: str) -> dict:
    """Extract the first complete JSON object from an LLM completion.

    SDK/API completions may wrap the JSON in markdown fences or append a trailing
    explanation after the object. A greedy ``{.*}`` over-captures to the LAST brace,
    so ``json.loads`` then raises ``Extra data``. Instead, decode the first complete
    object found at each ``{`` and ignore everything after it. Returns ``{}`` when no
    JSON object is present."""
    dec = json.JSONDecoder()
    idx = text.find("{")
    while idx != -1:
        try:
            obj, _end = dec.raw_decode(text, idx)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        idx = text.find("{", idx + 1)
    return {}


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
    return _extract_json(text)


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
    return _extract_json(text)
