from __future__ import annotations
import duckdb
from datetime import datetime, timezone
from xgraph_gateway import config
from xgraph_gateway.config import resolve_data_path
from graph_loader.hydrate import hydrate as _falkor_hydrate
from graph_loader.hydrate import _register_rows, _REL_RE
from graph_loader.duckdb_source import coerce_row, coerce_value


def _iso(ts):
    """DuckDB TIMESTAMP (datetime) or datetime -> ISO string (stable, comparable)."""
    return ts.isoformat() if hasattr(ts, "isoformat") else str(ts)


class DuckDBComputeEngine:
    def __init__(self, meta_path: str | None = None):
        self._meta_path = meta_path or config.resolve_meta_path()
        self._meta_ready = False

    def _meta_con(self):
        con = duckdb.connect(self._meta_path)
        if not self._meta_ready:
            con.execute(
                "CREATE TABLE IF NOT EXISTS xgraph_documents ("
                " graph VARCHAR, doc_uri VARCHAR, sha256 VARCHAR,"
                " source_type VARCHAR, first_ingested_ts TIMESTAMP,"
                " last_ingested_ts TIMESTAMP, status VARCHAR,"
                " PRIMARY KEY (graph, doc_uri))")
            con.execute(
                "CREATE TABLE IF NOT EXISTS xgraph_ontology ("
                " graph VARCHAR, type_kind VARCHAR, type_name VARCHAR,"
                " canonical_name VARCHAR, axis VARCHAR,"
                " first_seen_uri VARCHAR, first_seen_ts TIMESTAMP,"
                " PRIMARY KEY (graph, type_kind, type_name))")
            con.execute(
                "CREATE TABLE IF NOT EXISTS xgraph_creations ("
                " graph VARCHAR, engine VARCHAR, statement VARCHAR,"
                " source VARCHAR, ts TIMESTAMP,"
                " PRIMARY KEY (graph, engine))")
            self._meta_ready = True
        return con

    def record_document(self, graph, doc_uri, sha256, source_type):
        # Naive UTC: DuckDB TIMESTAMP is tz-naive and converts+drops tzinfo on
        # readback, so a tz-aware value wouldn't round-trip equal. Store naive
        # UTC so first_ingested_ts read back == the value we returned on insert.
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        con = self._meta_con()
        try:
            existing = con.execute(
                "SELECT sha256, first_ingested_ts FROM xgraph_documents"
                " WHERE graph = ? AND doc_uri = ?", [graph, doc_uri]).fetchone()
            if existing is None:
                con.execute(
                    "INSERT INTO xgraph_documents VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [graph, doc_uri, sha256, source_type, now, now, "ingested"])
                status, first_ts = "new", now
            elif existing[0] == sha256:
                con.execute(
                    "UPDATE xgraph_documents SET last_ingested_ts = ?"
                    " WHERE graph = ? AND doc_uri = ?", [now, graph, doc_uri])
                status, first_ts = "unchanged", existing[1]
            else:
                con.execute(
                    "UPDATE xgraph_documents SET sha256 = ?, last_ingested_ts = ?,"
                    " status = ? WHERE graph = ? AND doc_uri = ?",
                    [sha256, now, "ingested", graph, doc_uri])
                status, first_ts = "updated", existing[1]
            return {"status": status,
                    "first_ingested_ts": _iso(first_ts),
                    "last_ingested_ts": _iso(now)}
        finally:
            con.close()

    def list_documents(self, graph):
        con = self._meta_con()
        try:
            cols = ["graph", "doc_uri", "sha256", "source_type",
                    "first_ingested_ts", "last_ingested_ts", "status"]
            rows = con.execute(
                f"SELECT {', '.join(cols)} FROM xgraph_documents WHERE graph = ?",
                [graph]).fetchall()
            return [dict(zip(cols, [_iso(v) if hasattr(v, 'isoformat') else v
                                    for v in r])) for r in rows]
        finally:
            con.close()

    def get_document(self, graph, doc_uri):
        con = self._meta_con()
        try:
            cols = ["graph", "doc_uri", "sha256", "source_type",
                    "first_ingested_ts", "last_ingested_ts", "status"]
            row = con.execute(
                f"SELECT {', '.join(cols)} FROM xgraph_documents"
                " WHERE graph = ? AND doc_uri = ?", [graph, doc_uri]).fetchone()
            if row is None:
                return None
            return dict(zip(cols, [_iso(v) if hasattr(v, 'isoformat') else v
                                    for v in row]))
        finally:
            con.close()

    def record_creation(self, graph, engine, statement, source):
        """UPSERT the 'how this graph was created' recipe, keyed on
        (graph, engine). Latest write wins."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        con = self._meta_con()
        try:
            existing = con.execute(
                "SELECT 1 FROM xgraph_creations WHERE graph = ? AND engine = ?",
                [graph, engine]).fetchone()
            if existing is None:
                con.execute("INSERT INTO xgraph_creations VALUES (?, ?, ?, ?, ?)",
                            [graph, engine, statement, source, now])
            else:
                con.execute(
                    "UPDATE xgraph_creations SET statement = ?, source = ?, ts = ?"
                    " WHERE graph = ? AND engine = ?",
                    [statement, source, now, graph, engine])
            return {"graph": graph, "engine": engine, "source": source, "ts": _iso(now)}
        finally:
            con.close()

    def get_creation(self, graph):
        """Most-recent recorded creation recipe for `graph` (any engine)."""
        con = self._meta_con()
        try:
            row = con.execute(
                "SELECT graph, engine, statement, source, ts FROM xgraph_creations"
                " WHERE graph = ? ORDER BY ts DESC LIMIT 1", [graph]).fetchone()
            if not row:
                return None
            return {"graph": row[0], "engine": row[1], "statement": row[2],
                    "source": row[3], "ts": _iso(row[4])}
        finally:
            con.close()

    def clear_graph_metadata(self, graph):
        """Delete all ledger + ontology rows for `graph` (idempotent -- a
        no-op, not an error, if the tables don't exist yet or the graph has
        no rows). Called from /delete_graph so a deleted-then-re-extracted
        document isn't silently short-circuited as "unchanged"."""
        con = self._meta_con()
        try:
            con.execute("DELETE FROM xgraph_documents WHERE graph = ?", [graph])
            con.execute("DELETE FROM xgraph_ontology WHERE graph = ?", [graph])
            con.execute("DELETE FROM xgraph_creations WHERE graph = ?", [graph])
        finally:
            con.close()

    def record_type(self, graph, kind, type_name, canonical_name, axis, source_uri):
        # Naive UTC, matching record_document's convention: DuckDB TIMESTAMP is
        # tz-naive, so a tz-aware value would get silently shifted to local
        # time on readback.
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        con = self._meta_con()
        try:
            # First-seen wins: ON CONFLICT DO NOTHING preserves the original row.
            con.execute(
                "INSERT INTO xgraph_ontology VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (graph, type_kind, type_name) DO NOTHING",
                [graph, kind, type_name, canonical_name, axis, source_uri, now])
        finally:
            con.close()

    def resolve_canonical(self, graph, kind, type_name):
        con = self._meta_con()
        try:
            # Deterministic tie-break: an exact type_name match always wins over
            # a case-insensitive-only match (DuckDB orders booleans TRUE last,
            # so DESC puts the exact match -- TRUE -- first).
            row = con.execute(
                "SELECT canonical_name FROM xgraph_ontology"
                " WHERE graph = ? AND type_kind = ?"
                " AND (type_name = ? OR lower(type_name) = lower(?))"
                " ORDER BY (type_name = ?) DESC LIMIT 1",
                [graph, kind, type_name, type_name, type_name]).fetchone()
            return row[0] if row else None
        finally:
            con.close()

    def get_canonicals(self, graph, kind):
        con = self._meta_con()
        try:
            rows = con.execute(
                "SELECT DISTINCT canonical_name FROM xgraph_ontology"
                " WHERE graph = ? AND type_kind = ?", [graph, kind]).fetchall()
            return [r[0] for r in rows]
        finally:
            con.close()

    def axis_map(self, graph, kind):
        con = self._meta_con()
        try:
            rows = con.execute(
                "SELECT type_name, axis FROM xgraph_ontology"
                " WHERE graph = ? AND type_kind = ?", [graph, kind]).fetchall()
            return {name: axis for name, axis in rows}
        finally:
            con.close()

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

    def describe_relation(self, source):
        """Columns of a relation identified by a file path/source name.

        Thin wrapper over describe_source so callers (adapters) have a single
        'columns of this source' entry point. Returns [] on error."""
        try:
            return self.describe_source(source)
        except Exception:
            return []

    def preview_source(self, source, limit=25):
        """Storage viewer's "DuckDB source preview": columns + up to `limit`
        sample rows of the resolved source file. Same path-resolution +
        single-quote guard as `describe_source` -- `source` is untrusted
        (frontend-supplied), so it goes through `resolve_data_path` and is
        never string-interpolated raw."""
        source = resolve_data_path(source)
        if "'" in str(source):
            raise ValueError(f"unsafe source path: {source!r}")
        con = duckdb.connect()
        try:
            cur = con.execute(f"SELECT * FROM '{source}' LIMIT {int(limit)}")
            cols = [d[0] for d in cur.description]
            rows = [[coerce_value(v) for v in r] for r in cur.fetchall()]
            return {"columns": cols, "rows": rows}
        finally:
            con.close()

    def run_join_rows(self, cypher_rows, wide_rows, join_sql,
                      cypher_relation="cypher", wide_relation="wide"):
        """Post-join two IN-MEMORY row sets (no Parquet). Used when the wide
        attributes come from the graph's own nodes (`fetch_node_attrs`) rather
        than an external file -- the `wide` relation is registered from
        `wide_rows` instead of a file."""
        for rel in (cypher_relation, wide_relation):
            if not _REL_RE.fullmatch(rel):
                raise ValueError(f"unsafe relation name: {rel!r}")
        if not cypher_rows or not wide_rows:
            return []
        con = duckdb.connect()
        try:
            _register_rows(con, cypher_relation, cypher_rows)
            _register_rows(con, wide_relation, wide_rows)
            cur = con.execute(join_sql)
            cols = [d[0] for d in cur.description]
            return [coerce_row(cols, r) for r in cur.fetchall()]
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
