# Unified Query panel — design (fold Ask + Explain into Query)

**Date:** 2026-07-20
**Status:** Approved design, pre-implementation
**Origin:** The action bar has three overlapping tabs — **Ask** (NL question → answer), **Query** (SQL/Cypher editor + Query Helper + results), **Explain** (post-join semantic explanation over query results). They belong together: all three are "ask the graph a question and see the result." Consolidate into one **Query** action so a user can reach the query text three ways (NL Ask, Query Helper, or paste), run it, and Explain the results — all in one spot.

## Problem

Ask, Query, and Explain are separate tabs that pass data between each other awkwardly: Ask hands a generated Cypher into a new Query tab via `onRunInQuery`; Explain reads the active Query tab's lifted `sql`/`result` from App state. The three duplicate result-rendering and force-graph code. The user's mental model is one place: produce a query (any way) → run → explain.

## Decisions (locked during brainstorming)

1. **Ask runs fully + fills the editor.** An NL box inside Query calls `gwClient.ask` (today's one-shot: answer + results). The generated Cypher lands in the editor (editable/re-runnable), the response populates the panel's result (existing Results/Visualization tabs render it), and the plain-English answer shows in a small bubble.
2. **Explain is a tab.** Once a result exists, an **Explain** tab joins `View Results · Visualization · Map`, with a focus input + Explain button calling `gwClient.explain`.
3. **Latest answer bubble only** — drop Ask's per-graph conversation-history list (the round-trips still work; `askHistory` App state is removed with the old panel).
4. **Leave Storage for now** — out of scope; the resulting bar is `Setup · Build · List · Query · Visualize · Ontology · Storage` (Storage folded in a later iteration).
5. **No backend changes** — reuses `gwClient.ask` / `gwClient.explain`; `nl2cypher`/`synthesize` remain unused.

## Architecture

### QueryPanel (the one place)

QueryPanel already owns: the Query Helper (`showHelper` → `generateQuery` → `setSqlAndNotify`), the SQL/Cypher editor (`sql`/`setSqlAndNotify`), Run (`executeQuery` → `gwClient.runQuery` → `setResult` + `onResultChange`), and the results tabs (`activeTab`: `results`/`graph`/`map`). It gains:

- **Ask row** (top of the panel, above the Query Helper): NL `<textarea>` + **Ask** button (Ctrl/Cmd+Enter). Handler calls `gwClient.ask(props.graphName, question)`; on success:
  - `setSqlAndNotify(res.cypher || '')` — fill the editor,
  - `setResult(res)` + `props.onResultChange(res)` — drive the Results/Visualization tabs (the `ask` response carries `columns`/`rows`/`graph`, same shape as a query result),
  - `setAskAnswer(res.answer)` — the bubble.
  - Show `askAnswer` in a small dismissible bubble above the results tabs (cleared on the next Run/Ask).
- **Explain tab:** a new `activeTab === 'explain'` value; its tab button renders in the tabs row when a result exists. The tab body holds a "What to focus on (optional)" `<input>` (`explainFocus`) + **Explain** button (`explainBusy`), calling `gwClient.explain(explainFocus.trim() || 'Explain these results', result.columns, result.rows, sql, HYDRATE_SOURCE, props.graphName)` into `explainResp`; renders `explainResp.answer` + (when `explainResp.hydrated`) the post-join SQL `<pre>` (`join_sql`) and the hydrated table (`tableFromGateway(explainResp)`). This is `InteractExplainPanel`'s logic inlined; `HYDRATE_SOURCE` is a module-level const already in scope.

State added to QueryPanel: `askQuestion`, `askBusy`, `askAnswer`; `explainFocus`, `explainBusy`, `explainResp`, `explainError`. All local (per tab, since each queryTab is its own QueryPanel instance) — no new App-level bus.

### Action bar + dead code

- Remove `{ key: 'interact1', label: 'Ask', … }` and `{ key: 'interact2', label: 'Explain', … }` from `ACTIONS`.
- Remove the `activeAction === 'interact1'` and `activeAction === 'interact2'` render blocks.
- Delete `InteractAskPanel` and `InteractExplainPanel` functions and the App state/handlers only they used (`askHistory`/`setAskHistory`, the `onRunInQuery` handoff). Keep everything Query/Explain shared that Query still needs (`queryTabSql`/`queryTabResult` stay — they back the tabs).

## Data flow

- **NL → answer:** Ask box → `gwClient.ask` → editor filled + result set + answer bubble → Results/Visualization tabs render → Explain tab available.
- **Helper → query:** Query Helper → `generateQuery` → editor (unchanged).
- **Paste → query:** type/paste into the editor (unchanged) → Run.
- **Explain:** Explain tab → `gwClient.explain(focus, result.columns, result.rows, sql, HYDRATE_SOURCE, graph)` → answer + post-join SQL + hydrated table.

## Error handling

- Ask/Explain failures set a local error string shown near their control; they never crash the panel. Ask with no `activeGraph` is prevented (Query is only reachable with an active graph). Explain with no result → the tab/button is disabled or shows "Run a query first."

## Testing

- `gwClient.ask`/`explain` are already exercised via the Node client tests. The consolidated React panel is browser-verified (CLAUDE.md: no headless React runtime). Gate: esbuild JSX check (`ESBUILD_OK`) + gateway `curl` 200.

## Files (indicative)

- **Frontend only:** `XGraph.html` — QueryPanel (Ask row + Explain tab + state), remove `InteractAskPanel`/`InteractExplainPanel` + their ACTIONS entries + render blocks + `askHistory` state, version bump. No `gateway.js`, no backend.

## Deferred / out of scope (the user's "next iteration" vision)

- **Table Views in List** (like the explorer project).
- **Ontology under Visualize.**
- **Storage** consolidation.
- These are separate future tasks, each its own spec/plan.
