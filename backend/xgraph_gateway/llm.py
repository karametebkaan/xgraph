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
