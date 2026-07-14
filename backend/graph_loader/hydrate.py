from __future__ import annotations

import re
from typing import List

import duckdb

from .duckdb_source import coerce_row
from .mapper import safe_ident

# Relation names are interpolated into SQL (they can't be parameters), so they
# must be plain identifiers.
_REL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def hydrate(result_rows: List[dict], source: str, key: str = "NODE",
            columns: str = "*", con=None) -> List[dict]:
    """Attach attribute-rich columns to Cypher result rows by joining on `key`.

    This is the post-traversal hydration step: Cypher runs against the skinny
    FalkorDB graph and returns a (small) set of ids; this reads only the wide
    rows for THOSE ids out of `source` and merges their columns back onto the
    result rows. No Kinetica is involved.

    `source` is a Parquet/CSV path -- local, a glob, or an object-store URL
    (``s3://...``) when httpfs is loaded. DuckDB applies projection pushdown
    (only the selected columns are read) and never loads the whole wide table
    into memory, so this stays cheap even when `source` is large.

    Note: because the ids coming out of a traversal are scattered rather than a
    contiguous range, row-group *pruning* helps little; the reliable wins are
    projection + out-of-core streaming. On a local file that is plenty fast; on
    remote storage, each touched row group is a round trip.

    If `columns` is narrowed from "*", it MUST still include `key` (that is how
    each hydrated row is matched back to its result row).
    """
    if not result_rows:
        return []
    key = safe_ident(key)
    # Discard rows with no identity -- a null/missing key cannot be joined.
    result_rows = [r for r in result_rows if r.get(key) is not None]
    if not result_rows:
        return []
    ids = [r[key] for r in result_rows]

    own = con is None
    if own:
        con = duckdb.connect()
    try:
        cur = con.execute(
            f"SELECT {columns} FROM '{source}' "
            f"WHERE {key} IN (SELECT unnest(?))",
            [ids],
        )
        cols = [d[0] for d in cur.description]
        attrs = [coerce_row(cols, row) for row in cur.fetchall()]
    finally:
        if own:
            con.close()

    by_key = {a[key]: a for a in attrs}
    return [{**r, **by_key.get(r[key], {})} for r in result_rows]


def _cypher_rows(qr) -> List[dict]:
    """Turn a FalkorDB QueryResult into a list of dicts keyed by RETURN alias.

    `qr.header` is a list of ``[type, name]`` pairs (name may be bytes).
    """
    names = []
    for col in qr.header:
        name = col[1] if isinstance(col, (list, tuple)) and len(col) > 1 else col
        if isinstance(name, bytes):
            name = name.decode()
        names.append(name)
    return [dict(zip(names, row)) for row in qr.result_set]


def _register_rows(con, rel: str, rows: List[dict]) -> None:
    # Load a small result set into a DuckDB temp table. Types are inferred from
    # the first row (VALUES), then the remaining rows are inserted.
    cols = list(rows[0].keys())
    placeholders = ", ".join(["?"] * len(cols))
    coldefs = ", ".join(f'"{c}"' for c in cols)
    con.execute(f"DROP TABLE IF EXISTS {rel}")
    con.execute(
        f"CREATE TEMP TABLE {rel} AS "
        f"SELECT * FROM (VALUES ({placeholders})) AS t({coldefs})",
        list(rows[0].values()),
    )
    if len(rows) > 1:
        con.executemany(
            f"INSERT INTO {rel} VALUES ({placeholders})",
            [list(r.values()) for r in rows[1:]],
        )


def run_hydrated(cypher: str, join_sql: str, *, falkordb, source: str,
                 con=None, cypher_relation: str = "cypher",
                 wide_relation: str = "wide", key: str = "NODE") -> List[dict]:
    """Run a Cypher query in FalkorDB, then a post-join SQL in DuckDB.

    The two user inputs:
      - `cypher`   : run against `falkordb` (a graph with `.query(cypher)`); its
                     result becomes the DuckDB relation named `cypher_relation`
                     (default ``cypher``), one column per RETURN alias.
      - `join_sql` : arbitrary DuckDB SQL that may reference that relation and
                     the wide attribute file, exposed as the view
                     `wide_relation` (default ``wide``) over `source`.

    `key` names the identity column of the Cypher result (default ``NODE``);
    rows whose `key` is null are discarded before the join so they neither
    corrupt the join nor mistype the temp table. If the result has no such
    column, no rows are dropped.

    No Kinetica is involved. Numeric outputs are coerced (Decimal -> float).
    Returns the post-join rows as a list of dicts; returns ``[]`` if the Cypher
    query yields no rows.

    Example::

        run_hydrated(
            "MATCH (a:bank)-[:performed]->(w:wire_message) "
            "WHERE w.wire_message_risk_score > 20 "
            "RETURN a.NODE AS NODE, w.wire_message_risk_score AS risk",
            "SELECT c.NODE, c.risk, w.party_name, w.full_address "
            "FROM cypher c JOIN wide w USING (NODE) ORDER BY c.risk DESC",
            falkordb=graph, source="data/vertexes.parquet",
        )
    """
    for rel in (cypher_relation, wide_relation):
        if not _REL_RE.fullmatch(rel):
            raise ValueError(f"unsafe relation name: {rel!r}")
    if "'" in str(source):
        raise ValueError(f"unsafe source path: {source!r}")

    rows = _cypher_rows(falkordb.query(cypher))
    # Discard rows with no identity (only when the key column is present, so an
    # aggregate/other-aliased result isn't wiped out).
    if rows and key in rows[0]:
        rows = [r for r in rows if r.get(key) is not None]
    if not rows:
        return []

    own = con is None
    if own:
        con = duckdb.connect()
    try:
        _register_rows(con, cypher_relation, rows)
        con.execute(
            f"CREATE OR REPLACE VIEW {wide_relation} AS SELECT * FROM '{source}'")
        cur = con.execute(join_sql)
        cols = [d[0] for d in cur.description]
        return [coerce_row(cols, row) for row in cur.fetchall()]
    finally:
        if own:
            con.close()
