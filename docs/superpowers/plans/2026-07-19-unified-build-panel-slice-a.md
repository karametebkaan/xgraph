# Unified "Build" Panel — Slice A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fold graph-creation (`Create`) and extraction (`Extract`) into one `Build` action — a `BuildPanel` roof with a Source radio (Documents ↔ Tables/files) and a Mode radio (Append ↔ Recreate) — without changing how graphs are physically built.

**Architecture:** Frontend-only. A new `BuildPanel` React component (in the single-file `frontend/XGraph.html` Babel block) renders the *existing, unchanged* `ExtractPanel` or `CreatePanel` beneath two radio rows. The `ExtractPanel` gains a `buildMode` prop so Recreate does `delete_graph` → `extract`. The action bar's `create` + `extract` entries collapse into one `build` entry; the render dispatch collapses two blocks into one `<BuildPanel>`. No backend, no `gateway.js`, no test-file changes (reuses `gwClient.extract`, `gwClient.deleteGraph`, `gwClient.create`).

**Tech Stack:** React 18 UMD + Babel-standalone (no build step), single-file `frontend/XGraph.html`. Syntax validation via the project's local `esbuild` JSX check. Behavior acceptance is browser-driven (the React app can't be runtime-verified headlessly — see CLAUDE.md).

## Global Constraints

- **No `git commit` unless the user authorizes it** (CLAUDE.md). This plan's default is local edits only.
- **Do NOT touch the Query-tab Create-Helper** (`q.kind === 'create'` / `createHelper`, around `XGraph.html:8974–9131`) — that is the buried structured builder, reserved for Slice B.
- **Do NOT change build mechanics** — no edits to `/create`, `/extract`, or `gateway.js`. This is UX unification only.
- **Every frontend edit is validated by the esbuild JSX check** (command below) before commit. The check must print `ESBUILD_OK`.
- **Recreate is destructive** — it must confirm with the user and must tolerate a missing graph (idempotent delete).
- **Version badge**: bump `EXPLORER_VERSION` (`XGraph.html:50`) from `0.3.6` to `0.4.0` as part of the integration task so a stale cache is visible.
- Commit messages: concise 1–2 lines, no `Co-Authored-By` footer.

### Frontend esbuild JSX check (used as the "run the test" step throughout)

```bash
cd /home/kkaramete/xgraph/frontend
end=$(grep -n '</script>' XGraph.html | tail -1 | cut -d: -f1)
sed -n "47,$((end-1))p" XGraph.html | ./node_modules/.bin/esbuild --loader=jsx > /dev/null && echo ESBUILD_OK || echo ESBUILD_FAIL
```

Expected on success: `ESBUILD_OK`. A syntax error prints the offending line and `ESBUILD_FAIL`.

---

## File Structure

- **Modify only:** `/home/kkaramete/xgraph/frontend/XGraph.html`
  - `ExtractPanel` (~`6972–7175`): add `buildMode` handling in `handleExtract` + button label (Task 1).
  - New `BuildPanel` function inserted immediately after `ExtractPanel`'s closing brace, before the `StoragePanel` banner comment at `7176` (Task 2).
  - `ACTIONS` array (`6536–6548`): replace `create` + `extract` entries with one `build` entry (Task 3).
  - Render dispatch (`9371–9403`): replace the two `activeAction === 'create'` / `'extract'` blocks with one `activeAction === 'build'` block rendering `<BuildPanel>` (Task 3).
  - `EXPLORER_VERSION` (`50`): `0.3.6` → `0.4.0` (Task 3).

No new files. No deletions.

---

## Task 1: ExtractPanel — Recreate support via `buildMode`

**Files:**
- Modify: `/home/kkaramete/xgraph/frontend/XGraph.html:6997-7038` (the `handleExtract` function) and `:7073` (the submit button label).

