from __future__ import annotations

from typing import Optional

from falkordb import FalkorDB


class FalkorDBSink:
    """Writes Cypher to a FalkorDB graph."""

    def __init__(self, db, graph, name: str):
        self._db = db
        self._graph = graph
        self._name = name

    @classmethod
    def connect(cls, graph_name: str, host: str = "localhost", port: int = 6379,
                password: Optional[str] = None) -> "FalkorDBSink":
        db = FalkorDB(host=host, port=port, password=password)
        return cls(db, db.select_graph(graph_name), graph_name)

    def wipe(self) -> None:
        # Only delete when the graph actually exists. Checking existence
        # (rather than catching every exception from delete()) keeps a real
        # failure -- auth, connectivity, permissions -- from being silently
        # swallowed and letting a "full rebuild" MERGE onto stale data.
        if self._name in self._db.list_graphs():
            self._graph.delete()

    def run(self, query: str, params: dict = None):
        return self._graph.query(query, params or {})
