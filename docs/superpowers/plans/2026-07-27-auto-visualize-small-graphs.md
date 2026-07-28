# Auto pull+visualize for small graphs — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a graph is selected and its edge count is under an editable threshold (default 10,000), automatically pull its entities so the visualization is ready — no manual "Pull+Visualize" click, and no tab switch.

**Architecture:** Frontend-only change to the single-file React app `frontend/XGraph.html`. A new `useEffect` in `App` watches the already-loaded `graphCounts` and calls the existing `handleVisualizeLoad()` once per small graph. A checkbox + editable number input in the `CanvasGraph` viz toolbar control the behavior; their state lives in `App` and is threaded to `CanvasGraph` as props (same pattern as `bigGraphThreshold`).

**Tech Stack:** React 18 UMD + Babel-standalone (no build step). `frontend/node_modules/.bin/esbuild` is available for the transpile check.

## Global Constraints

- **No `git commit` anywhere under `xgraph/`** (repo rule, `CLAUDE.md`). Write files; never stage or commit. This plan therefore has **no commit steps** — each task ends with a validation checkpoint instead.
- **No backend or `gateway.js` changes.** Edge counts already arrive via `/schema` → `graphCounts`; the pull is the existing `handleVisualizeLoad`.
- Frontend edits to the ~10,000-line `XGraph.html` are anchored search-and-replace against **verbatim** code strings (line numbers shift as edits land — match on the string, not the number).
- The React app cannot be runtime-verified headlessly. The only automated gate is the Babel/JSX transpile check below; real behavior is browser-verified by the user.
- Follow existing code style in the file: `var` locals inside components, inline style objects, `function(){}` (not arrow) callbacks in JSX handlers to match surrounding code.

### Transpile check (used as the "run the test" step in every task)

Run from `frontend/`:

```bash
awk '/type="text\/babel"/{f=1;next} f&&/^<\/script>/{f=0} f' XGraph.html \
  | ./node_modules/.bin/esbuild --loader=jsx --jsx=transform --log-level=warning >/dev/null \
  && echo "TRANSPILE OK"
```

Expected on success: prints `TRANSPILE OK` and exits 0. On a syntax error esbuild prints the error with a line/column and exits 1.

---

## File Structure

- Modify: `frontend/XGraph.html` — the only file touched. Four regions:
  - module consts (~line 64) — add `AUTO_VIZ_EDGE_THRESHOLD`.
  - `App` state block (~line 8476) — add two `useState` + one `useRef`.
  - after `handleVisualizeLoad` (~line 9582) — add the trigger `useEffect`.
  - `CanvasGraph` prop aliases (~line 5948) + toolbar JSX (~line 6468) — add the control.
  - two `<CanvasGraph .../>` call sites (~lines 9948 and 10004) — thread four props each.

---

## Task 1: Auto-viz core logic (const + state + trigger effect)

Adds the constant, App state, once-per-graph guard ref, and the effect that performs the automatic pull. After this task the behavior works with the default-on setting; the UI control (Task 2) only exposes it.

**Files:**
- Modify: `frontend/XGraph.html` (module consts ~64; App state ~8476; effect ~9582)

**Interfaces:**
- Consumes (existing, already in scope in `App`): `activeGraph` (string), `graphCounts` (`{nodes:number, edges:number}`), `handleVisualizeLoad` (`useCallback`, called with no args → normal capped pull).
- Produces (used by Task 2): App state `autoVizEnabled` (bool) / `setAutoVizEnabled`, `autoVizThreshold` (number) / `setAutoVizThreshold`; module const `AUTO_VIZ_EDGE_THRESHOLD` (number).

- [ ] **Step 1: Add the module constant**

Find this verbatim block (~line 64):

```js
const VISUALIZE_PAGE_SIZE = 10000;
```

Insert immediately AFTER it:

```js
// Graphs whose edge count is below this auto-pull on select (background, no tab
// switch) so Visualize is instant. Editable at runtime via App's autoVizThreshold
// state + the CanvasGraph "Auto-viz" toolbar control; see App's auto-viz effect.
const AUTO_VIZ_EDGE_THRESHOLD = 10000;
```

- [ ] **Step 2: Add App state + guard ref**

Find this verbatim line (~8476):

```js
    const [bigGraphThreshold, setBigGraphThreshold] = useState(100000);
```

Insert immediately AFTER it:

```js
    // Auto pull+visualize small graphs on select (default on; threshold editable).
    const [autoVizEnabled, setAutoVizEnabled] = useState(true);
    const [autoVizThreshold, setAutoVizThreshold] = useState(AUTO_VIZ_EDGE_THRESHOLD);
    const _autoVizGraphRef = useRef('');   // graph most recently auto-pulled (once-per-graph guard)
```

- [ ] **Step 3: Add the trigger effect after `handleVisualizeLoad`**

