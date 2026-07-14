"""LLM Interact: NL -> Cypher -> run -> English round-trip.

Uses the local `_llm(prompt, *, schema=None)` backend (`xgraph_gateway.llm`) —
by default that's the `claude` CLI on PATH, returning a dict when `schema` is
given, a str otherwise. Every public function here accepts an optional
`llm` callable with that same signature so tests can inject a fake and never
shell out to the real CLI.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional


LLMFunc = Callable[..., Any]

_llm_fn: Optional[LLMFunc] = None


def _get_llm() -> LLMFunc:
    """Lazily bind the local `_llm` the first time a real call is needed.

    Deferred (rather than a top-level import) so importing this module never
    requires the `claude` CLI to be present, and so tests can inject a fake
    `llm` and never shell out.
    """
    global _llm_fn
    if _llm_fn is None:
        from .llm import _llm
        _llm_fn = _llm
    return _llm_fn


# --- read-only guard ---------------------------------------------------------

_WRITE_KW = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|ALTER|INSERT|UPDATE|TRUNCATE|LOAD\s+CSV)\b"
    r"|CALL\s+db\.",
    re.IGNORECASE,
)

_CYPHER_SCHEMA = {
    "type": "object",
    "properties": {
        "cypher": {"type": "string", "description": "A single read-only query in the requested dialect."},
    },
    "required": ["cypher"],
    "additionalProperties": False,
}

_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "description": "Plain-English answer to the question."},
    },
    "required": ["answer"],
    "additionalProperties": False,
}

_DIALECT_FALKORDB = """\
Target dialect: openCypher, as implemented by FalkorDB. Rules (MUST follow):
- Do NOT prefix the query with GRAPH "..." — FalkorDB Cypher has no GRAPH clause, it runs
  directly against the connected graph.
- Do NOT put predicates inline in the node/relationship pattern (that is Kinetica-only
  syntax). Instead, write the whole MATCH first, then a single WHERE clause after it,
  AND-joining every hop's predicates, e.g.:
    MATCH p=(a:bank)-[:performed]->(b:wire_message) WHERE a.NODE = 'b1' AND b.risk > 20 RETURN a, b, p
- To make the result visualizable, bind the traversal to a path variable with
  `MATCH p=(...)-...-(...)` and include `p` in the RETURN list, in addition to any
  scalar columns the question asks for.
- Node/edge identity properties are `NODE` and `ID` respectively; a node/edge's Cypher
  label/relationship type also exists as a `LABEL` property, but prefer matching via the
  label/type in the pattern (:bank, -[:performed]->) over filtering on the LABEL property.
- Reversed edges are kept verbatim: (e)<-[:manages]-(g) stays exactly like that (do not
  flip it into a forward pattern).
- GROUP BY is implicit: the non-aggregated columns in RETURN are the grouping keys; never
  write a GROUP BY keyword.
- Read-only ONLY: never CREATE/MERGE/DELETE/SET/REMOVE/DROP/etc.
- Use ONLY the node labels and relationship types listed in the schema below. Add a
  sensible LIMIT (<= 100) unless the question calls for an aggregate over everything.
"""

_DIALECT_KINETICA = """\
Target dialect: Kinetica GQL. Rules (MUST follow):
- Start the query with: GRAPH "{graph}" MATCH ... RETURN ...  (the graph name IS quoted).
- Predicates may be written inline in the node pattern, e.g. (a:bank WHERE a.NODE = '...'),
  or as a trailing WHERE after the MATCH — either is fine.
- Reversed edges are kept verbatim: (e)<-[:manages]-(g) stays exactly like that.
- GROUP BY is implicit: the non-aggregated RETURN columns are the grouping keys; never
  write a GROUP BY keyword. Use ROUND(SUM(...), 0) etc. for aggregates as needed.
- Wrap column-number ORDER BY as an alias instead (ORDER BY <alias> DESC), not ORDER BY 3.
- Read-only ONLY: never CREATE/MERGE/DELETE/SET/REMOVE/DROP/etc.
- Use ONLY the node labels and relationship types listed in the schema below. Add a
  sensible LIMIT (<= 100) unless the question calls for an aggregate over everything.
