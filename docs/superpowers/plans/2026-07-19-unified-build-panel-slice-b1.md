# Unified "Build" Panel — Slice B1 Implementation Plan (Kinetica structured builder)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make xGraph's carried-over-but-orphaned Create-Helper builder *live* for Kinetica by extracting it into a standalone `CreateHelperPanel` and mounting it in Build → Tables/files, above the existing DDL field, so a user fills NODES/EDGES/WEIGHTS/RESTRICTIONS sections instead of hand-writing `CREATE GRAPH` DDL.

**Architecture:** Frontend-only, single-file `frontend/XGraph.html` (React 18 UMD + Babel-standalone, no build step). The builder currently lives inside `QueryPanel` behind `props.createHelper`, which **nothing mounts** (verified: the `+ Create` `DashboardHeader` and floating `queryPanels` array are never rendered). B1 (a) hoists the two pure, cross-shared helpers to module scope, (b) copies the builder into a new focused `CreateHelperPanel` that emits DDL via one `onGenerate(ddl)` callback, (c) mounts it in `CreatePanel`'s Kinetica branch wired to the existing `gwClient.create({graph, ddl})` execute path, then (d) removes `QueryPanel`'s now-redundant orphaned copy. Steps (a)–(c) ship the value; (d) is the de-dup cleanup, done last so B1 still lands if (d) is deferred.

**Tech Stack:** React 18 UMD + Babel-standalone. Validation via the project's local `esbuild` JSX check; behavior acceptance is browser-driven (the React app cannot be runtime-verified headlessly — CLAUDE.md).

## Global Constraints

- **No `git commit` unless the user authorizes it** (CLAUDE.md). This plan's default is local commits on `main` (repo convention).
- **These are code-MOVE tasks over existing, working code.** Copy the cited source verbatim; change ONLY the adaptations each task names. Do not "improve," rename, or reflow the moved code — a faithful move keeps the diff reviewable.
- **Anchor every edit on verbatim code strings, not line numbers** — the file is ~9,650 lines and line numbers drift as you edit. Cited line numbers are current-as-of-planning approximations.
- **Do NOT alter the live query-tab path.** `QueryPanel`'s query-run, Solve, and Match behavior, its `sql`/`result`/`error`/`size`/`pos`/session-save for *those* modes, and the embedded Query-tab render (`activeAction === 'query'`, `embedded={true}`) must be byte-for-byte unaffected. Only the `createHelper`-gated (dead) parts move/change.
- **The builder only GENERATES DDL.** It never executes. Execution stays the existing `CreatePanel` **Create / Replace graph** button → `gwClient.create({graph, ddl})`. Do not add a Run/execute path to `CreateHelperPanel`.
- **Kinetica only in B1.** `CreateHelperPanel` mounts only when `graphEngine === 'kinetica'`. Non-Kinetica keeps today's `CreatePanel` form untouched (B2 generalizes).
- **Every frontend edit is validated by the esbuild JSX check** (command below); it must print `ESBUILD_OK` before each commit.
- **Version badge:** bump `EXPLORER_VERSION` from `0.4.0` to `0.5.0` in the final task.
- Commit messages: concise 1–2 lines, no `Co-Authored-By` footer.

### Frontend esbuild JSX check (the "run the test" step throughout)

```bash
cd /home/kkaramete/xgraph/frontend
end=$(grep -n '</script>' XGraph.html | tail -1 | cut -d: -f1)
sed -n "47,$((end-1))p" XGraph.html | ./node_modules/.bin/esbuild --loader=jsx > /dev/null && echo ESBUILD_OK || echo ESBUILD_FAIL
```

Expected on success: `ESBUILD_OK`.

---

## File Structure

