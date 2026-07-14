from __future__ import annotations

import re
from decimal import Decimal
from typing import Dict, Iterator

import duckdb

# Page size for streaming rows out of a DuckDB result. Unlike Kinetica there is
# no server-side row cap to work around here; we fetch in chunks (via
# `fetchmany`) purely to avoid materialising a whole wide table in Python.
DEFAULT_PAGE_SIZE = 10000

# Table names are interpolated into DDL (they can't be DuckDB parameters), so
# they must be simple identifiers, optionally schema-qualified.
_NAME_RE = re.compile(r"^[A-Za-z0-9_.]+$")

# Prefixes that need the httpfs extension (cold / object-store reads).
_REMOTE_PREFIXES = ("s3://", "http://", "https://", "gcs://", "az://", "azure://")


def coerce_value(value):
    """Make a DuckDB scalar safe for the FalkorDB (redis) client.

    DuckDB returns DECIMAL/NUMERIC columns as Python ``Decimal``, which the
    FalkorDB client cannot serialise as a Cypher parameter. Coerce those to
    ``float``; leave every other type untouched.
    """
    if isinstance(value, Decimal):
        return float(value)
    return value


def coerce_row(cols, row) -> dict:
    return {c: coerce_value(v) for c, v in zip(cols, row)}


class DuckDBSource:
    """Runs SQL against Parquet/CSV files via DuckDB and yields rows as plain
    dicts.

    A drop-in replacement for ``KineticaSource`` -- same ``.rows(sql)`` contract
    -- so the graph can be built with NO Kinetica. Each configured table name is
    registered as a DuckDB view over a file, which lets the existing mapping SQL
    (``... FROM expero.vertexes``) run completely unchanged.
    """

    def __init__(self, con, page_size: int = DEFAULT_PAGE_SIZE):
        self._con = con
        self._page_size = page_size

    @classmethod
    def connect(cls, tables: Dict[str, str],
                page_size: int = DEFAULT_PAGE_SIZE) -> "DuckDBSource":
        # `tables` maps a table name used in the mapping SQL (e.g.
        # "expero.vertexes") to a Parquet/CSV path or glob. The path may be a
        # local file, a glob ("data/*.parquet"), or an object-store URL
        # ("s3://bucket/vertexes.parquet") when httpfs is available.
        con = duckdb.connect()
        if any(str(p).startswith(_REMOTE_PREFIXES) for p in tables.values()):
            con.execute("INSTALL httpfs; LOAD httpfs;")
        for name, path in tables.items():
            if not _NAME_RE.fullmatch(name):
                raise ValueError(f"unsafe table name in duckdb config: {name!r}")
            if "'" in str(path):
                raise ValueError(f"unsafe file path in duckdb config: {path!r}")
            if "." in name:
                schema = name.split(".", 1)[0]
                con.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            # DuckDB infers the reader (parquet/csv/json) from the extension.
            con.execute(f"CREATE VIEW {name} AS SELECT * FROM '{path}'")
        return cls(con, page_size)

    def rows(self, sql: str) -> Iterator[dict]:
        cur = self._con.execute(sql)
        cols = [d[0] for d in cur.description]
        while True:
            chunk = cur.fetchmany(self._page_size)
            if not chunk:
                break
            for row in chunk:
                yield coerce_row(cols, row)
