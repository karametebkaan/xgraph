from __future__ import annotations
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


def _ids_sql_list(ids) -> str:
    return ", ".join(_escape_sql_literal(i) for i in ids)


def _validate_source(source: str) -> str:
    # `source` may be schema-qualified (e.g. "expero.vertexes"); safe_ident
    # rejects dots, so validate each dot-separated part individually.
    for part in str(source).split("."):
        safe_ident(part)
    return source


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