- **Modify only:** `/home/kkaramete/xgraph/frontend/XGraph.html`
  - Module scope (~line 100–260, where `extractNodeSourceTable` etc. live): receives the hoisted `chGenericId`, `chParseRef`, `parseCreateGraphStmt`, `buildAlterSqlFromCh` (Task 1 + Task 2's parser hoist).
  - `QueryPanel` (~1410–3760): its `chGenericId`/`chParseRef` closures are removed in Task 1 (now module-level); its orphaned `createHelper` block + CH-only state are removed in Task 4.
  - New `CreateHelperPanel` function, inserted just before `CreatePanel` (~line 6739) (Task 2).
  - `CreatePanel` (~6739–6958): Kinetica branch gains `<CreateHelperPanel … onGenerate={setCreateDdl} />` above the DDL textarea (Task 3).
  - `BuildPanel.createProps` / App dispatch (~7228, ~9430): thread `graphGrammar`, `tables`, `tableColumnsCache`, `onFetchTableColumns` through to `CreatePanel` (Task 3).
  - `EXPLORER_VERSION` (~line 50): `0.4.0` → `0.5.0` (Task 4).

No new files. Landmarks (current approx. lines): `chGenericId` 1948–1968; `chParseRef` 2045–2050; CH builders `chPickedTable`/`chAddConfig`/`chRemoveGroup`/`chUpdateRow`/`chRemoveRow`/`chBuildSection`/`chBuildOptions`/`chGenerate`/`chAddOption`/`chUpdateOption`/`chRemoveOption` in 1436–2113; CH state `chShow`/`chGraphName`/`chDirected`/`chRows`/`chSectionTable`/`chOptions`/`chExpanded` 1478–1494 and `chMode` 1936; CH section JSX `{createHelper && (` 3024 → `)}` 3178; `parseCreateGraphStmt` ~8305, `buildAlterSqlFromCh` ~8393; App grammar/tables/columns state `DEFAULT_GRAMMAR`/`graphGrammar` 7778–7815, `tables` 7769, `tableColumnsCache`/`fetchTableColumns` 7824–7841; `CreatePanel` `handleCreate` 6794 + Kinetica DDL textarea 6900–6905.

---

## Task 1: Hoist `chGenericId` + `chParseRef` to module scope

**Files:** Modify `/home/kkaramete/xgraph/frontend/XGraph.html`.

**Interfaces:**
- Produces: two module-level functions `chGenericId(id, component)` and `chParseRef(s)`, identical in behavior to the current `QueryPanel` closures. After this task, all in-`QueryPanel` callers (`chBuildSection`, Solve's `shAliasFor`/`shBuildSection`, Match's `mhAliasFor`/`mhBuildSection`) resolve to the module-level versions, and `CreateHelperPanel` (Task 2) can call them too.
- Consumes: nothing new.

**Context:** Both functions are pure (args only, no captured state — verified). Moving them out of `QueryPanel` lets the new component share them without duplication and without importing `QueryPanel`.

- [ ] **Step 1: Copy the two functions to module scope**

Read the current bodies of `chGenericId` (≈1948–1968) and `chParseRef` (≈2045–2050) from `QueryPanel`. Insert them **verbatim** (converting the `function chGenericId(...)` / `function chParseRef(...)` closure declarations into identical module-level `function` declarations) into the module-level parser block near `extractNodeSourceTable` — anchor: insert immediately BEFORE the line `function extractNodeSourceTable(` (≈line 106). Do not change their bodies.

- [ ] **Step 2: Remove the two now-duplicate closures from `QueryPanel`**

Delete the `function chGenericId(id, component) { … }` closure (≈1948–1968) and the `function chParseRef(s) { … }` closure (≈2045–2050) from inside `QueryPanel`. Every remaining caller (`chGenericId(`, `chParseRef(`) now resolves to the module-level functions. Do not touch the callers.

- [ ] **Step 3: Run the esbuild JSX check**

Run the Global-Constraints esbuild command. Expected: `ESBUILD_OK`.

- [ ] **Step 4: Verify no stray second definition remains**

```bash
cd /home/kkaramete/xgraph/frontend
grep -nE "function chGenericId|function chParseRef" XGraph.html
```
Expected: exactly ONE `function chGenericId` and ONE `function chParseRef` (the module-level ones).

- [ ] **Step 5: Commit**

```bash
cd /home/kkaramete/xgraph
git add frontend/XGraph.html
git commit -m "refactor(build): hoist chGenericId/chParseRef to module scope for reuse"
```

---

## Task 2: Extract the standalone `CreateHelperPanel` component

**Files:** Modify `/home/kkaramete/xgraph/frontend/XGraph.html` — add a new `CreateHelperPanel` function immediately BEFORE `function CreatePanel(props) {` (anchor on that string, ≈line 6739). Also hoist `parseCreateGraphStmt` + `buildAlterSqlFromCh` to module scope (needed for the Modify round-trip and to keep them near `chGenericId`).

**Interfaces:**
- Produces: `CreateHelperPanel(props)`. Props it consumes:
  - `graphGrammar` (object; the `DEFAULT_GRAMMAR`-shaped legacy grammar) — required for the section config dropdowns.
  - `tables` (array; may be empty) — section table dropdowns; empty is fine (manual entry).
  - `tableColumnsCache` (object) + `onFetchTableColumns(tableName)` (function) — column autocomplete; best-effort, degrades to manual entry.
  - `graphName` (string) + `setGraphName(v)` — the graph name field (shared with `CreatePanel`'s `createGraphName`).
  - `initialMode` (`'recreate'` | `'modify'`, default `'recreate'`).
  - `onGenerate(ddl)` — called with the assembled DDL string when the user clicks **Generate**.
- Owns (internal state): `chRows`, `chSectionTable`, `chOptions`, `chDirected`, `chMode`, `chExpanded`, `chShow`. It does NOT own graph-name (that is the `graphName`/`setGraphName` prop) and does NOT own `sql`/results/window-chrome.
- Consumes: the module-level `chGenericId`/`chParseRef` from Task 1.

**Context:** This is a faithful copy of the builder out of `QueryPanel` into a focused component, with exactly three adaptations: (1) the graph-name input binds to `props.graphName`/`props.setGraphName` instead of local `chGraphName`; (2) the **Generate** handler calls `props.onGenerate(ddl)` with the assembled DDL instead of `setSqlAndNotify(...)`; (3) `graphGrammar`/`tables`/`tableColumnsCache`/`onFetchTableColumns` come from `props` instead of `QueryPanel`'s props/closures. `QueryPanel`'s original copy stays in place for now (removed in Task 4) — temporary duplication is intentional so this task leaves a working build and Task 3 can prove the new component live before the old one is deleted.

- [ ] **Step 1: Hoist the Modify round-trip parsers to module scope**

Move `parseCreateGraphStmt(text)` (≈8305–8386) and `buildAlterSqlFromCh(ch)` (≈8393–8420) out of the App component to module scope, inserting them right after the `chParseRef` function added in Task 1. They capture no App state (verified) — move verbatim, then delete the originals from inside App. (Their only caller, `openModifyGraphPanel`, is itself orphaned; it will now reference the module-level versions.)

- [ ] **Step 2: Author `CreateHelperPanel` by copying the builder**

Insert a new `function CreateHelperPanel(props) { … }` immediately before `function CreatePanel(props) {`. Build its body by copying from `QueryPanel`, verbatim except the three adaptations:

  a. **State (own these):** copy the `useState` declarations for `chShow` (≈1478), `chDirected` (≈1480), `chRows` (≈1481), `chSectionTable` (≈1487), `chOptions` (≈1489), `chExpanded` (≈1494), and `chMode` (≈1936). Initialise `chMode` from `props.initialMode === 'modify' ? 'modify' : 'recreate'`. Do NOT copy `chGraphName` — use `props.graphName`/`props.setGraphName` wherever the original referenced `chGraphName`/`setChGraphName`.

  b. **Grammar/tables/columns:** where the original read `graphGrammar`, `availableTables`, `tableColumnsCache`, and called the column fetcher / `chPickedTable`, source them from `props.graphGrammar`, `props.tables || []`, `props.tableColumnsCache || {}`, and `props.onFetchTableColumns`. Copy `chPickedTable` (≈1436–1445) adapting its `availableTables` reference to `props.tables`.

  c. **Mutators/builders:** copy `chAddConfig` (≈1972–1988), `chRemoveGroup` (≈1989), `chUpdateRow` (≈1996), `chRemoveRow` (≈2003), `chAddOption` (≈2103), `chUpdateOption` (≈2104), `chRemoveOption` (≈2111), `chBuildSection` (≈2055–2075), `chBuildOptions` (≈2076–2081), and `chGenerate` (≈2082–2102) verbatim. In `chGenerate`, replace the final `setSqlAndNotify(sql)` / `setSql(...)` line (≈2101) with `props.onGenerate(sql);` — the assembled string is the same; only the sink changes. `chGenerate` must read the graph name from `props.graphName` (not `chGraphName`).

  d. **Render:** copy the Create-Helper section JSX from `{createHelper && (` (≈3024) through its closing `)}` (≈3178) as the component's returned JSX (drop the `createHelper && ` gate — this component IS the helper). Keep the graph-name input but bind it to `props.graphName`/`props.setGraphName`. Keep the **Generate** button (`onClick={chGenerate}`). Do NOT copy the surrounding `QueryPanel` window chrome (title bar, drag/resize handles ≈2955–3003), the DDL textarea (≈3735–3750), the Run button (≈3751–3758), or the vestigial success banner (≈3722–3734) — those belong to the host.

- [ ] **Step 3: Run the esbuild JSX check**

Run the Global-Constraints esbuild command. Expected: `ESBUILD_OK`. (`CreateHelperPanel` is defined but not yet mounted — that is expected; Task 3 mounts it.)

- [ ] **Step 4: Verify the sink adaptation landed**

```bash
cd /home/kkaramete/xgraph/frontend
awk '/^function CreateHelperPanel/{f=1} f&&/props\.onGenerate\(/{print NR": "$0} /^function CreatePanel/{f=0}' XGraph.html
```
Expected: at least one line showing `props.onGenerate(` inside `CreateHelperPanel` (the `chGenerate` sink).

- [ ] **Step 5: Commit**

```bash
cd /home/kkaramete/xgraph
git add frontend/XGraph.html
git commit -m "feat(build): extract standalone CreateHelperPanel (emits DDL via onGenerate)"
```

---

## Task 3: Mount `CreateHelperPanel` in CreatePanel's Kinetica branch + thread props

**Files:** Modify `/home/kkaramete/xgraph/frontend/XGraph.html` — `CreatePanel` (Kinetica branch ≈6900–6905 and its `props` destructuring ≈6739–6757), `BuildPanel`'s `createProps` at the App dispatch (≈9430+), and `BuildPanel` (≈7192) if it needs to forward new props (it spreads `createProps`, so no change there).

**Interfaces:**
- Consumes: `CreateHelperPanel` (Task 2); App state `graphGrammar` (≈7815), `tables` (≈7769), `tableColumnsCache` (≈7824), `fetchTableColumns` (≈7825). These already exist in App.
- Produces: a live structured builder in Build → Tables/files for Kinetica; `onGenerate` writes into `createDdl`, executed by the existing button.

**Context:** `CreatePanel` already receives `createDdl`/`setCreateDdl` and renders the DDL textarea in its Kinetica branch. `BuildPanel` passes `createProps` (a spread object) to `CreatePanel`. We add four keys to `createProps` and render `<CreateHelperPanel>` above the DDL textarea. Since `BuildPanel` spreads `{...props.createProps}` onto `CreatePanel`, the new keys flow through with no `BuildPanel` edit.

- [ ] **Step 1: Add the four keys to `createProps` in the App dispatch**

In the `activeAction === 'build'` dispatch's `createProps={{ … }}` object (added in Slice A, ≈line 9430), add these four entries (values are existing App-scope identifiers):

```javascript
                            graphGrammar: graphGrammar, tables: tables,
                            tableColumnsCache: tableColumnsCache, onFetchTableColumns: fetchTableColumns,
```

- [ ] **Step 2: Destructure the new props in `CreatePanel`**

In `CreatePanel`'s `var … = props.…` destructuring block (≈6739–6757), add:

```javascript
    var graphGrammar = props.graphGrammar, tables = props.tables || [];
    var tableColumnsCache = props.tableColumnsCache || {}, onFetchTableColumns = props.onFetchTableColumns;
```

- [ ] **Step 3: Render `<CreateHelperPanel>` above the DDL textarea (Kinetica branch only)**

In the Kinetica branch (the `graphEngine === 'kinetica' ? ( … )` block, ≈6900–6905), insert the builder immediately BEFORE the `<Label>DDL</Label>` line so the sections sit above the (still-visible, still-editable) DDL field:

```javascript
                    <CreateHelperPanel
                        graphGrammar={graphGrammar} tables={tables}
                        tableColumnsCache={tableColumnsCache} onFetchTableColumns={onFetchTableColumns}
                        graphName={createGraphName} setGraphName={setCreateGraphName}
                        initialMode="recreate"
                        onGenerate={function(ddl){ setCreateDdl(ddl); }}
                    />
```

Leave the `<Label>DDL</Label>` textarea and the caption ("Full DDL authoring is on you…") in place — the generated DDL lands in that textarea, visible and editable, and the existing **Create / Replace graph** button runs it.

- [ ] **Step 4: Run the esbuild JSX check**

Run the Global-Constraints esbuild command. Expected: `ESBUILD_OK`.

- [ ] **Step 5: Verify the gateway serves the page (HTTP 200)**

```bash
cd /home/kkaramete/xgraph
./xgraph status >/dev/null 2>&1 || ./xgraph start
sleep 1
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8090/
```
Expected: `200`.

- [ ] **Step 6: Commit**

```bash
cd /home/kkaramete/xgraph
git add frontend/XGraph.html
git commit -m "feat(build): mount CreateHelperPanel in CreatePanel Kinetica branch (Generate fills DDL)"
```

---

## Task 4: Remove QueryPanel's orphaned Create-Helper + version bump

**Files:** Modify `/home/kkaramete/xgraph/frontend/XGraph.html` — `QueryPanel` (CH-only state ≈1478–1494 + `chMode` ≈1936; CH mutators/builders ≈1436–2113 that are now unused in `QueryPanel`; the `{createHelper && ( … )}` JSX block ≈3024–3178; the `props.createHelper` gate ≈1425; and the `ch*` references in `QueryPanel`'s session-save effect ≈2543–2559 and `initCH` seeding ≈1477). Also `EXPLORER_VERSION` (≈50).

**Interfaces:**
- Consumes: nothing new.
- Produces: a single copy of the builder (in `CreateHelperPanel`); `QueryPanel` no longer carries the dead Create-Helper.

**Context:** `QueryPanel`'s Create-Helper is unmounted dead code (nothing renders `<QueryPanel createHelper />`). Removing it cannot change live behavior — but the removal must be surgical: the query-run, Solve, and Match paths and their session-save must stay intact. Do this LAST so Task 3's live builder is already proven; if any ambiguity arises, STOP and report rather than risk the live query tabs.

- [ ] **Step 1: Remove the CH-only state and the `createHelper` gate**

Delete from `QueryPanel`: the `var createHelper = !!props.createHelper;` line (≈1425); the CH-only `useState` declarations `chShow`/`chGraphName`/`chDirected`/`chRows`/`chSectionTable`/`chOptions`/`chExpanded` (≈1478–1494) and `chMode` (≈1936); and the `create` portion of the `initCH`/`initialHelper` seeding (≈1477) — **keep** any `initSH`/`initMH` (Solve/Match) seeding untouched. Do NOT remove Solve (`sh*`) or Match (`mh*`) state.

- [ ] **Step 2: Remove the CH mutators/builders now unused in `QueryPanel`**

Delete the `QueryPanel` copies of `chPickedTable`, `chAddConfig`, `chRemoveGroup`, `chUpdateRow`, `chRemoveRow`, `chBuildSection`, `chBuildOptions`, `chGenerate`, `chAddOption`, `chUpdateOption`, `chRemoveOption` (within ≈1436–2113). **Do NOT delete** anything Solve/Match still call — in particular the module-level `chGenericId`/`chParseRef` (Task 1) stay, and `shBuildSection`/`mhBuildSection`/`shAliasFor`/`mhAliasFor` stay. If a function is referenced by any `sh*`/`mh*` code, keep it and report it as a concern.

- [ ] **Step 3: Remove the CH render block**

Delete the `{createHelper && ( … )}` JSX block (≈3024–3178) in `QueryPanel`. Leave the DDL textarea (≈3735–3750), Run button, `result`/`error` rendering, and all Solve/Match render blocks intact.

- [ ] **Step 4: Excise `ch*` from the session-save effect**

In `QueryPanel`'s session-save `useEffect` (≈2543–2559), remove the `ch*` values from the serialized `helper.create` object and from the effect's dependency array. **Keep** `helper.solve`/`helper.match` and their deps. If the effect serializes create/solve/match together in a shape that would break, STOP and report — do not guess.

- [ ] **Step 5: Bump the version badge**

Change `EXPLORER_VERSION` (≈50) from `"0.4.0"` to `"0.5.0"`.

- [ ] **Step 6: Run the esbuild JSX check + verify single builder copy**

```bash
cd /home/kkaramete/xgraph/frontend
end=$(grep -n '</script>' XGraph.html | tail -1 | cut -d: -f1)
sed -n "47,$((end-1))p" XGraph.html | ./node_modules/.bin/esbuild --loader=jsx > /dev/null && echo ESBUILD_OK || echo ESBUILD_FAIL
grep -c "function chGenerate" XGraph.html   # expect 1 (only CreateHelperPanel's)
grep -n "props.createHelper\|createHelper &&" XGraph.html || echo "NO_ORPHAN_CREATEHELPER"
```
Expected: `ESBUILD_OK`; `chGenerate` count `1`; `NO_ORPHAN_CREATEHELPER`.

- [ ] **Step 7: Verify gateway 200 + commit**

```bash
cd /home/kkaramete/xgraph
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8090/   # expect 200
git add frontend/XGraph.html
git commit -m "refactor(build): remove QueryPanel's orphaned Create-Helper; v0.5.0"
```

If Step 4/6 reveal that removing the session-save `ch*` references risks the live query/solve/match serialization, STOP after Task 3, leave the orphaned copy in place, bump the version in a minimal commit, and report — B1's value (the live Kinetica builder) has already shipped.

---

## Manual (browser) acceptance — run after Task 3 (and re-confirm after Task 4)

Hard-reload `http://localhost:8090/`, connect to **Kinetica**, open **Build**, Source = **Tables / files**:

1. The **NODES / EDGES / WEIGHTS / RESTRICTIONS** section builder appears above the DDL field (not just the raw textarea).
2. Add a NODES config (e.g. `NODE_NAME`), type a DEFAULT TABLE (e.g. `expero.vertexes`), set the column; add an EDGES config similarly. Click **Generate** → the DDL textarea fills with a `CREATE OR REPLACE DIRECTED GRAPH … NODES => INPUT_TABLES((SELECT … )) …` statement.
3. Edit the generated DDL by hand if desired, then **Create / Replace graph** builds the graph (existing execute path) and the graph appears in **List**.
4. `Recreate | Modify` and `Directed | Undirected` toggles behave; OPTIONS rows append `OPTIONS(...)`.
5. Table dropdowns may be empty and column autocomplete may not populate (expected in B1 — manual `table.column` entry works); the DDL still assembles correctly.
6. After Task 4: the Query tab (Cypher) still runs queries normally — the builder removal didn't touch query/solve/match.

---

## Self-Review

- **Spec coverage (B1):** hoist shared helpers (Task 1); standalone `CreateHelperPanel` emitting DDL via `onGenerate` (Task 2); mounted in the Kinetica Create branch on the existing `gwClient.create` execute path (Task 3); orphaned copy removed + version bump (Task 4). Kinetica-only; table-list/autocomplete limitations documented as B2. Matches the spec's B1 section.
- **Placeholder scan:** none — Task 1 and Task 3 give exact code; Tasks 2 and 4 are code-MOVEs that cite exact source ranges + the precise adaptations/removals (transcribing ~600 lines verbatim into the plan would be more error-prone than pointing at the source, which the implementer copies).
- **Type/name consistency:** `onGenerate(ddl)` is produced by `CreateHelperPanel` (Task 2) and consumed as `onGenerate={function(ddl){ setCreateDdl(ddl); }}` (Task 3); `createProps` keys `graphGrammar`/`tables`/`tableColumnsCache`/`onFetchTableColumns` added in Task 3 match the App identifiers (`graphGrammar` 7815, `tables` 7769, `tableColumnsCache` 7824, `fetchTableColumns` 7825) and the `CreatePanel` destructuring; module-level `chGenericId`/`chParseRef` (Task 1) are the single definitions both `QueryPanel` and `CreateHelperPanel` call.
- **Scope:** B1 only. No backend, no `gateway.js`, no `/tables`·`/columns` (B2), no DuckDB/FalkorDB generalization (B2), no Solve/Match unification (Slice C).
- **Risk sequencing:** the live builder ships in Task 3; the risky dead-code removal is isolated in Task 4 with an explicit STOP-and-defer escape hatch, so B1's value cannot be lost to a Task 4 complication.
