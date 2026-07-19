from __future__ import annotations
from . import registry

class SessionStore:
    """In-memory session store mapping a session id to a cached
    (graph adapter, compute engine) pair.

    Session ids are opaque strings derived from a monotonic per-store
    counter ("s1", "s2", ...) — deliberately NOT random/uuid so behavior
    is deterministic in tests.
    """

    def __init__(self, adapter_factory=registry.get_adapter,
                 compute_factory=registry.get_compute):
        self._adapter_factory = adapter_factory
        self._compute_factory = compute_factory
        self._sessions: dict[str, dict] = {}
        self._counter = 0

    def create(self, graph_engine, graph_conn, compute_engine, compute_conn,
               extract_mode=None) -> str:
        self._counter += 1
        session_id = f"s{self._counter}"
        self._sessions[session_id] = {
            "adapter": self._adapter_factory(graph_engine, graph_conn),
            "compute": self._compute_factory(compute_engine, compute_conn),
            "graph_engine": graph_engine,
            "compute_engine": compute_engine,
            "extract_mode": extract_mode or "sequential",
            "files": [],
        }
        return session_id

    def get(self, session_id: str) -> dict:
        if session_id not in self._sessions:
            raise KeyError(f"unknown session: {session_id}")
        return self._sessions[session_id]

    def register_file(self, session_id: str, path: str) -> list[str]:
        """Remember a data-file path against a session so it shows up as a
        pickable builder source. Raw path stored verbatim (resolved at
        describe/build time). Deduped, insertion-ordered."""
        s = self.get(session_id)  # raises KeyError for unknown session
        files = s.setdefault("files", [])
        if path and path not in files:
            files.append(path)
        return files
