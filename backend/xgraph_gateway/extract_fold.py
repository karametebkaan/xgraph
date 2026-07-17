"""Extraction label folding: rewrite LLM-proposed entity/relation type labels
to their canonical forms, learning aliases as it goes.

Engine-neutral: depends only on a duck-typed metadata store (the ComputeEngine
methods record_type / resolve_canonical / get_canonicals) and an injectable
LLM func `llm(prompt, *, schema=None)`. Ported from kgr `ontology.py`
(resolve_canonical / fold_check_via_llm / fold_proposal), single-canonical
subset; facet handling is layered on in a later task.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

LLMFunc = Callable[..., Any]

_ENTITY_AXIS = "EntityType"
_RELATION_AXIS = "RelationType"

_llm_fn: Optional[LLMFunc] = None


def _get_llm() -> LLMFunc:
    """Lazily bind the local `_llm` (mirrors extract.py) so importing this
    module never requires the `claude` CLI, and tests inject a fake."""
    global _llm_fn
    if _llm_fn is None:
        from .llm import _llm
        _llm_fn = _llm
    return _llm_fn


_FOLD_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["canonical"],
    "properties": {"canonical": {"type": ["string", "null"]}},
}


def fold_check_via_llm(kind, proposed_name, existing_canonicals, llm):
    """Ask the LLM whether `proposed_name` is a synonym of an existing
    canonical. Returns the canonical to fold into, or None. Never raises:
    on any error/absence, returns None (treat as new canonical)."""
    if not existing_canonicals:
        return None
    prompt = (
        f"You decide whether a newly-proposed {kind} type name is a synonym of "
        f"any existing canonical type in the ontology.\n\n"
        f"Proposed {kind} type: {proposed_name}\n"
        f"Existing canonical {kind} types: {', '.join(sorted(existing_canonicals))}\n\n"
        f"If the proposed type is semantically the same as one of the existing "
        f"canonicals, return that canonical's exact name. Otherwise return null.\n"
        f'Reply with only JSON: {{"canonical": "<existing name>"}} or {{"canonical": null}}.'
    )
    try:
        out = llm(prompt, schema=_FOLD_SCHEMA)
        if isinstance(out, str):
            import json
            out = json.loads(out)
        canonical = (out or {}).get("canonical")
        if isinstance(canonical, str) and canonical in existing_canonicals:
            return canonical
    except Exception:
        return None
    return None


def _resolve_one(store, graph, kind, name, axis, llm, cache, report, source_uri):
    """Resolve a single (kind, name) to a canonical, learning + persisting the
    decision. `cache` dedupes within one call; `report` accumulates folds."""
    name = (name or "").strip()
    if not name:
        return name
    key = (kind, name)
    if key in cache:
        return cache[key]

    canonical = store.resolve_canonical(graph, kind, name)
    if canonical is None:
        existing = store.get_canonicals(graph, kind)
        fold_to = fold_check_via_llm(kind, name, existing, llm)
        if fold_to:
            store.record_type(graph, kind, name, fold_to, axis, source_uri)
            canonical = fold_to
        else:
            store.record_type(graph, kind, name, name, axis, source_uri)
            canonical = name

    if canonical != name:
        report.append({"kind": kind, "from": name, "to": canonical, "axis": axis})
    cache[key] = canonical
    return canonical


def fold_labels(store, graph, entities, relations, source_uri, llm=None):
    """Rewrite each entity/relation `label` to its canonical, in place.
    Returns a report list of the folds applied `[{kind, from, to, axis}]`."""
    llm = llm or _get_llm()
    cache: dict = {}
    report: list = []
    for e in entities:
        e["label"] = _resolve_one(store, graph, "entity",
                                  e.get("label", ""), _ENTITY_AXIS, llm, cache, report,
                                  source_uri)
    for r in relations:
        r["label"] = _resolve_one(store, graph, "relation",
                                  r.get("label", ""), _RELATION_AXIS, llm, cache, report,
                                  source_uri)
    return report
