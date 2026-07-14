from __future__ import annotations
from .base import SourceReader
from graph_loader.duckdb_source import DuckDBSource

class DuckDBSourceReader(SourceReader):
    def read(self, spec):
        src = DuckDBSource.connect(spec["tables"])
        return list(src.rows(spec["sql"]))
