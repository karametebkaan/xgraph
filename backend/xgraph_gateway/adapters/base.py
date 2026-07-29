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
    def fetch_entities(self, graph: str, limit: int, after: str | None = None) -> dict: ...

    def fetch_subgraph(self, graph: str, limit: int) -> dict:
        """Whole capped subgraph in ONE call, concise/columnar -- the fast
        Visualize path (A+B). Returns:

            {"ids": [str], "labels": [str|list], "src": [int], "dst": [int],
             "etype": [str], "total_nodes": int, "total_edges": int,
             "capped": bool}

        `src`/`dst` are 0-based INDICES into `ids` (the Kinetica concise-edge
        analog -- repeating node-id strings collapse to small ints). An edge is
        kept ONLY when BOTH endpoints are in the pulled node set (induced
        subgraph), so every index is valid; a full pull (`limit >= total_nodes`)
        drops nothing. `total_*` are the TRUE graph counts (may exceed the pulled
        N/E when capped).

        Default: page this adapter's own `fetch_entities` until `limit` nodes or
        exhaustion. Not `@abstractmethod` (mirrors `ingest_elements`/`storage`):
        adapters with a cheaper bulk path (FalkorDB) override it."""
        PAGE = 10000
        ids: list = []
        labels: list = []
        index: dict = {}
        edge_rows: list = []  # (source_id, target_id, type)
        after = None
        while len(ids) < limit:
            page = self.fetch_entities(graph, min(PAGE, limit - len(ids)), after)
            page_nodes = page.get("nodes") or []
            if not page_nodes:
                break
            for nd in page_nodes:
                nid = nd.get("id")
                if nid in index:
                    continue
                index[nid] = len(ids)
                ids.append(nid)
                labels.append(nd.get("label"))
            for ed in (page.get("edges") or []):
                edge_rows.append((ed.get("source"), ed.get("target"), ed.get("type")))
            after = page.get("next_cursor")
            if not after:
                break
        src: list = []
        dst: list = []
        etype: list = []
        for s, d, t in edge_rows:
            si = index.get(s)
            di = index.get(d)
            if si is None or di is None:
                continue  # induced subgraph: edge to a node beyond the cap
            src.append(si)
            dst.append(di)
            etype.append(t)
        total_nodes, total_edges = self._subgraph_totals(graph, len(ids), len(src))
        return {"ids": ids, "labels": labels, "src": src, "dst": dst,
                "etype": etype, "total_nodes": total_nodes,
                "total_edges": total_edges, "capped": len(ids) < total_nodes}

    def _subgraph_totals(self, graph: str, pulled_nodes: int, pulled_edges: int):
        """True (graph-wide) node/edge counts for `fetch_subgraph`. Default: no
        cheap count source, so report the pulled counts (so `capped` is never a
        false positive). Adapters with a count query override this."""
        return pulled_nodes, pulled_edges

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

    def promote_columns(self, graph, source, key="NODE", columns=None):
        """Promote whole wide-source columns onto existing nodes as properties
        (making them mid-traversal filterable). FalkorDB-only; the default is
        unsupported.

        Raises a ValueError whose message avoids "timeout"/"unreachable"/
        "connection" so the gateway maps it to 400 (bad request), not 502/504.
        """
        raise ValueError(
            f"promotion not supported for {getattr(self, 'engine', 'this engine')}")

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

    def graph_grammar(self) -> dict:
        """Per-component graph-creation grammar for the structured builder,
        shaped as {COMPONENT: {"configurations": [{"label","required"[,"filtervalues"]}],
        "optional": [ids]}} for COMPONENT in NODES/EDGES/WEIGHTS/RESTRICTIONS.

        Default: {} -- the frontend falls back to its built-in static grammar.
        Kinetica overrides this from show_graph_grammar.
        """
        return {}

    def register_file(self, path, table=None, fmt=None, data_source=None) -> dict:
        """Import a file as a table/relation for the builder. Engine-specific;
        only Kinetica implements server-side ingestion here (DuckDB/FalkorDB are
        handled by the /register_file session path-registry, not the adapter)."""
        raise NotImplementedError("register_file not supported for this engine")

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
