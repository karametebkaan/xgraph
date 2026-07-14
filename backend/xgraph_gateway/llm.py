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


def _llm(prompt: str, *, schema: Optional[dict] = None) -> Any:
    """Return a dict (when schema given) or str. Honors XGRAPH_LLM=stub."""
    if os.environ.get("XGRAPH_LLM") == "stub":
        raise RuntimeError(
            "XGRAPH_LLM=stub: ask/explain need a real LLM backend "
            "(claude CLI or ANTHROPIC_API_KEY)")
    if shutil.which("claude"):
        return _llm_claude_cli(prompt, schema)
    if os.environ.get("ANTHROPIC_API_KEY"):
        return _llm_claude_sdk(prompt, schema)
    raise RuntimeError("no LLM backend: install the `claude` CLI or set ANTHROPIC_API_KEY")


def _llm_claude_cli(prompt: str, schema: Optional[dict]) -> Any:
    cmd = ["claude", "-p", "--output-format", "json"]
    if schema is not None:
        cmd += ["--json-schema", json.dumps(schema)]
    model = os.environ.get("XGRAPH_LLM_MODEL")
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


def _llm_claude_sdk(prompt: str, schema: Optional[dict]) -> Any:
    import anthropic
    client = anthropic.Anthropic()
    model = os.environ.get("XGRAPH_LLM_MODEL", "claude-opus-4-7")
    resp = client.messages.create(model=model, max_tokens=2048,
                                  messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in resp.content if b.type == "text")
    if schema is None:
        return text
    m = re.search(r"\{.*\}", text, re.DOTALL)
    return json.loads(m.group(0)) if m else {}