**Interfaces:**
- Consumes: `props.buildMode` — a string, `'append'` (default/absent) or `'recreate'`, passed by `BuildPanel` (Task 2). Also the already-present `props.gwClient` (has `.deleteGraph(graph)` and `.extract(graph, payload, hint)`), `props.extractGraph`.
- Produces: no new exported symbol; `ExtractPanel` simply honors `buildMode`. When `buildMode === 'recreate'`, it confirms, calls `gwClient.deleteGraph(graphName)` (tolerating failure), bypasses the "already added" short-circuit, then extracts.

**Context:** `ExtractPanel` today always appends: identical re-pushes short-circuit (`already`), and it never deletes. Recreate must rebuild from scratch, so it (a) must NOT short-circuit on an identical source, and (b) must drop the graph first. `gwClient.deleteGraph` already exists (`gateway.js:140`).

- [ ] **Step 1: Add the Recreate branch to `handleExtract`**

In `handleExtract`, the current guard is:

```javascript
        var already = extractSources.some(function(s){ return s.graph === graphName && (s.engine === graphEngine || !s.engine) && s.key === srcKey; });
        if (already) {
            setError(null);
            setNoop('This source was already added to ' + graphName + ' — skipped (no new extraction).');
            return;
        }
```

Change the `if (already)` line so Recreate ignores the short-circuit:

```javascript
        var already = extractSources.some(function(s){ return s.graph === graphName && (s.engine === graphEngine || !s.engine) && s.key === srcKey; });
        if (already && props.buildMode !== 'recreate') {
            setError(null);
            setNoop('This source was already added to ' + graphName + ' — skipped (no new extraction).');
            return;
        }
```

Then, immediately after the line `setBusy(true); setError(null); setResult(null);` and before `try {`, insert the confirm + delete:

```javascript
        if (props.buildMode === 'recreate') {
            if (!window.confirm('Recreate "' + graphName + '"? This deletes the existing graph, then rebuilds it from this document.')) {
                setBusy(false);
                return;
            }
            // Idempotent: a missing graph is fine — the extract below rebuilds it.
            try { await gwClient.deleteGraph(graphName); } catch (e) { /* best effort */ }
        }
```

Note: the `setBusy(false)` on cancel is required because `setBusy(true)` already ran on the line above.

- [ ] **Step 2: Reflect the mode in the submit button label**

At `:7073`, the button currently reads:

```javascript
            }}>{busy ? 'Extracting…' : 'Extract & Build'}</button>
```

Change the idle label to depend on `buildMode`:

```javascript
            }}>{busy ? 'Extracting…' : (props.buildMode === 'recreate' ? 'Recreate & Build' : 'Extract & Build')}</button>
```

- [ ] **Step 3: Run the esbuild JSX check**

Run the Global-Constraints esbuild command.
Expected: `ESBUILD_OK`.

- [ ] **Step 4: Commit**

```bash
cd /home/kkaramete/xgraph
git add frontend/XGraph.html
git commit -m "feat(build): ExtractPanel honors buildMode (recreate = delete then extract)"
```

---

## Task 2: BuildPanel component (the roof + radios)

**Files:**
- Modify: `/home/kkaramete/xgraph/frontend/XGraph.html` — insert a new `BuildPanel` function immediately after `ExtractPanel`'s closing `}` (just before the `/* ═══ StoragePanel ═══ */` banner comment at `:7176`).

**Interfaces:**
- Consumes (props, supplied by App in Task 3):
  - `props.extractProps` — an object spread verbatim onto `<ExtractPanel {...props.extractProps} buildMode={effMode} />`. Contains exactly the props `ExtractPanel` receives today: `gwClient, setGraphs, setActiveGraph, setActiveAction, extractSources, setExtractSources, extractGraph, setExtractGraph, graphEngine, onGraphUpdated`.
  - `props.createProps` — an object spread verbatim onto `<CreatePanel {...props.createProps} />`. Contains exactly the props `CreatePanel` receives today (all the `createNode*` / `createEdge*` / `createDdl` pairs plus `graphEngine, gwClient, setGraphs, setActiveAction, createGraphName, setCreateGraphName, activeGraph, extractSources, createHistory, onCreated`).
- Produces: the `BuildPanel(props)` function symbol, rendered by App's dispatch in Task 3.

