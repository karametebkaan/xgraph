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


def _llm(prompt: str, *, schema: Optional[dict] = None, model: Optional[str] = None) -> Any:
    """Return a dict (when schema given) or str. Honors XGRAPH_LLM=stub.

    `model` (optional) overrides the model for THIS call — used to run the cheap,
    high-volume paths (extraction, fold-checks) on a fast model while leaving the
    reasoning paths (ask/explain) on the default. Falls back to XGRAPH_LLM_MODEL,
    then the backend default."""
    if os.environ.get("XGRAPH_LLM") == "stub":
        raise RuntimeError(
            "XGRAPH_LLM=stub: ask/explain need a real LLM backend "
            "(claude CLI or ANTHROPIC_API_KEY)")
    # Prefer the SDK when an API key is explicitly set -- a persistent client
    # with no per-call CLI cold-start (faster on high-volume extraction). With
    # no key, fall back to the `claude` CLI (the default dev path, no key needed).
    if os.environ.get("ANTHROPIC_API_KEY"):
        return _llm_claude_sdk(prompt, schema, model)
    if shutil.which("claude"):
        return _llm_claude_cli(prompt, schema, model)
    raise RuntimeError("no LLM backend: set ANTHROPIC_API_KEY or install the `claude` CLI")


def _llm_claude_cli(prompt: str, schema: Optional[dict], model: Optional[str] = None) -> Any:
    cmd = ["claude", "-p", "--output-format", "json"]
    if schema is not None:
        cmd += ["--json-schema", json.dumps(schema)]
    model = model or os.environ.get("XGRAPH_LLM_MODEL")
    if model:
        cmd += ["--model", model]
    cmd.append(prompt)
    proc = subprocess.run(cmd, capture_output=True, text=True,
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


def _llm_claude_sdk(prompt: str, schema: Optional[dict], model: Optional[str] = None) -> Any:
    import anthropic
    client = anthropic.Anthropic()
    model = model or os.environ.get("XGRAPH_LLM_MODEL", "claude-opus-4-7")
    resp = client.messages.create(model=model, max_tokens=2048,
                                  messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in resp.content if b.type == "text")
    if schema is None:
        return text
    m = re.search(r"\{.*\}", text, re.DOTALL)
    return json.loads(m.group(0)) if m else {}
