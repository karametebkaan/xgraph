from __future__ import annotations
from abc import ABC, abstractmethod

class GraphEngineAdapter(ABC):
    @abstractmethod
    def list_graphs(self) -> list[str]: ...
    @abstractmethod
    def get_schema(self, graph: str, options: dict | None = None) -> dict: ...
    @abstractmethod
    def run_query(self, graph: str, cypher: str, timeout: int = 60000) -> dict: ...
    @abstractmethod
    def fetch_entities(self, graph: str, limit: int, offset: int = 0) -> dict: ...
    @abstractmethod
    def get_record(self, graph: str, node_id: str) -> dict: ...
    @abstractmethod
    def load_graph(self, spec: dict) -> dict: ...
    @abstractmethod
    def graph_sizes(self) -> dict: ...
    def ingest_elements(self, graph: str, nodes: list[dict], edges: list[dict]) -> dict:
        """MERGE extracted entities/relations into `graph` (accumulating,
        idempotent by id). `nodes`: [{id,label,name,attrs}]; `edges`:
        [{id,src,dst,label,attrs}]. Returns {"nodes": int, "edges": int,
        "nodes_created": int, "edges_created": int, "labels": {"node_labels":
        [...], "edge_labels": [...]}} -- "nodes"/"edges" is the total ensured
        present this call (so a repeat/overlapping Extract still reports the
        elements as present), "nodes_created"/"edges_created" is how many were
        newly created (vs. matched/updated) this call, and "labels" is the
        distinct labels seen in this call.

        Not `@abstractmethod`: FakeAdapter/FalkorDBAdapter/KineticaAdapter all
        implement it, but future adapters aren't forced to.
        """
        raise NotImplementedError

    def delete_graph(self, graph: str) -> dict:
        """Delete/drop the named graph. Returns {'deleted': <graph>}.

        Not `@abstractmethod`: FakeAdapter/FalkorDBAdapter/KineticaAdapter all
        implement it, but future adapters aren't forced to.
        """
        raise NotImplementedError

    def storage(self, graph: str) -> dict:
        """Best-effort inspection of the storage backing `graph` (Storage
        viewer action). Default -- used by any adapter that stores the graph
        itself rather than in separate inspectable tables (FalkorDB, and
        FakeAdapter by inheritance): there is nothing to preview beyond the
        graph, so point the caller at the existing Visualize/Ontology/Query
        actions.

        Concrete, not `@abstractmethod` -- mirrors `ingest_elements`/
        `delete_graph`: KineticaAdapter overrides this (its Extract backing
        tables ARE separately inspectable), but future adapters aren't forced
        to.
        """
        return {"kind": "graph-store",
                "note": "This engine stores the graph itself — inspect it via Visualize / Ontology / Query.",
                "tables": []}
