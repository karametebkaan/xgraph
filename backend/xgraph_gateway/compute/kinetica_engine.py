from __future__ import annotations
from datetime import datetime, timezone
from graph_loader.kinetica_source import KineticaSource
from graph_loader.duckdb_source import coerce_row
from graph_loader.mapper import safe_ident


def _escape_sql_literal(value) -> str:
    """Render a scalar id as a single-quoted SQL string literal.

    Embedded single quotes are doubled (the standard SQL escape) so an id
    containing one can never break out of the literal and get interpreted as
    SQL syntax. Ids are never string-interpolated raw.
    """
    return "'" + str(value).replace("'", "''") + "'"


def _lit(value) -> str:
    """Render any scalar value as a SQL literal, `None` -> unquoted `NULL`.

    Kept separate from `_escape_sql_literal` (used elsewhere in this file for
    ids/columns that are always non-None, e.g. hydrate's `_ids_sql_list` and
    the NOT NULL metadata columns) so this null-safety only applies where a
    caller can legitimately pass `None` -- the nullable ontology columns
    (`canonical_name`, `axis`, `first_seen_uri`). `NULL` must never be
    wrapped in quotes, or Kinetica would store the 4-char string "NULL"
    instead of a real SQL NULL.
    """
    return "NULL" if value is None else _escape_sql_literal(value)


def _ids_sql_list(ids) -> str:
    return ", ".join(_escape_sql_literal(i) for i in ids)


def _validate_source(source: str) -> str:
    # `source` may be schema-qualified (e.g. "expero.vertexes"); safe_ident
    # rejects dots, so validate each dot-separated part individually.
    for part in str(source).split("."):
        safe_ident(part)
    return source


# ---------------------------------------------------------------------------
# Metadata store -- Kinetica-backed mirror of compute/duckdb_engine.py's
# xgraph_documents/xgraph_ontology tables (documents ledger + ontology
# registry), so the same contract (record_document/list_documents/
# record_type/resolve_canonical/get_canonicals/axis_map) works whichever
# ComputeEngine the gateway is wired to. Tables live in an `xgraph_meta`
# Kinetica schema, keyed by `graph` (unlike the falkor/kgr original, which
# had one fixed schema for one graph) so multiple graphs share one store.
# `attr_name`/`attr_sql_type` from kgr's schema.sql are omitted -- xgraph
# does not induce attribute columns from the ontology (see kinetica_adapter's
# own evolve-columns path, which discovers attrs from ingest payloads, not
# from this registry).
# ---------------------------------------------------------------------------

_META_SCHEMA = "xgraph_meta"
_DOCUMENTS_TABLE = f"{_META_SCHEMA}.documents"
_ONTOLOGY_TABLE = f"{_META_SCHEMA}.ontology"