Find this verbatim line — the closing of the `handleVisualizeLoad` `useCallback` (~9582):

```js
    }, [activeGraph, gwClient, graphCounts, bigGraphThreshold, graphEngine, credentials]);
```

Insert immediately AFTER it:

```js

    // Auto pull+visualize small graphs: once a selected graph's counts load and
    // show it's under the (editable) edge threshold, pull its entities in the
    // background so Visualize is instant — no manual Pull+Visualize click, and NO
    // tab switch. Guarded once-per-graph via _autoVizGraphRef (mirrors the
    // _labelJsonGraphRef one-shot pattern below). The edges>0 test skips both the
    // transient {0,0} that loadSchema sets on every graph switch and genuinely
    // empty graphs.
    useEffect(function() {
        if (!autoVizEnabled || !activeGraph) return;
        var edges = graphCounts.edges || 0;
        if (edges <= 0 || edges >= autoVizThreshold) return;
        if (_autoVizGraphRef.current === activeGraph) return;   // already auto-pulled this graph
        _autoVizGraphRef.current = activeGraph;
        handleVisualizeLoad();   // no arg -> normal capped pull; loads the whole (small) graph
    }, [activeGraph, graphCounts, autoVizEnabled, autoVizThreshold, handleVisualizeLoad]);
```

- [ ] **Step 4: Run the transpile check**

Run the Transpile check command (Global Constraints). Expected: `TRANSPILE OK`.

- [ ] **Step 5: Validation checkpoint (no commit — repo rule)**

Confirm: the three inserts are present, `AUTO_VIZ_EDGE_THRESHOLD` is referenced by both the const definition and the new `useState`, and the effect sits after the `handleVisualizeLoad` callback (so `handleVisualizeLoad` is in scope). Do NOT `git commit`.

---

## Task 2: Toolbar control (checkbox + editable threshold) and prop wiring

Exposes the setting in the `CanvasGraph` viz toolbar next to the existing Viz-limit selector, and threads the App state into both `CanvasGraph` render sites.

**Files:**
- Modify: `frontend/XGraph.html` (CanvasGraph prop aliases ~5948; toolbar JSX ~6468; two call sites ~9948 and ~10004)

**Interfaces:**
- Consumes (from Task 1): App state `autoVizEnabled` / `setAutoVizEnabled`, `autoVizThreshold` / `setAutoVizThreshold`, const `AUTO_VIZ_EDGE_THRESHOLD`.
- Consumes (existing in `CanvasGraph`): `ctrlStyle` (style object used by the neighboring N:/E: sliders, in scope in `CanvasGraph`).
- Produces: new `CanvasGraph` props `autoVizEnabled` (bool), `onToggleAutoViz` (`(bool)=>void`), `autoVizThreshold` (number), `onChangeAutoVizThreshold` (`(number)=>void`).

- [ ] **Step 1: Add prop aliases in `CanvasGraph`**

Find this verbatim line (~5948):

```js
    var bigGraphThreshold = props.bigGraphThreshold || 10000000, onChangeBigGraphThreshold = props.onChangeBigGraphThreshold;
```

Insert immediately AFTER it:

```js
    var autoVizEnabled = props.autoVizEnabled !== false, onToggleAutoViz = props.onToggleAutoViz;
    var autoVizThreshold = props.autoVizThreshold || AUTO_VIZ_EDGE_THRESHOLD, onChangeAutoVizThreshold = props.onChangeAutoVizThreshold;
```

(`!== false` makes the control default to checked when the prop is absent, e.g. the maximized instance during any transitional render.)

- [ ] **Step 2: Add the toolbar control before the Pull+Visualize button**

Find this verbatim block — the end of the Viz-limit `<select>` and the start of the Pull+Visualize button (~6468-6469):

```jsx
                </select>}
                {onVisualize && <button onClick={onVisualize} disabled={vizLoading} title="Re-fetch graph data and refresh visualization" style={{
```

Replace it with (inserts the control between the `</select>}` and the button):

```jsx
                </select>}
                {onToggleAutoViz && <label title="Automatically pull small graphs the moment they're selected" style={Object.assign({}, ctrlStyle, { display:'flex', alignItems:'center', gap:3, flexShrink:0 })}>
                    <input type="checkbox" checked={autoVizEnabled} onChange={function(e){ onToggleAutoViz(e.target.checked); }} style={{ margin:0 }}/>
                    {'Auto-viz \u003C'}
                    <input type="number" min="0" step="1000" value={autoVizThreshold} onChange={function(e){ onChangeAutoVizThreshold(parseInt(e.target.value) || 0); }} title="Edge count below which a graph auto-visualizes on select" style={{ width:56, fontSize:9, border:'1px solid #ddd', borderRadius:4, padding:'2px 3px', fontFamily:'inherit', color:'#636e72' }}/>
                    {' edges'}
                </label>}
                {onVisualize && <button onClick={onVisualize} disabled={vizLoading} title="Re-fetch graph data and refresh visualization" style={{
```

