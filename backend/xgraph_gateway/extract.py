"""Extract: PDF/text -> LLM entity+relationship extraction (open-ended ontology).

Mirrors `nlcypher.py`'s pattern for the local `_llm(prompt, *, schema=None)`
backend: a module-level, lazily-bound `_get_llm()` (so importing this module
never requires the `claude` CLI to be present) and an injectable `llm`
parameter on every public function, so tests can supply a fake and never
shell out to the real CLI.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Callable, Optional


LLMFunc = Callable[..., Any]

# Extraction is high-volume and structural, not reasoning-heavy — run it on a
# fast, cheap model by default (overridable). ask/explain keep the default.
EXTRACT_MODEL = os.environ.get("XGRAPH_EXTRACT_MODEL", "claude-haiku-4-5-20251001")

_llm_fn: Optional[LLMFunc] = None


def _get_llm() -> LLMFunc:
    """Lazily bind the local `_llm` (on EXTRACT_MODEL) the first time a real call
    is needed. Deferred so importing this module never requires the `claude` CLI,
    and so tests can inject a fake `llm` and never shell out.
    """
    global _llm_fn
    if _llm_fn is None:
        from .llm import _llm
        def _fast(prompt, *, schema=None):
            return _llm(prompt, schema=schema, model=EXTRACT_MODEL)
        _llm_fn = _fast
    return _llm_fn


# --- id helpers ---------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def canonical_id(name: str) -> str:
    """Deterministic id from a name: same real-world name -> same id.

    `slug(name.lower())[:48] + '-' + sha1(name.lower()).hexdigest()[:8]`.
    Mirrors kgr's `concept_id`.
    """
    lowered = name.lower()
    slug = _SLUG_RE.sub("-", lowered).strip("-")
    digest = hashlib.sha1(lowered.encode("utf-8")).hexdigest()[:8]
    return f"{slug[:48]}-{digest}"


# --- chunking -------------------------------------------------------------

_PARA_RE = re.compile(r"\n\s*\n+")

EXTRACT_MAX_CHUNKS = 40


def chunk(text: str, max_chunks: int = EXTRACT_MAX_CHUNKS) -> tuple[list[str], bool]:
    """Split `text` on blank lines into paragraphs; drop empties.

    Returns `(chunks[:max_chunks], truncated)` where `truncated` is True iff
    more paragraphs existed than `max_chunks` (no silent cap).
    """
    parts = [p.strip() for p in _PARA_RE.split(text)]
    parts = [p for p in parts if p]
    truncated = len(parts) > max_chunks
    return parts[:max_chunks], truncated


# --- document reading -------------------------------------------------------

_TEXT_EXTS = (".txt", ".md", ".markdown")


def read_document(filename: str, data: bytes) -> str:
    """Extract plain text from a `.pdf`/`.txt`/`.md`/`.markdown` file's bytes."""
    lower = filename.lower()
    if lower.endswith(".pdf"):
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    if lower.endswith(_TEXT_EXTS):
        return data.decode("utf-8", "replace")
    ext = lower.rsplit(".", 1)[-1] if "." in lower else lower
    raise ValueError(f"unsupported file type: {ext}")


# --- LLM extraction ---------------------------------------------------------

_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The entity's name, spelled consistently across mentions."},
                    "label": {"type": "string", "description": "Concise Title-Case entity type, e.g. Person, Organization, Location."},
                    "attrs": {"type": "object", "description": "Optional extra attributes about the entity."},
                    "facets": {
                        "type": "array",
                        "description": "Optional classifying facets, each a {name, axis} pair "
                                       "(e.g. {\"name\":\"AI\",\"axis\":\"Industry\"}). The primary "
                                       "structural type stays in `label`.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "axis": {"type": "string"},
                            },
                            "required": ["name", "axis"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["name", "label"],
                "additionalProperties": False,
            },
        },
        "relations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "Must equal an entity `name` from this same chunk."},
                    "target": {"type": "string", "description": "Must equal an entity `name` from this same chunk."},
                    "label": {"type": "string", "description": "UPPER_SNAKE relationship type, e.g. WORKS_AT, LOCATED_IN."},
                    "attrs": {"type": "object", "description": "Optional extra attributes about the relationship."},
                },
                "required": ["source", "target", "label"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["entities", "relations"],
    "additionalProperties": False,
}


def _prompt(chunk_text: str, hint: Optional[str]) -> str:
    hint_block = f"\nFocus on: {hint}\n" if hint else ""
    return (
        "You extract entities and relationships (open-ended ontology -- discover "
        "whatever types are present, do not force a fixed schema) from a passage of "
        "text.\n\n"
        "Rules (MUST follow):\n"
        "- Give each entity a concise Title-Case `label` (e.g. Person, Organization, "
        "Location, Event) that names its type.\n"
        "- Give each relationship an UPPER_SNAKE `label` (e.g. WORKS_AT, LOCATED_IN, "
        "FOUNDED_BY) describing how the two entities relate.\n"
        "- Optionally add `facets`: classifying dimensions of an entity beyond its "
        "structural type, each `{name, axis}` (e.g. a Company with "
        "`{\"name\":\"AI\",\"axis\":\"Industry\"}`). Keep the structural type in `label`.\n"
        "- Use the exact same `name` spelling every time the same real-world entity is "
        "mentioned (so mentions of the same thing merge together).\n"
        "- Every relation's `source` and `target` MUST equal the `name` of an entity you "
        "also returned in `entities` for this same passage.\n"
        f"{hint_block}\n"
        "Return JSON only (no markdown fences, no commentary) with `entities` "
        "(each `{name, label, facets?, attrs?}`) and `relations` (each "
        "`{source, target, label, attrs?}`).\n\n"
        f"Passage:\n{chunk_text}"
    )


