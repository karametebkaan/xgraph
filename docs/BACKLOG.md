# xGraph — Backlog

Deferred work, roughly in priority order. Living document.

## 1. Geo / WKT → deck.gl map render
The `mm` geo graph (Kinetica mode) doesn't render into the map view. Pull+Visualize returns
nodes/edges but no geometry, so nothing reaches the deck.gl/MapLibre `DeckMapView` the explorer
frontend carried over.
- Backend: a geo-aware fetch that returns **WKT** geometry for spatial nodes (the current
  `fetch_entities` drops it).
- Frontend: detect geo results (WKT present) and route them to `DeckMapView` instead of the
  force-graph canvas.

## 2. Create "grammar" helper
A form-based builder for Kinetica graph DDL (`CREATE` / `SOLVE` / `MATCH`), like the one in the
original explorer.
- Needs a gateway `/grammar` proxy endpoint (surfacing the engine's grammar/templates).
- Frontend: a guided form in the Create action that emits valid DDL.

## 3. PuppyGraph-over-Iceberg as a 3rd graph engine
Evaluate PuppyGraph (graph queries over Iceberg/lakehouse tables) as an alternate graph route
alongside FalkorDB and Kinetica. Larger effort — its own spec/plan session.

## 4. Git LFS migration for the demo data
`data/*.parquet.zip` (~110 MB) is committed as plain files, which weighs down clones. Migrate to
Git LFS (`.gitattributes` + `git lfs migrate import --include="data/*.zip"`), then force-push the
rewritten history. Low urgency.

## Done (for reference)
- Vendor-neutral gateway + carried-over frontend; 3 engine axes (graph × OLAP × LLM).
- Action bar: Setup · Connect · Create · List · Load · Ask · Query · Explain · Visualize · Ontology.
- Ask (NL→query→run→answer) and Explain (results→domain English).
- **Explain post-join hydration + OLAP** (focus-driven NL→SQL over the wide Parquet).
- Standalone repo: vendored `graph_loader`, local `llm.py`, own venv, zipped in-repo data.
