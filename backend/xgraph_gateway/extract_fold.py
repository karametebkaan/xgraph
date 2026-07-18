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


def _resolve_one(store, graph, kind, name, axis, llm, cache, report, source_uri, pre_canon):
    """Resolve a single (kind, name) to a canonical, learning + persisting the
    decision. `cache` dedupes within one call; `report` accumulates folds.

    `pre_canon[kind]` is the set of canonicals that existed BEFORE this
    extraction call — the only valid fold targets. We deliberately do NOT
    fold-check against canonicals recorded during this same call: on a fresh
    graph that set is empty, so `fold_check_via_llm` short-circuits and makes
    ZERO LLM calls (each fold-check is a slow `claude` CLI round-trip, and one
    document introducing N new types would otherwise fire N of them). Cross-run
    folding — a later document's `Firm` folding into an earlier document's
    `Company` — is preserved, because by then `Company` is pre-existing."""
    name = (name or "").strip()
    if not name:
        return name
    key = (kind, name)
    if key in cache:
        return cache[key]

    canonical = store.resolve_canonical(graph, kind, name)
    if canonical is None:
        existing = pre_canon.get(kind) or []
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
    # Snapshot the pre-existing canonicals ONCE (fold targets). Types recorded
    # during this call are intentionally excluded — see _resolve_one.
    pre_canon = {
        "entity": store.get_canonicals(graph, "entity"),
        "relation": store.get_canonicals(graph, "relation"),
    }
    for e in entities:
        raw_struct = (e.get("label") or "").strip()
        struct_canon = _resolve_one(store, graph, "entity",
                                    raw_struct, _ENTITY_AXIS, llm, cache, report,
                                    source_uri, pre_canon)
        e["label"] = struct_canon
        labels = [struct_canon] if struct_canon else []
        label_raw = [raw_struct] if raw_struct else []
        for f in e.get("facets") or []:
            f_name = (f.get("name") or "").strip()
            f_axis = (f.get("axis") or _ENTITY_AXIS).strip()
            if not f_name:
                continue
            f_canon = _resolve_one(store, graph, "entity",
                                   f_name, f_axis, llm, cache, report,
                                   source_uri, pre_canon)
            label_raw.append(f_name)
            if f_canon and f_canon not in labels:
                labels.append(f_canon)
        e["labels"] = labels
        e["label_raw"] = label_raw
    for r in relations:
        r["label"] = _resolve_one(store, graph, "relation",
                                  r.get("label", ""), _RELATION_AXIS, llm, cache, report,
                                  source_uri, pre_canon)
    return report