# How many chunk-extraction LLM calls to run concurrently. Each call is a
# blocking `claude` subprocess, so a long document's chunks finish in roughly
# one call's wall-clock instead of N sequential ones.
EXTRACT_CONCURRENCY = int(os.environ.get("XGRAPH_EXTRACT_CONCURRENCY", "6"))


EXTRACT_MODES = ("sequential", "parallel", "whole")


def _extract_chunks(chunks: list[str], hint: Optional[str], call: LLMFunc,
                    parallel: bool = True) -> list:
    """Run the per-chunk extraction. When `parallel`, chunk calls run
    concurrently (threads release the GIL while each subprocess blocks) but
    results come back in chunk order, so the downstream merge stays
    first-seen-wins deterministic regardless of which call finishes first."""
    if len(chunks) <= 1 or not parallel:
        return [call(_prompt(c, hint), schema=_EXTRACT_SCHEMA) for c in chunks]
    import concurrent.futures
    workers = min(len(chunks), max(1, EXTRACT_CONCURRENCY))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(lambda c: call(_prompt(c, hint), schema=_EXTRACT_SCHEMA), chunks))


def extract_document(text: str, hint: Optional[str] = None, llm: Optional[LLMFunc] = None,
                      max_chunks: int = EXTRACT_MAX_CHUNKS, mode: str = "sequential") -> dict:
    """Extract and merge entities/relationships across `text`'s paragraphs.

    Calls the LLM once per chunk (`call = llm or _get_llm()`, mirroring
    `nlcypher`'s lazy `_get_llm`), then merges: entities dedupe by
    `canonical_id(name)` (first non-empty label/name wins, attrs shallow-merged
    with first-seen values winning); relations map `source`/`target` names to
    ids via that chunk's own name->id map (a relation whose endpoint isn't one
    of that chunk's entities is dropped as dangling), `id = sha1(src|dst|label)
    [:16]`, deduped by that id.

    Returns `{"entities": [{id,label,name,facets,attrs}], "relations":
    [{id,src,dst,label,attrs}], "truncated": bool}`.
    """
    call = llm or _get_llm()
    mode = mode if mode in EXTRACT_MODES else "sequential"
    if mode == "whole":
        # One LLM call over the entire document -- fewest calls, and relations
        # that span paragraphs survive (all entities are in one call's scope).
        body = text.strip()
        chunks, truncated = ([body] if body else []), False
    else:
        chunks, truncated = chunk(text, max_chunks=max_chunks)

    entities: dict[str, dict] = {}
    relations: dict[str, dict] = {}

    for out in _extract_chunks(chunks, hint, call, parallel=(mode == "parallel")):
        if isinstance(out, str):
            out = json.loads(out)

        chunk_entities = out.get("entities") or []
        chunk_relations = out.get("relations") or []

        name_to_id: dict[str, str] = {}
        for e in chunk_entities:
            name = (e.get("name") or "").strip()
            if not name:
                continue
            label = (e.get("label") or "").strip()
            attrs = e.get("attrs") or {}
            facets = e.get("facets") or []
            cid = canonical_id(name)  # case-insensitive dedup KEY only (not the NODE)
            if cid in entities:
                existing = entities[cid]
                name_to_id[name] = existing["id"]  # map this spelling to the canonical NODE
                if not existing.get("label"):
                    existing["label"] = label
                if not existing.get("facets"):
                    existing["facets"] = facets
                existing["attrs"] = {**attrs, **existing["attrs"]}
            else:
                # NODE is the readable NAME (what Visualize / queries show), not the
                # slug-hash id. Dedup still keys on canonical_id so 'Mullin'/'mullin'
                # collapse; the first-seen spelling becomes the NODE.
                entities[cid] = {"id": name, "label": label, "name": name,
                                  "facets": facets, "attrs": dict(attrs)}
                name_to_id[name] = name

        for r in chunk_relations:
            src_name = (r.get("source") or "").strip()
            dst_name = (r.get("target") or "").strip()
            label = (r.get("label") or "").strip()
            if src_name not in name_to_id or dst_name not in name_to_id:
                continue  # dangling: endpoint not among this chunk's entities
            src_id = name_to_id[src_name]
            dst_id = name_to_id[dst_name]
            rid = hashlib.sha1(f"{src_id}|{dst_id}|{label}".encode("utf-8")).hexdigest()[:16]
            if rid not in relations:
                attrs = r.get("attrs") or {}
                relations[rid] = {"id": rid, "src": src_id, "dst": dst_id, "label": label,
                                   "attrs": dict(attrs)}

    return {
        "entities": list(entities.values()),
        "relations": list(relations.values()),
        "truncated": truncated,
    }
