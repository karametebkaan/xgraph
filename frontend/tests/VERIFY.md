# xGraph — Browser Acceptance (action-bar shell)

Frontend can't be verified headlessly; backend, `gateway.js` (transforms/client), and full-app Babel
transpile all pass automatically. This is the human browser check of the action-bar workflow.

## Start

```bash
# 1. Gateway (from xgraph/backend, falkor's venv) — port 8090 (8088 is taken by Kinetica Graph)
cd xgraph/backend && /home/kkaramete/github-graph/graph/falkor/.venv/bin/uvicorn xgraph_gateway.app:app --port 8090
# 2. Frontend (separate shell)
cd xgraph/frontend && python3 -m http.server 8099   # open http://localhost:8099/XGraph.html
```

Prereqs: live FalkorDB (`banking_graph`, :6379); Kinetica reachable; wide Parquet at
`/home/kkaramete/github-graph/graph/falkor/data/vertexes.parquet`.

## The shell

A horizontal **action bar** under the xGraph wordmark:
`Setup · Connect · List · Load · Interact · Query · Interact · Visualize · Ontology`.
Each action owns the panel beneath it. Actions dim/disable until reachable (List needs a connection;
Load needs a selected graph; Interact/Query/Visualize/Ontology need a completed Load).

## Steps (record ✅/❌ + screenshot each)

1. **Setup.** Three axis selectors: **Graph engine** (Kinetica | FalkorDB), **OLAP/ingest**
   (Kinetica | DuckDB), **LLM** (Claude). Pick e.g. FalkorDB + DuckDB + Claude. FalkorDB shows
   host/port/password; DuckDB shows "(embedded)"; Claude shows an optional API-key field. Gateway URL
   defaults to `http://localhost:8090`.
2. **Connect.** "Connect & List" → "Connected — N graphs"; auto-advances to List.
3. **List.** `banking_graph` (and `demo`) appear; click `banking_graph`.
4. **Load.** "Load graph" → node/edge counts shown, and the **live ontology** (Graphviz DOT) renders
   in the panel. (Re-loading / switching graphs re-derives it.)
5. **Query.** Run a Cypher, e.g.
   `MATCH (b:bank)-[:performed]->(w:wire_message) WHERE w.wire_message_risk_score>90 RETURN b.NODE AS NODE, b.bank_name AS bank_name, w.wire_message_risk_score AS risk ORDER BY risk DESC LIMIT 25`
   → results table. Click a result node → node-detail → **Hydrate attributes** → "Hydrated from
   DuckDB" section shows `bank:bank_number` (never in the graph).
   *(Known rough edge: the query panel is currently a floating/draggable window over the Query area —
   functional, but flag if you want it inline.)*
6. **Visualize.** Force-graph node-link render of the loaded graph.
7. **Ontology.** Full-size ontology diagram + a label-distribution chart (real per-label node counts).
8. **Interact ① / ②.** Both show a "wired in the next slice" stub — the LLM NL→Cypher→English
   round-trip is the next chunk (B/S3), not built yet.
9. **Engine combos.** Re-Setup with **FalkorDB + Kinetica** (hybrid) and **Kinetica + Kinetica**
   (native) and confirm Connect/List work; Kinetica-only UI (grammar/geo/WMS) appears only when the
   **graph** engine is Kinetica.
10. **explorer untouched:** `git -C /home/kkaramete/github-graph/graph status --short explorer/` → empty.

## Automated (green — rerun anytime)
```bash
cd xgraph/backend && /home/kkaramete/github-graph/graph/falkor/.venv/bin/python -m pytest tests/ -q   # 43 passed
cd xgraph/frontend && node tests/test_transforms.mjs && node tests/test_client.mjs                    # OK / OK
```
