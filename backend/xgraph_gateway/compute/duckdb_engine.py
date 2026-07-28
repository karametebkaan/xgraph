from __future__ import annotations
import duckdb
import json
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
                " source VARCHAR, ts TIMESTAMP, spec_json VARCHAR,"
                " PRIMARY KEY (graph, engine))")
            con.execute(
                "CREATE TABLE IF NOT EXISTS xgraph_document_texts ("
                " graph VARCHAR, doc_uri VARCHAR, text VARCHAR,"
                " char_len INTEGER, PRIMARY KEY (graph, doc_uri))")
            # Migration for DBs created before spec_json existed. Idempotent:
            # ADD COLUMN raises if it already exists, so swallow that one case.
            try:
                con.execute("ALTER TABLE xgraph_creations ADD COLUMN spec_json VARCHAR")
            except Exception:
                pass
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

    def record_document_text(self, graph, doc_uri, text):
        """Upsert the FULL source text for a document, keyed on
        (graph, doc_uri). Idempotent (DELETE then INSERT). Best-effort
        provenance -- callers wrap this so a failure never breaks /extract."""
        text = text or ""
        con = self._meta_con()
        try:
            con.execute(
                "DELETE FROM xgraph_document_texts WHERE graph = ? AND doc_uri = ?",
                [graph, doc_uri])
            con.execute(
                "INSERT INTO xgraph_document_texts VALUES (?, ?, ?, ?)",
                [graph, doc_uri, text, len(text)])
        finally:
            con.close()

    def has_document_text(self, graph, doc_uri):
        """Cheap existence check for the /extract reuse-path backfill."""
        con = self._meta_con()
        try:
            row = con.execute(
                "SELECT 1 FROM xgraph_document_texts"
                " WHERE graph = ? AND doc_uri = ?", [graph, doc_uri]).fetchone()
            return row is not None
        finally:
            con.close()

    def get_document_text(self, graph, doc_uri, limit=None):
        """Return {doc_uri, text, char_len, truncated} with text sliced to
        `limit` (full text when limit is None or >= length); None if no row."""
        con = self._meta_con()
        try:
            row = con.execute(
                "SELECT text, char_len FROM xgraph_document_texts"
                " WHERE graph = ? AND doc_uri = ?", [graph, doc_uri]).fetchone()
            if row is None:
                return None
            text, char_len = row[0] or "", row[1] or 0
            sliced = text if limit is None else text[:limit]
            return {"doc_uri": doc_uri, "text": sliced, "char_len": char_len,
                    "truncated": char_len > len(sliced)}
        finally:
            con.close()

    def record_creation(self, graph, engine, statement, source, spec=None):
        """UPSERT the 'how this graph was created' recipe, keyed on
        (graph, engine). Latest write wins. `spec` (the structured /create
        spec) is stored as JSON in spec_json; None -> NULL (legacy shape)."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        spec_json = json.dumps(spec) if spec is not None else None
        con = self._meta_con()
        try:
            existing = con.execute(
                "SELECT 1 FROM xgraph_creations WHERE graph = ? AND engine = ?",
                [graph, engine]).fetchone()
            if existing is None:
                con.execute(
                    "INSERT INTO xgraph_creations VALUES (?, ?, ?, ?, ?, ?)",
                    [graph, engine, statement, source, now, spec_json])
            else:
                con.execute(
                    "UPDATE xgraph_creations SET statement = ?, source = ?,"
                    " ts = ?, spec_json = ? WHERE graph = ? AND engine = ?",
                    [statement, source, now, spec_json, graph, engine])
            return {"graph": graph, "engine": engine, "source": source, "ts": _iso(now)}
        finally:
            con.close()

    def get_creation(self, graph):
        """Most-recent recorded creation recipe for `graph` (any engine),
        including the parsed structured spec (None when not recorded)."""
        con = self._meta_con()
        try:
            row = con.execute(
                "SELECT graph, engine, statement, source, ts, spec_json"
                " FROM xgraph_creations WHERE graph = ?"
                " ORDER BY ts DESC LIMIT 1", [graph]).fetchone()
            if not row:
                return None
            spec = None
            if row[5]:
                try:
                    spec = json.loads(row[5])
                except Exception:
                    spec = None
            return {"graph": row[0], "engine": row[1], "statement": row[2],
                    "source": row[3], "ts": _iso(row[4]), "spec": spec}
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
            con.execute("DELETE FROM xgraph_document_texts WHERE graph = ?", [graph])
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

    def read_columns(self, source, key="NODE", columns=None):
        """Read (key + columns) for EVERY row of a wide source, so those
        columns can be PROMOTED onto graph nodes. Unlike `hydrate`, there is
        no `WHERE key IN (...)` id filter -- the whole column is read
        (projection pushdown keeps unused columns off the read). Returns a
        list of dicts (key + each requested column); Decimals are coerced to
        float via coerce_row. `columns` is a list of source column names; the
        key is always included and de-duplicated. Column identifiers may
        contain non-identifier characters (e.g. 'party:party_name'), so each
        is double-quoted; the resolved path is single-quoted with an
        injection guard (mirroring describe_source)."""
        cols = [c for c in (columns or []) if c and c != key]
        if not cols:
            raise ValueError("columns must be a non-empty list")
        path = resolve_data_path(source)
        if "'" in str(path):
            raise ValueError(f"unsafe source path: {path!r}")
        select = ", ".join('"' + c.replace('"', '""') + '"'
                           for c in [key] + cols)
        con = duckdb.connect()
        try:
            cur = con.execute(f"SELECT {select} FROM '{path}'")
            out_cols = [d[0] for d in cur.description]
            return [coerce_row(out_cols, r) for r in cur.fetchall()]
        finally:
            con.close()

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