"""


def _schema_text(schema: dict) -> str:
    labels = schema.get("labels") or []
    rel_types = schema.get("rel_types") or []
    dot = schema.get("dot") or ""
    lines = [
        "NODE LABELS: " + ", ".join(labels),
        "RELATIONSHIP TYPES: " + ", ".join(rel_types),
    ]
    if dot:
        lines.append("SCHEMA GRAPH (dot, label -> label edges observed):\n" + dot)
    return "\n".join(lines)


def generate_cypher(schema: dict, engine: str, question: str, graph: str = "",
                     llm: Optional[LLMFunc] = None) -> str:
    """Ask the LLM for a single read-only query in `engine`'s dialect.

    `schema` is the dict returned by an adapter's `get_schema()`
    ({labels, rel_types, dot, counts}). `llm` overrides the default kgr
    `_llm` — pass a fake in tests to avoid shelling out to the `claude` CLI.
    """
    call = llm or _get_llm()
    dialect = _DIALECT_KINETICA.format(graph=graph or "<graph>") if engine == "kinetica" else _DIALECT_FALKORDB
    prompt = (
        "You translate a natural-language question into ONE read-only graph query.\n\n"
        + dialect + "\n"
        "SCHEMA:\n" + _schema_text(schema) + "\n\n"
        f"Question: {question}\n\n"
        "Return JSON with a single field `cypher` containing the query text only "
        "(no markdown fences, no trailing semicolon)."
    )
    out = call(prompt, schema=_CYPHER_SCHEMA)
    if isinstance(out, str):
        out = json.loads(out)
    cypher = (out.get("cypher") or "").strip()
    return cypher.rstrip(";").strip()


def validate_cypher(cypher: str, schema: Optional[dict] = None) -> tuple[bool, str]:
    """Reject write/DDL queries. Read-only only.

    Label-in-schema checking is intentionally best-effort/omitted here (the
    grounding schema is small and the LLM is instructed to stick to it) so we
    don't over-reject queries that are otherwise fine.
    """
    if not cypher or not cypher.strip():
        return False, "empty query"
    m = _WRITE_KW.search(cypher)
    if m:
        return False, f"query is not read-only (found {m.group(0).strip().upper()})"
    return True, ""


_SQL_WRITE_KW = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|ATTACH|DETACH|COPY|INSTALL|LOAD|PRAGMA|EXPORT|IMPORT|CALL)\b",
    re.IGNORECASE,
)

_JOIN_SQL_SCHEMA = {
    "type": "object",
    "properties": {
        "sql": {"type": "string", "description": "A single read-only DuckDB SELECT, or empty string if not needed."},
    },
    "required": ["sql"],
    "additionalProperties": False,
}


def generate_join_sql(focus: str, cypher: str, result_columns: list, wide_columns: list,
                       llm: Optional[LLMFunc] = None) -> str:
    """Ask the LLM for a read-only DuckDB SELECT that post-joins wide attributes.

    `cypher` is the graph query that produced the already-fetched result rows,
    exposed to DuckDB as relation `cypher` (alias `c`, columns = `result_columns`).
    `wide` is the hydrate file (alias `w`, columns = `wide_columns`), keyed by `NODE`.
    Returns "" if `focus` can be answered from the graph result alone (no wide
    column needed). `llm` overrides the default kgr `_llm` — pass a fake in
    tests to avoid shelling out to the `claude` CLI.
    """
    call = llm or _get_llm()
    prompt = (
        "You translate a natural-language focus question into ONE read-only DuckDB SQL "
        "SELECT that post-joins wide attribute columns onto an already-fetched graph "
        "query result.\n\n"
        "There are two relations available to your SQL:\n"
        "- `cypher` (alias c): the graph result rows already fetched. Columns hold NODE "
        "ids for node columns, and `*_LABEL`/label columns hold relationship-type "
        "strings.\n"
        "- `wide` (alias w): the attribute file, keyed by `NODE`.\n\n"
        "Use the cypher query text below to figure out which node type each result "
        "column holds (e.g. `RETURN c.NODE AS c_node` with `(c:party)` in the MATCH means "
        "`c_node` holds party ids), then join the matching `*_node` column from `cypher` "
        "to `wide.NODE` to pull an attribute for that node.\n\n"
        "'Number of paths' means COUNT(*) over the cypher rows, grouped by the attribute "
        "in question.\n\n"
        "Rules (MUST follow):\n"
        "- Return a single read-only SELECT statement only — no markdown fences, no "
        "trailing semicolon.\n"
        "- If the focus can be answered from the `cypher` result alone and no `wide` "
        "attribute is needed, return `sql` as an empty string.\n\n"
        f"Focus: {focus}\n\n"
        f"Cypher (query that produced `cypher`'s rows):\n{cypher}\n\n"
        f"result_columns (columns of `cypher`): {result_columns}\n"
        f"wide_columns (columns of `wide`): {wide_columns}\n\n"
        "Return JSON with a single field `sql` containing the SELECT text only (or empty "
        "string if no wide attribute is needed)."
    )
    out = call(prompt, schema=_JOIN_SQL_SCHEMA)
    if isinstance(out, str):
        out = json.loads(out)
    sql = (out.get("sql") or "").strip().rstrip(";").strip()
    return sql


def validate_sql(sql: str) -> tuple[bool, str]:
    """Reject empty, multi-statement, or write/DDL DuckDB SQL. Read-only only."""
    if not sql or not sql.strip():
        return False, "empty query"
    stripped = sql.strip()
    first_word = re.match(r"[A-Za-z]+", stripped)
    keyword = first_word.group(0).upper() if first_word else ""
    if keyword not in ("SELECT", "WITH"):
        return False, f"query must start with SELECT or WITH (found {keyword or stripped[:20]!r})"
    semi = stripped.find(";")
    if semi != -1 and stripped[semi + 1:].strip():
        return False, "multi-statement SQL is not allowed"
    m = _SQL_WRITE_KW.search(stripped)
    if m:
        return False, f"query is not read-only (found {m.group(0).strip().upper()})"
    return True, ""


def _compact_rows(columns: list, rows: list, limit: int = 30) -> list:
    sample = []
    for row in rows[:limit]:
        rec = {}
        for col, val in zip(columns, row):
            s = val
            if isinstance(s, str) and len(s) > 200:
                s = s[:200] + "…"
            rec[col] = s
        sample.append(rec)
    return sample


def synthesize(question: str, columns: list, rows: list, llm: Optional[LLMFunc] = None,
               cypher: Optional[str] = None) -> str:
    """Turn query results into a domain-relevant, plain-English explanation."""
    call = llm or _get_llm()
    sample = _compact_rows(columns, rows)
    cypher_block = f"Query (Cypher) that produced these results:\n{cypher}\n\n" if cypher else ""
    prompt = (
        "You are a data analyst explaining graph query results to a business user. Given "
        "the query and a sample of its result rows, explain in plain, domain-relevant "
        "English WHAT THE RESULTS MEAN — the real-world entities and relationships involved "
        "and the key finding. If the results are empty, say so plainly — do not invent "
        "facts.\n\n"
        "Interpret node labels and relationship types by their meaning; expand likely "
        "abbreviations (e.g. 'sar' = Suspicious Activity Report, 'tin' = Tax ID Number, "
        "'party' = an involved party/person, 'street_address' = a street address). Infer "
        "sensible meanings from label names even if they are not in this list.\n\n"
        "Report the substance: how many distinct meaningful things were found and what the "
        "relationship chain represents in the real world, and any notable pattern — all in "
        "domain terms.\n\n"
        "DO NOT describe mechanics: do NOT count raw rows, do NOT mention UUIDs, row "
        "multiplicity, branching/fan-out, duplicate or parallel edges, or 'N-hop path "
        "pattern'. Be concrete and pertinent. 2-4 sentences.\n\n"
        f"Question: {question}\n\n"
        f"{cypher_block}"
        f"Columns: {columns}\n"
        f"Results ({len(rows)} row(s), showing up to {len(sample)}):\n"
        f"{json.dumps(sample, default=str, indent=2)}\n\n"
        "Return JSON with a single field `answer` containing the plain-English explanation."
    )
    out = call(prompt, schema=_ANSWER_SCHEMA)
    if isinstance(out, str):
        try:
            out = json.loads(out)
        except json.JSONDecodeError:
            return out.strip()
    return str(out.get("answer", "")).strip()
