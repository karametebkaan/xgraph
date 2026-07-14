from __future__ import annotations
import duckdb
from xgraph_gateway.config import resolve_data_path
from graph_loader.hydrate import hydrate as _falkor_hydrate
from graph_loader.hydrate import _register_rows, _REL_RE
from graph_loader.duckdb_source import coerce_row

class DuckDBComputeEngine:
    def hydrate(self, rows, source, key="NODE", columns="*"):
        return _falkor_hydrate(rows, resolve_data_path(source), key=key, columns=columns)

    def run_sql(self, sql):
        con = duckdb.connect()
        try:
            cur = con.execute(sql)
            cols = [d[0] for d in cur.description]
            return [coerce_row(cols, r) for r in cur.fetchall()]
        finally:
            con.close()

    def describe_source(self, source):
        source = resolve_data_path(source)
        if "'" in str(source):
            raise ValueError(f"unsafe source path: {source!r}")
        con = duckdb.connect()
        try:
            cur = con.execute(f"DESCRIBE SELECT * FROM '{source}'")
            return [row[0] for row in cur.fetchall()]
        finally:
            con.close()

    def run_join(self, rows, source, join_sql, cypher_relation="cypher", wide_relation="wide"):
        source = resolve_data_path(source)
        for rel in (cypher_relation, wide_relation):
            if not _REL_RE.fullmatch(rel):
                raise ValueError(f"unsafe relation name: {rel!r}")
        if "'" in str(source):
            raise ValueError(f"unsafe source path: {source!r}")
        if not rows:
            return []
        con = duckdb.connect()
        try:
            _register_rows(con, cypher_relation, rows)
            con.execute(
                f"CREATE OR REPLACE VIEW {wide_relation} AS SELECT * FROM '{source}'")
            cur = con.execute(join_sql)
            cols = [d[0] for d in cur.description]
            return [coerce_row(cols, r) for r in cur.fetchall()]
        finally:
            con.close()

# Back-compat alias: existing imports (`from ...compute.duckdb_engine import
# ComputeEngine`) keep working after the DuckDB-specific rename.
ComputeEngine = DuckDBComputeEngine