**Context:** `BuildPanel` owns two local radios and renders one of the two *existing* panels. It adds no new create/extract logic — it only chooses which panel is visible and, for Documents, which `buildMode` to pass. Default Source is `'documents'` (Extract-first, per the "unify Extract in first" decision); default Mode is `'append'`. For Tables/files, structured Append is Slice B, so the Append radio is disabled and the effective mode is forced to `'recreate'` (which is what `CreatePanel`'s `CREATE OR REPLACE` already does — the mode is presentational there).

- [ ] **Step 1: Insert the `BuildPanel` function**

Insert this block right after the closing `}` of `ExtractPanel` and before the `StoragePanel` banner comment at `:7176`:

```javascript
/* ═══════════════════ BuildPanel ═══════════════════════════
   One "Build a graph" roof unifying Create (structured, from
   tables/files) and Extract (from documents). A Source radio swaps
   between the existing CreatePanel and ExtractPanel; a Mode radio
   picks Append vs Recreate. This panel adds NO build logic — it only
   selects which sub-panel shows and, for Documents, the buildMode it
   receives. Structured Append is Slice B, so Append is disabled when
   Source = Tables/files (CreatePanel already does CREATE OR REPLACE). */
function BuildPanel(props) {
    const [source, setSource] = useState('documents'); // 'documents' | 'tables'
    const [mode, setMode] = useState('append');        // 'append' | 'recreate'
    // Tables/files: structured append is Slice B — CreatePanel is always
    // CREATE OR REPLACE, so the effective mode there is 'recreate'.
    var effMode = source === 'tables' ? 'recreate' : mode;

    var wrap = { display:'flex', flexDirection:'column', flex:1, minHeight:0, width:'100%' };
    var bar = { display:'flex', flexWrap:'wrap', alignItems:'center', gap:18, padding:'12px 16px', borderBottom:'1px solid #eee', background:'#fafbfc' };
    var groupLbl = { fontSize:12, fontWeight:700, color:'#636e72', marginRight:8 };

    function radio(name, val, cur, set, text, disabled) {
        return (
            <label style={{ display:'inline-flex', alignItems:'center', gap:5, fontSize:13, cursor: disabled ? 'not-allowed' : 'pointer', opacity: disabled ? 0.45 : 1 }}
                   title={disabled ? 'Structured append is coming in a later slice — use Recreate (CREATE OR REPLACE) for now.' : ''}>
                <input type="radio" name={name} checked={cur === val} disabled={!!disabled}
                       onChange={function(){ if (!disabled) set(val); }} />
                {text}
            </label>
        );
    }

    return (
        <div style={wrap}>
            <div style={bar}>
                <div style={{ display:'inline-flex', alignItems:'center' }}>
                    <span style={groupLbl}>Source</span>
                    {radio('build-source', 'tables', source, setSource, 'Tables / files', false)}
                    {radio('build-source', 'documents', source, setSource, 'Documents (extract)', false)}
                </div>
                <div style={{ display:'inline-flex', alignItems:'center' }}>
                    <span style={groupLbl}>Mode</span>
                    {radio('build-mode', 'append', mode, setMode, 'Append', source === 'tables')}
                    {radio('build-mode', 'recreate', effMode, setMode, 'Recreate', false)}
                </div>
            </div>
            {source === 'documents'
                ? <ExtractPanel {...props.extractProps} buildMode={effMode} />
                : <CreatePanel {...props.createProps} />}
        </div>
    );
}

```

Note the Mode radios bind their `checked` to `effMode`/`mode` as shown: the Recreate radio reads `effMode` so it appears selected (and locked) when Source = Tables/files; the Append radio reads `mode` and is disabled there.

- [ ] **Step 2: Run the esbuild JSX check**

Run the Global-Constraints esbuild command.
Expected: `ESBUILD_OK`.

(At this point `BuildPanel` is defined but not yet rendered — the check only proves it parses. Integration is Task 3.)

- [ ] **Step 3: Commit**

```bash
cd /home/kkaramete/xgraph
git add frontend/XGraph.html
git commit -m "feat(build): add BuildPanel roof with Source/Mode radios"
```

---

## Task 3: Wire BuildPanel into the action bar + render dispatch + version bump

**Files:**
- Modify: `/home/kkaramete/xgraph/frontend/XGraph.html:6539-6540` (ACTIONS), `:9371-9403` (render dispatch), `:50` (version).

**Interfaces:**
- Consumes: `BuildPanel` (Task 2), `ExtractPanel` (Task 1). All App state/handlers referenced below already exist and are currently passed to `CreatePanel`/`ExtractPanel` at `:9371-9403` — this task only regroups them into `createProps` / `extractProps` objects.
- Produces: a single `build` action; the two old actions are gone.

**Context:** No code elsewhere references `activeAction === 'create'` or `'extract'` outside `:9371/:9394` (verified), and no `setActiveAction('create'/'extract')` calls exist, so the key rename is contained to these two sites. The Query-tab `q.kind === 'create'` references are unrelated and must stay.

- [ ] **Step 1: Replace the two ACTIONS entries with one `build` entry**

At `:6539-6540`, replace:

```javascript
    { key: 'create',    label: 'Create',    reachable: function(s) { return !!(s.connected || (s.graphs && s.graphs.length > 0)); } },
    { key: 'extract',   label: 'Extract',   reachable: function(s) { return !!(s.connected || (s.graphs && s.graphs.length > 0)); } },
```

with:

```javascript
    { key: 'build',     label: 'Build',     reachable: function(s) { return !!(s.connected || (s.graphs && s.graphs.length > 0)); } },
```

- [ ] **Step 2: Replace the two render blocks with one BuildPanel block**

At `:9371-9403`, replace the entire `{activeAction === 'create' && (…)}` and `{activeAction === 'extract' && (…)}` pair with:

```javascript
                {activeAction === 'build' && (
                    <BuildPanel
                        extractProps={{
                            gwClient: gwClient, setGraphs: setGraphs,
                            setActiveGraph: setActiveGraph, setActiveAction: setActiveAction,
                            extractSources: extractSources, setExtractSources: setExtractSources,
                            extractGraph: extractGraph, setExtractGraph: setExtractGraph,
                            graphEngine: graphEngine,
                            onGraphUpdated: refreshGraph,
                        }}
                        createProps={{
                            graphEngine: graphEngine, gwClient: gwClient,
                            setGraphs: setGraphs, setActiveAction: setActiveAction,
                            createGraphName: createGraphName, setCreateGraphName: setCreateGraphName,
                            createNodeTable: createNodeTable, setCreateNodeTable: setCreateNodeTable,
                            createNodeFile: createNodeFile, setCreateNodeFile: setCreateNodeFile,
                            createNodeSql: createNodeSql, setCreateNodeSql: setCreateNodeSql,
                            createNodeIdCol: createNodeIdCol, setCreateNodeIdCol: setCreateNodeIdCol,
                            createNodeLabelCol: createNodeLabelCol, setCreateNodeLabelCol: setCreateNodeLabelCol,
                            createNodeProps: createNodeProps, setCreateNodeProps: setCreateNodeProps,
                            createEdgeTable: createEdgeTable, setCreateEdgeTable: setCreateEdgeTable,
                            createEdgeFile: createEdgeFile, setCreateEdgeFile: setCreateEdgeFile,
                            createEdgeSql: createEdgeSql, setCreateEdgeSql: setCreateEdgeSql,
                            createEdgeIdCol: createEdgeIdCol, setCreateEdgeIdCol: setCreateEdgeIdCol,
                            createEdgeSrcCol: createEdgeSrcCol, setCreateEdgeSrcCol: setCreateEdgeSrcCol,
                            createEdgeTgtCol: createEdgeTgtCol, setCreateEdgeTgtCol: setCreateEdgeTgtCol,
                            createEdgeTypeCol: createEdgeTypeCol, setCreateEdgeTypeCol: setCreateEdgeTypeCol,
                            createDdl: createDdl, setCreateDdl: setCreateDdl,
                            activeGraph: activeGraph, extractSources: extractSources,
                            createHistory: createHistory, onCreated: recordCreate,
                        }}
                    />
                )}
```

(These are exactly the props the old `<CreatePanel>` and `<ExtractPanel>` received at `:9372-9402`, regrouped. Do not add or drop any.)

- [ ] **Step 3: Bump the version badge**

At `:50`, change:

```javascript
const EXPLORER_VERSION = "0.3.6";  // xGraph version — bump on frontend changes so a stale browser cache is visible
```

to:

```javascript
const EXPLORER_VERSION = "0.4.0";  // xGraph version — bump on frontend changes so a stale browser cache is visible
```

- [ ] **Step 4: Run the esbuild JSX check**

Run the Global-Constraints esbuild command.
Expected: `ESBUILD_OK`.

- [ ] **Step 5: Verify the gateway serves the page (HTTP 200)**

The gateway also serves the UI. Ensure it's up, then curl:

```bash
cd /home/kkaramete/xgraph
./xgraph status >/dev/null 2>&1 || ./xgraph start
sleep 1
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8090/
```

Expected: `200`. (If `./xgraph start` was needed, that's fine — it runs in the background.)

- [ ] **Step 6: Confirm the old action keys are gone and `build` is the only creation entry**

```bash
cd /home/kkaramete/xgraph/frontend
grep -nE "key: 'create'|key: 'extract'|activeAction === 'create'|activeAction === 'extract'" XGraph.html || echo "NO_STALE_ACTION_KEYS"
grep -nE "key: 'build'|activeAction === 'build'" XGraph.html
```

Expected: `NO_STALE_ACTION_KEYS`, then two matches for the `build` key/dispatch. (The `q.kind === 'create'` Query-tab references are unaffected and won't match these patterns.)

- [ ] **Step 7: Commit**

```bash
cd /home/kkaramete/xgraph
git add frontend/XGraph.html
git commit -m "feat(build): unify Create+Extract into one Build action (v0.4.0)"
```

---

## Manual (browser) acceptance — run after Task 3

The React app cannot be verified headlessly (CLAUDE.md). Hard-reload `http://localhost:8090/`, confirm `v0.4.0` shows, then:

1. The action bar shows a single **Build** button (no separate Create/Extract).
2. Build opens with **Source = Documents (extract)**, **Mode = Append**; the Extract form renders; the button reads **Extract & Build**. A normal extract still appends as before.
3. Switch **Mode = Recreate** → button reads **Recreate & Build**; extracting prompts a confirm, and on OK the graph is dropped then rebuilt (an identical source is no longer skipped).
4. Switch **Source = Tables / files** → the Create form renders; **Append is disabled** (tooltip explains Slice B), Recreate is selected; **Create / Replace graph** still builds as before.
5. The Kinetica `show_graph` provenance banner still appears inside the Tables/files (Create) frame for a Kinetica graph.

---

## Self-Review

- **Spec coverage (Slice A):** roof + Source/Mode radios (Task 2); Extract folded in with Append/Recreate = `delete_graph`→`extract` (Task 1); `Create`+`Extract` → one `Build` (Task 3); structured Append disabled as Slice B (Task 2); no backend / `gateway.js` change (all tasks reuse existing client calls). Covered.
- **Placeholder scan:** none — every code step shows the exact before/after string and the exact command with expected output.
- **Type/name consistency:** `buildMode` is produced in Task 2 (`buildMode={effMode}`) and consumed in Task 1 (`props.buildMode`); `extractProps`/`createProps` object keys in Task 3 match the prop names `ExtractPanel`/`CreatePanel` read today (verified against `:6972-6977` and `:6739-6757`); `refreshGraph`/`recordCreate` are the exact handler names the old dispatch used (`:9391`, `:9401`).
- **Scope:** Slice A only; Slices B (structured builder generalization, `/tables`·`/columns`·`/register_file`, Kinetica file sources) and C (polish) are out of scope and get their own plans.
