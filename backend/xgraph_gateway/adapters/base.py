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

    def fetch_node_attrs(self, graph: str, ids) -> list[dict]:
        """Wide attribute rows `[{NODE, ...}]` for the given NODE ids, for
        Explain's post-join when attributes live ON the graph nodes. Default:
        none, so engines backed by an external wide source use that instead."""
        return []

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

    def list_tables(self) -> list[dict]:
        """List tables/relations usable as builder section sources.

        Each item is {"name": str, "type": str}. Default: no introspection
        (empty list) so the builder degrades to manual table entry.
        """
        return []

    def list_columns(self, table: str) -> list[str]:
        """Column names for a table/relation (for builder autocomplete).

        Default: no introspection (empty list). Never raises for an unknown
        table -- returns [] so manual column entry still works.
        """
        return []

    def creation_statement(self, graph) -> dict:
        """Best-effort "how was this graph created" recipe (Create panel's
        recipe viewer). Returns {"statement": <DDL text or None>, "source":
        <where it came from, or None>}.

        Concrete, not `@abstractmethod` -- mirrors `storage`/`delete_graph`:
        KineticaAdapter overrides this (show_graph carries the authoritative
        CREATE GRAPH DDL), but FalkorDB has no server-side creation DDL (a
        FalkorDB graph is built incrementally by whatever queries touched it,
        with no stored recipe) -- so it, and any future adapter that doesn't
        override this, inherits this default.
        """
        return {"statement": None, "source": None}
