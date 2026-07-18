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
