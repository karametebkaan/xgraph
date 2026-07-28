# Auto pull+visualize for small graphs — Design

**Date:** 2026-07-27
**Status:** Approved (design); ready for implementation plan
**Scope:** Frontend only (`frontend/XGraph.html`). No backend or `gateway.js` changes.

## Goal

When a graph is selected and its edge count is known to be small
(`0 < edges < threshold`, default 10,000), automatically pull its entities so the
visualization is ready — without requiring a manual click on **Pull+Visualize**.

## Why this is cheap

The edge count for the active graph already arrives automatically: selecting a graph
runs `loadSchema(activeGraph)` which sets `graphCounts = {nodes, edges}` from
`/schema`'s `counts`. The data pull itself is the existing `handleVisualizeLoad()`
handler (behind every "Pull+Visualize" button), which already handles both the
Kinetica direct-fetch branch and the FalkorDB/other paging branch. So the feature is
purely: *decide to call the existing handler automatically for small graphs.*

## Behavior

- **Trigger:** on graph select (i.e. once `graphCounts` for the newly-active graph has
  loaded), if the toggle is on and `0 < graphCounts.edges < autoVizThreshold`, call
  `handleVisualizeLoad()` once.
- **No tab switch:** the pull happens in the background regardless of which tab is
  active (List / Ontology / etc.). The user is *not* navigated to the Visualize tab.
  The effect is that opening Visualize later is instant because `graphTableData` is
  already populated.
- **Once per graph:** guarded so it fires a single time per active graph; switching to
  a different graph re-arms it.
- **Applies to all engines:** Kinetica and FalkorDB both route through
  `handleVisualizeLoad()`, which already branches internally.

### Edge cases

- The transient `graphCounts = {nodes:0, edges:0}` reset that occurs on every graph
  switch (before real counts arrive) is skipped by the `edges > 0` guard — no spurious
  fire.
- A genuinely empty graph (0 edges) does not auto-visualize (nothing to show).
- A graph with `edges >= threshold` does nothing automatically — the user still clicks
  Pull+Visualize as today.
- If the toggle is off, behavior is exactly as today (manual only).

## State (added in the `App` component)

- `autoVizEnabled` — `useState(true)`. Master on/off.
- `autoVizThreshold` — `useState(AUTO_VIZ_EDGE_THRESHOLD)` where
  `AUTO_VIZ_EDGE_THRESHOLD = 10000`, a named module const declared near
  `VISUALIZE_PAGE_SIZE` (line ~64).
- `_autoVizGraphRef` — `useRef('')`. Holds the graph name most recently auto-visualized,
  giving the once-per-graph guard (mirrors the existing `_labelJsonGraphRef` /
  `_schemaGraphRef` one-shot pattern).

Not persisted across sessions — consistent with the current session-restore being
legacy; keeps scope minimal.

## Trigger effect

A new `useEffect` in `App`, deps `[activeGraph, graphCounts, autoVizEnabled,
autoVizThreshold, handleVisualizeLoad]`:

```js
useEffect(function () {
    if (!autoVizEnabled) return;
    if (!activeGraph) return;
    var edges = graphCounts.edges || 0;
    if (edges <= 0 || edges >= autoVizThreshold) return;
    if (_autoVizGraphRef.current === activeGraph) return;  // already did this graph
    _autoVizGraphRef.current = activeGraph;
    handleVisualizeLoad();   // no arg -> normal capped pull (whole graph, since small)
}, [activeGraph, graphCounts, autoVizEnabled, autoVizThreshold, handleVisualizeLoad]);
```

Notes:
- `handleVisualizeLoad` is a `useCallback` that closes over `graphCounts`; it must be in
  the deps so the effect always calls the current instance.
- Placed *after* `handleVisualizeLoad` is defined (line ~9582) so it is in scope.
- Calling with no argument means `loadAll` is `undefined`, so the handler uses its
  normal capped `target = min(graphCounts.nodes, bigGraphThreshold)`. For a sub-10K-edge
  graph this loads the entire graph in one pass.

## UI control (Visualize toolbar)

Alongside the existing **Viz limit** selector in the `CanvasGraph` toolbar
(lines ~6465-6467), add:

- A checkbox labeled **"Auto-visualize small graphs"** bound to `autoVizEnabled`.
- An inline number input for `autoVizThreshold` (edges), so the 10K threshold is
  editable. Suffix/label makes clear it counts edges (e.g. `< [10000] edges`).

Because these controls live inside `CanvasGraph` but the state lives in `App`, four
props are threaded through to `CanvasGraph` (following the existing pattern used for
`bigGraphThreshold` / viz-limit): `autoVizEnabled`, `setAutoVizEnabled`,
`autoVizThreshold`, `setAutoVizThreshold`. (Implementation may instead site the control
in the App-level Visualize toolbar if that avoids prop threading — decided during
planning by inspecting the exact toolbar markup; the toggle+threshold pairing and
default-on behavior are the fixed requirements.)

## Testing

- Frontend has no headless runtime test for the React app; validate via:
  - Babel transpile check of the `<script type="text/babel">` block (existing method).
  - `curl` 200 on the served page.
  - Browser acceptance driven by the user: select a small (<10K-edge) graph → confirm
    Visualize tab shows data without a manual Pull click; select a large graph → confirm
    nothing auto-loads; toggle off → confirm manual-only behavior; edit the threshold →
    confirm the boundary moves.
- `gateway.js` unchanged, so its Node tests are unaffected.

## Out of scope

- Persisting the toggle/threshold across sessions.
- Auto-switching to the Visualize tab.
- Any change to the manual Pull+Visualize path, the paging cap (`bigGraphThreshold`),
  or backend endpoints.