(`\u003C` is a literal `<` — written escaped so it is unambiguous inside JSX text.)

- [ ] **Step 3: Thread props into the primary `CanvasGraph` (split view)**

Find this verbatim line (~9948):

```jsx
                                            bigGraphThreshold={bigGraphThreshold} onChangeBigGraphThreshold={setBigGraphThreshold}
```

Replace it with:

```jsx
                                            bigGraphThreshold={bigGraphThreshold} onChangeBigGraphThreshold={setBigGraphThreshold}
                                            autoVizEnabled={autoVizEnabled} onToggleAutoViz={setAutoVizEnabled}
                                            autoVizThreshold={autoVizThreshold} onChangeAutoVizThreshold={setAutoVizThreshold}
```

- [ ] **Step 4: Thread props into the maximized `CanvasGraph`**

Find this verbatim line (~10004) — note the indentation differs from Step 3, so match this one exactly:

```jsx
                        bigGraphThreshold={bigGraphThreshold} onChangeBigGraphThreshold={setBigGraphThreshold}
```

Replace it with:

```jsx
                        bigGraphThreshold={bigGraphThreshold} onChangeBigGraphThreshold={setBigGraphThreshold}
                        autoVizEnabled={autoVizEnabled} onToggleAutoViz={setAutoVizEnabled}
                        autoVizThreshold={autoVizThreshold} onChangeAutoVizThreshold={setAutoVizThreshold}
```

> Note: Steps 3 and 4 replace the **same string** at two indentation levels. Because Edit requires a unique match, apply them one at a time (each becomes unique once you include its surrounding indentation), or use the distinct leading-whitespace shown above to disambiguate.

- [ ] **Step 5: Run the transpile check**

Run the Transpile check command (Global Constraints). Expected: `TRANSPILE OK`.

- [ ] **Step 6: Serve check (if the gateway is running)**

If the gateway is up, confirm the page still serves:

```bash
curl -sf -o /dev/null -w "%{http_code}\n" http://localhost:8090/
```

Expected: `200`. (If the gateway isn't running, start it with `./xgraph start` from the repo root, or skip — this is a smoke check, not a behavior test.)

- [ ] **Step 7: Validation checkpoint (no commit — repo rule)**

Confirm both call sites received the four props and the toolbar control renders in the transpiled output. Do NOT `git commit`.

---

## Manual browser acceptance (user-driven, after both tasks)

Open `http://localhost:8090/` and reload. With the gateway connected to an engine:

1. **Small graph auto-loads:** Select a graph with `< 10,000` edges (List → click it). Without touching Pull+Visualize, open the Visualize tab → the node-link graph is already rendered.
2. **No tab switch:** Selecting the small graph does NOT navigate you to Visualize; you remain on List/Ontology. The pull happens in the background.
3. **Large graph does nothing:** Select a graph with `>= 10,000` edges → Visualize shows the empty state with "Click Pull+Visualize" (manual only, unchanged).
4. **Toggle off:** In the Visualize toolbar, uncheck "Auto-viz" → select another small graph → it does NOT auto-load (manual only).
5. **Editable threshold:** Set the number lower than a given small graph's edge count → that graph no longer auto-loads; raise it above → it does.
6. **Once per graph:** Re-selecting the same small graph doesn't re-trigger a redundant pull (the guard ref); switching to a different small graph does.

---

## Self-Review

- **Spec coverage:** Trigger-on-select-no-tab-switch → Task 1 Step 3 effect (`handleVisualizeLoad()` with no tab change). Toggle default-on → Task 1 Step 2 (`useState(true)`) + Task 2 control. Editable edge threshold → Task 1 `autoVizThreshold` state + Task 2 number input. Once-per-graph guard → `_autoVizGraphRef`. All-engines → uses `handleVisualizeLoad` which branches internally (no engine check added). Non-persisted → no session-store wiring added. Visualize-toolbar home → Task 2 Step 2 placement. All spec sections mapped.
- **Placeholder scan:** none — every step has verbatim find/replace code and a concrete run command.
- **Type consistency:** `autoVizEnabled` bool throughout; `autoVizThreshold` number (via `parseInt(...) || 0`); `onToggleAutoViz` = `setAutoVizEnabled` (bool); `onChangeAutoVizThreshold` = `setAutoVizThreshold` (number). Const name `AUTO_VIZ_EDGE_THRESHOLD` identical in definition (Task 1 Step 1), state init (Task 1 Step 2), and CanvasGraph fallback (Task 2 Step 1). Prop names identical between CanvasGraph aliases (Task 2 Step 1), toolbar usage (Step 2), and both call sites (Steps 3-4).
