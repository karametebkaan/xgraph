from __future__ import annotations
from .base import GraphEngineAdapter

_NODES = [{"id": "b1", "label": "bank", "props": {"bank_name": "Acme"}},
          {"id": "w1", "label": "wire_message", "props": {"risk": 90}}]
_EDGES = [{"id": "e1", "source": "b1", "target": "w1", "type": "performed"}]

class FakeAdapter(GraphEngineAdapter):
    def list_graphs(self):
        return ["demo_graph"]
    def get_schema(self, graph, options=None):
        return {"labels": ["bank", "wire_message"], "rel_types": ["performed"],
                "dot": 'digraph { "bank" -> "wire_message" [label="performed"]; }',
                "counts": {"nodes": len(_NODES), "edges": len(_EDGES)}}
    def run_query(self, graph, cypher, timeout=60000):
        return {"columns": ["NODE"], "rows": [[n["id"]] for n in _NODES],
                "graph": {"nodes": list(_NODES), "edges": list(_EDGES)}}
    def fetch_entities(self, graph, limit, offset=0):
        return {"nodes": _NODES[offset:offset + limit], "edges": _EDGES[offset:offset + limit]}
    def get_record(self, graph, node_id):
        for n in _NODES:
            if n["id"] == node_id:
                return n
        return {}
    def load_graph(self, spec):
        return {"nodes": {"bank": 2}, "edges": {"performed": 1}}
    def graph_sizes(self):
        return {"demo_graph": {"nodes": len(_NODES), "edges": len(_EDGES)}}
    def ingest_elements(self, graph, nodes, edges):
        node_labels = sorted({n["label"] for n in nodes if n.get("label")})
        edge_labels = sorted({e["label"] for e in edges if e.get("label")})
        return {"nodes": len(nodes), "edges": len(edges),
                "labels": {"node_labels": node_labels, "edge_labels": edge_labels}}