def _now_ms() -> datetime:
    """Naive-UTC "now", truncated to millisecond precision.

    Kinetica TIMESTAMP columns round-trip through KineticaSource.rows() as
    raw epoch-millisecond integers (see `_ts_from_kinetica`) -- truncating
    the Python-side value up front keeps a just-computed timestamp equal to
    what a later read-back of that same row converts back to (otherwise the
    microsecond digits below the millisecond would never match after a
    round trip through the DB).
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)


def _ts_literal(dt: datetime) -> str:
    """naive-UTC datetime -> Kinetica TIMESTAMP SQL literal (millisecond
    precision -- Kinetica's TIMESTAMP literal/JSON-insert grain; see
    kinetica_adapter.py's `_now_ts_str`, the same convention)."""
    return "'" + dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "'"


def _ts_from_kinetica(v):
    """Kinetica TIMESTAMP columns round-trip through KineticaSource.rows()
    (execute_sql_and_decode + dict(rec), no type coercion) as raw
    epoch-millisecond integers -- convert to a naive-UTC ISO string, matching
    DuckDB engine's `_iso()`/naive-UTC convention so callers see the same
    shape regardless of compute engine."""
    if isinstance(v, (int, float)):
        return (datetime.fromtimestamp(v / 1000, tz=timezone.utc)
                .replace(tzinfo=None).isoformat())
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


class KineticaComputeEngine:
    """ComputeEngine backed by Kinetica: `hydrate` performs the wide-column
    join server-side (a `SELECT ... WHERE key IN (...)` against a Kinetica
    table) instead of DuckDB reading a Parquet file.

    `conn` is a dict of `{url, user, password}` used to build a `KineticaSource`
    unless `_source_factory` is supplied (test seam -- lets unit tests inject a
    fake source with canned rows, so this class needs no live Kinetica to test).
    """

    def __init__(self, conn=None, _source_factory=None):
        # Connection is lazy: constructing this engine (e.g. via
        # registry.get_compute) must not touch the network, so the real
        # GPUdb/KineticaSource is only built on first use. `_source_factory`
        # is a test seam that skips the network entirely.
        self._conn = conn
        self._source_factory = _source_factory
        self._src_cache = None
        self._meta_ready = False

    @property
    def _src(self):
        if self._src_cache is None:
            if self._source_factory is not None:
                self._src_cache = self._source_factory()
            else:
                import gpudb
                conn = self._conn or {}
                db = gpudb.GPUdb(host=conn["url"], username=conn.get("user"),
                                 password=conn.get("password"))
                self._src_cache = KineticaSource(db)
        return self._src_cache

    def hydrate(self, rows, source, key="NODE", columns="*"):
        if not rows:
            return []
        key = safe_ident(key)
        _validate_source(source)
        rows = [r for r in rows if r.get(key) is not None]
        if not rows:
            return []
        ids = [r[key] for r in rows]
        sql = (f"SELECT {columns} FROM {source} "
              f"WHERE {key} IN ({_ids_sql_list(ids)})")
        attrs = [coerce_row(list(rec.keys()), list(rec.values()))
                for rec in self._src.rows(sql)]
        by_key = {a[key]: a for a in attrs}
        return [{**r, **by_key.get(r[key], {})} for r in rows]

    def run_sql(self, sql):
        return [coerce_row(list(rec.keys()), list(rec.values()))
               for rec in self._src.rows(sql)]

    # -- Metadata store: documents ledger + ontology registry -------------
    # Mirrors DuckDBComputeEngine's contract exactly (same method
    # signatures/return shapes) -- see compute/duckdb_engine.py. Executed via
    # the same `self._src.rows(sql)` path `hydrate`/`run_sql` already use;
    # Kinetica SQL has no parameterized-query API through KineticaSource, so
    # every value is escaped via `_escape_sql_literal`, never string-
    # interpolated raw. `self._src.rows(...)` is consumed with `list(...)`
    # even for DDL/DML (CREATE/INSERT/UPDATE) -- confirmed live that
    # execute_sql_and_decode (which `.rows()` wraps) executes those
    # statements fine and simply yields no records back.

    def _ensure_meta_schema(self):
        if self._meta_ready:
            return
        for stmt in (
            f"CREATE SCHEMA IF NOT EXISTS {_META_SCHEMA}",
            f"CREATE TABLE IF NOT EXISTS {_DOCUMENTS_TABLE} ("
            " graph VARCHAR(256, PRIMARY_KEY, SHARD_KEY) NOT NULL,"
            " doc_uri VARCHAR(512, PRIMARY_KEY) NOT NULL,"
            " sha256 VARCHAR(64) NOT NULL,"
            " source_type VARCHAR(32) NOT NULL,"
            " first_ingested_ts TIMESTAMP NOT NULL,"
            " last_ingested_ts TIMESTAMP NOT NULL,"
            " status VARCHAR(16) NOT NULL)",
            f"CREATE TABLE IF NOT EXISTS {_ONTOLOGY_TABLE} ("
            " graph VARCHAR(256, PRIMARY_KEY, SHARD_KEY) NOT NULL,"
            " type_kind VARCHAR(16, PRIMARY_KEY) NOT NULL,"
            " type_name VARCHAR(128, PRIMARY_KEY) NOT NULL,"
            " canonical_name VARCHAR(128),"
            " axis VARCHAR(64),"
            " first_seen_uri VARCHAR(512),"
            " first_seen_ts TIMESTAMP NOT NULL)",
        ):
            list(self._src.rows(stmt))
        self._meta_ready = True

    def record_document(self, graph, doc_uri, sha256, source_type):
        self._ensure_meta_schema()
        now = _now_ms()
        now_lit = _ts_literal(now)
        g, u = _escape_sql_literal(graph), _escape_sql_literal(doc_uri)
        existing = list(self._src.rows(
            f"SELECT sha256, first_ingested_ts FROM {_DOCUMENTS_TABLE}"
            f" WHERE graph = {g} AND doc_uri = {u}"))
        if not existing:
            list(self._src.rows(
                f"INSERT INTO {_DOCUMENTS_TABLE}"
                " (graph, doc_uri, sha256, source_type, first_ingested_ts,"
                "  last_ingested_ts, status)"
                f" VALUES ({g}, {u}, {_escape_sql_literal(sha256)},"
                f" {_escape_sql_literal(source_type)}, {now_lit}, {now_lit}, 'ingested')"))
            status, first_ts = "new", now.isoformat()
        else:
            row = existing[0]
            first_ts = _ts_from_kinetica(row["first_ingested_ts"])
            if row["sha256"] == sha256:
                list(self._src.rows(
                    f"UPDATE {_DOCUMENTS_TABLE} SET last_ingested_ts = {now_lit}"
                    f" WHERE graph = {g} AND doc_uri = {u}"))
                status = "unchanged"
            else:
                list(self._src.rows(
                    f"UPDATE {_DOCUMENTS_TABLE} SET sha256 = {_escape_sql_literal(sha256)},"
                    f" last_ingested_ts = {now_lit}, status = 'ingested'"
                    f" WHERE graph = {g} AND doc_uri = {u}"))
                status = "updated"
        return {"status": status, "first_ingested_ts": first_ts,
                "last_ingested_ts": now.isoformat()}

    def list_documents(self, graph):
        self._ensure_meta_schema()
        cols = ["graph", "doc_uri", "sha256", "source_type",
                "first_ingested_ts", "last_ingested_ts", "status"]
        rows = list(self._src.rows(
            f"SELECT {', '.join(cols)} FROM {_DOCUMENTS_TABLE}"
            f" WHERE graph = {_escape_sql_literal(graph)}"))
        out = []
        for r in rows:
            d = {c: r.get(c) for c in cols}
            d["first_ingested_ts"] = _ts_from_kinetica(d["first_ingested_ts"])
            d["last_ingested_ts"] = _ts_from_kinetica(d["last_ingested_ts"])
            out.append(d)
        return out

    def record_type(self, graph, kind, type_name, canonical_name, axis, source_uri):
        self._ensure_meta_schema()
        now_lit = _ts_literal(_now_ms())
        # First-seen wins: ON CONFLICT DO NOTHING preserves the original row
        # (Kinetica requires an explicit column list for ON CONFLICT -- see
        # live probe in the task-9k report).
        list(self._src.rows(
            f"INSERT INTO {_ONTOLOGY_TABLE}"
            " (graph, type_kind, type_name, canonical_name, axis, first_seen_uri, first_seen_ts)"
            f" VALUES ({_escape_sql_literal(graph)}, {_escape_sql_literal(kind)},"
            f" {_escape_sql_literal(type_name)}, {_lit(canonical_name)},"
            f" {_lit(axis)}, {_lit(source_uri)}, {now_lit})"
            " ON CONFLICT (graph, type_kind, type_name) DO NOTHING"))

    def resolve_canonical(self, graph, kind, type_name):
        self._ensure_meta_schema()
        g, k = _escape_sql_literal(graph), _escape_sql_literal(kind)
        tn = _escape_sql_literal(type_name)
        # Deterministic tie-break: an exact type_name match always wins over
        # a case-insensitive-only match (same ORDER BY trick as DuckDB's
        # resolve_canonical -- confirmed live that Kinetica SQL also orders
        # TRUE after FALSE, so DESC puts the exact match first).
        rows = list(self._src.rows(
            f"SELECT canonical_name FROM {_ONTOLOGY_TABLE}"
            f" WHERE graph = {g} AND type_kind = {k}"
            f" AND (type_name = {tn} OR lower(type_name) = lower({tn}))"
            f" ORDER BY (type_name = {tn}) DESC LIMIT 1"))
        return rows[0]["canonical_name"] if rows else None

    def get_canonicals(self, graph, kind):
        self._ensure_meta_schema()
        g, k = _escape_sql_literal(graph), _escape_sql_literal(kind)
        rows = list(self._src.rows(
            f"SELECT DISTINCT canonical_name FROM {_ONTOLOGY_TABLE}"
            f" WHERE graph = {g} AND type_kind = {k}"))
        return [r["canonical_name"] for r in rows]

    def axis_map(self, graph, kind):
        self._ensure_meta_schema()
        g, k = _escape_sql_literal(graph), _escape_sql_literal(kind)
        rows = list(self._src.rows(
            f"SELECT type_name, axis FROM {_ONTOLOGY_TABLE}"
            f" WHERE graph = {g} AND type_kind = {k}"))
        return {r["type_name"]: r["axis"] for r in rows}
