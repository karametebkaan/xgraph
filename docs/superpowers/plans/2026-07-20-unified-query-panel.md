# Unified Query panel — Implementation Plan (fold Ask + Explain into Query)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Consolidate the **Ask** and **Explain** action tabs into the **Query** panel: an NL "Ask" row (runs `gwClient.ask`, fills the editor, shows an answer bubble), the existing Query Helper, and paste — all feed one editor; an **Explain** tab on the results explains them. Remove the two now-redundant tabs/panels.

**Architecture:** Frontend-only, single-file `frontend/XGraph.html`. All new state is local to `QueryPanel` (each query tab is its own instance). Reuses `gwClient.ask` and `gwClient.explain` — no backend, no `gateway.js` change.

**Tech Stack:** React 18 UMD + Babel-standalone (no build step). Validation via the local esbuild JSX check; behavior is browser-driven (CLAUDE.md — React app not headlessly verifiable).

## Global Constraints

- **No `git commit` unless authorized** (CLAUDE.md); in a background job commits land on the worktree branch and are fast-forwarded onto `main`.
- **Anchor edits on verbatim strings** — `XGraph.html` is ~9,700 lines; **read each region immediately before editing** (line numbers drift as you edit).
- **Do NOT alter the query-run path** — `executeQuery`, `setSqlAndNotify`, the results table/graph/map tabs, and the Query Helper (`showHelper`/`generateQuery`) must keep working byte-for-byte except where this plan adds to them.
- **`HYDRATE_SOURCE`** is a module-level const already in scope in `QueryPanel` (used by the inlined Explain).
- **Every frontend edit validated by the esbuild JSX check** (below) → `ESBUILD_OK` before each commit; gateway `curl` 200 after wiring.
- **Version badge:** bump `EXPLORER_VERSION` `0.11.0` → `0.12.0` in the final task.
- Commit messages: concise 1–2 lines, no `Co-Authored-By` footer.

### Frontend esbuild JSX check

```bash
cd /home/kkaramete/xgraph/frontend
end=$(grep -n '</script>' XGraph.html | tail -1 | cut -d: -f1)
sed -n "47,$((end-1))p" XGraph.html | ./node_modules/.bin/esbuild --loader=jsx > /dev/null && echo ESBUILD_OK || echo ESBUILD_FAIL
```

---

## File Structure

- **Modify only:** `frontend/XGraph.html`
  - `QueryPanel` (~1578+): add Ask/Explain local state; add the Ask row JSX (above the Query Helper); add the Explain tab button + tab body (in the results tabs region ~3601+).
  - `ACTIONS` (~6373): remove `interact1` + `interact2` entries.
  - Render dispatch (~9643, ~9724): remove the `interact1` and `interact2` blocks; drop `askHistory`/`onRunInQuery` wiring.
  - `InteractAskPanel` (~7642–7876) and `InteractExplainPanel` (~7883–7990): delete.
  - `askHistory`/`setAskHistory` App state: delete.
  - `EXPLORER_VERSION` (L50): bump.

---

## Task 1: Ask row in QueryPanel (NL → ask → fill editor + result + answer bubble)

**Files:** Modify `frontend/XGraph.html` — `QueryPanel`.

**Interfaces:**
- Consumes: `props.gwClient.ask(graph, question)` → `{answer, cypher, columns, rows, graph}`; existing `setSqlAndNotify`, `setResult`, `props.onResultChange`, `props.graphName`.
- Produces: an NL Ask control that fills the editor + drives the existing result tabs + shows the answer bubble.

**Context:** `gwClient.ask` is the same one-shot Ask uses today (`InteractAskPanel`, ~7658). Its response shape matches a query result (columns/rows/graph), so `setResult(res)` makes the existing Results/Visualization tabs render it. Filling the editor via `setSqlAndNotify(res.cypher)` leaves an editable, re-runnable query so Explain (Task 2) can act on it.

- [ ] **Step 1: Read the QueryPanel state region + the top of its returned JSX**

```bash
cd /home/kkaramete/xgraph/frontend
grep -n "const \[sql, setSql\]\|const \[showHelper\|const \[activeTab\|function setSqlAndNotify\|{!hideQueryHelper && (" XGraph.html | head
```
Read ~10 lines around each hit to get verbatim anchors.

- [ ] **Step 2: Add Ask state**

Immediately after the `const [sql, setSql] = useState(initialSql);` line (~1613), add:

```javascript
    const [askQuestion, setAskQuestion] = useState('');
    const [askBusy, setAskBusy] = useState(false);
    const [askAnswer, setAskAnswer] = useState(null);
    const [askError, setAskError] = useState(null);
    var handleAsk = async function() {
        var q = askQuestion.trim();
        if (!q || !props.gwClient || !props.graphName) return;
        setAskBusy(true); setAskError(null); setAskAnswer(null);
        try {
            var res = await props.gwClient.ask(props.graphName, q);
            if (res && res.cypher) setSqlAndNotify(res.cypher);
            if (res && (res.columns || res.rows || res.graph)) {
                setResult(res);
                if (props.onResultChange) props.onResultChange(res);
                setActiveTab('results');
            }
            setAskAnswer((res && res.answer) || '(no answer)');
        } catch (err) { setAskError(err.message); }
        finally { setAskBusy(false); }
    };
```

(If `setResult`/`setActiveTab` are named differently, match the actual names found in Step 1. `result`/`setResult` and `activeTab`/`setActiveTab` already exist in QueryPanel.)

- [ ] **Step 3: Add the Ask row JSX above the Query Helper**

Anchor on the Query Helper open (`{!hideQueryHelper && (`, ~3484) and insert **before** it (inside the same scrollable content container):

```jsx
                {!hideQueryHelper && (
                    <div style={{ display:'flex', flexDirection:'column', gap:6, padding:'0 0 8px', borderBottom:'1px solid #f1f2f6', marginBottom:8 }}>
                        <label style={{ fontSize:12, fontWeight:700, color:'#0984e3' }}>Ask (natural language)</label>
                        <div style={{ display:'flex', gap:6, alignItems:'flex-start' }}>
                            <textarea value={askQuestion} onChange={function(e){ setAskQuestion(e.target.value); }}
                                onKeyDown={function(e){ if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); handleAsk(); } }}
                                placeholder="e.g. which parties sent the largest wires?  (fills the query below and runs it)"
                                spellCheck="false" rows={2}
                                style={{ flex:1, resize:'vertical', border:'1px solid #dfe6e9', borderRadius:6, padding:8, fontFamily:'inherit', fontSize:12, outline:'none' }}/>
                            <button onClick={handleAsk} disabled={askBusy || !askQuestion.trim()} style={{ padding:'8px 14px', border:'none', borderRadius:6, cursor: askBusy ? 'not-allowed' : 'pointer', fontWeight:700, color:'#fff', background: askBusy ? '#74b9ff' : '#0984e3', fontSize:12, fontFamily:'inherit', whiteSpace:'nowrap' }}>{askBusy ? 'Asking…' : 'Ask'}</button>
                        </div>
                        {askError && <span style={{ fontSize:11, color:'#d63031' }}>{askError}</span>}
                        {askAnswer && (
                            <div style={{ padding:'8px 12px', background:'#eef7ff', border:'1px solid #d0e6ff', borderRadius:6, fontSize:12, color:'#2d3436', position:'relative' }}>
                                <span style={{ position:'absolute', top:4, right:8, cursor:'pointer', color:'#b2bec3', fontSize:13 }} title="Dismiss" onClick={function(){ setAskAnswer(null); }}>{'✕'}</span>
                                {askAnswer}
                            </div>
                        )}
                    </div>
                )}
                {!hideQueryHelper && (
```

(This adds a sibling block right before the existing `{!hideQueryHelper && (` Query-Helper block — the Ask row and Helper are both gated on `!hideQueryHelper` so they only show in the main Query tab, not in table-preview embeds.)

- [ ] **Step 4: Clear the answer bubble on a manual run**

In `executeQuery` (~2576), after `setCreateSuccess(null);` (or wherever it resets per-run state), add `setAskAnswer(null);` so a fresh Run clears a stale Ask answer. (Read the region first; match the existing reset line.)

- [ ] **Step 5: esbuild check**

Run the esbuild JSX check → `ESBUILD_OK`.

- [ ] **Step 6: Commit**

```bash
cd /home/kkaramete/xgraph
git add frontend/XGraph.html
git commit -m "feat(query): in-panel Ask row (NL -> ask, fills editor + results + answer bubble)"
```

---

## Task 2: Explain tab in QueryPanel

**Files:** Modify `frontend/XGraph.html` — `QueryPanel`.

**Interfaces:**
- Consumes: `props.gwClient.explain(question, columns, rows, cypher, source, graph)`; existing `result`, `sql`, `activeTab`/`setActiveTab`, `HYDRATE_SOURCE`, `window.xgraphGateway.tableFromGateway`.
- Produces: an `activeTab === 'explain'` tab (button appears when a result exists) with a focus input + Explain button + answer/post-join-SQL/hydrated-table output.

**Context:** Inlines `InteractExplainPanel`'s logic (~7893–7986). The `explain` response is `{answer, join_sql, columns, rows, hydrated}`; render the hydrated table with `tableFromGateway`.

- [ ] **Step 1: Read the results-tabs button row + a results tab body for anchors**

```bash
cd /home/kkaramete/xgraph/frontend
grep -n "View Results\|activeTab === 'results'\|activeTab === 'graph'\|setActiveTab(" XGraph.html | head
```
Read ~15 lines around the tab-buttons row and one tab body.

- [ ] **Step 2: Add Explain state**

Near the Ask state (Task 1), add:

```javascript
    const [explainFocus, setExplainFocus] = useState('');
    const [explainBusy, setExplainBusy] = useState(false);
    const [explainResp, setExplainResp] = useState(null);
    const [explainError, setExplainError] = useState(null);
    var handleExplain = async function() {
        if (!props.gwClient || !props.graphName) return;
        var cols = (result && result.columns) || [];
        var rws = (result && result.rows) || [];
        setExplainBusy(true); setExplainError(null); setExplainResp(null);
        try {
            var r = await props.gwClient.explain(
                explainFocus.trim() || 'Explain these results',
                cols, rws, sql, HYDRATE_SOURCE, props.graphName);
            setExplainResp(r);
        } catch (err) { setExplainError(err.message); }
        finally { setExplainBusy(false); }
    };
```

- [ ] **Step 3: Add the Explain tab button**

In the results-tabs button row (~3601–3609), after the existing tab buttons, add an Explain button (only meaningful once a result exists — the whole row already renders after results):

```jsx
                        <button onClick={function(){ setActiveTab('explain'); }} style={tabBtnStyle(activeTab === 'explain')}>Explain</button>
```

(Match the existing tab buttons' styling helper/pattern found in Step 1 — reuse whatever style expression the sibling buttons use, e.g. an inline style keyed on `activeTab === 'graph'`.)

- [ ] **Step 4: Add the Explain tab body**

After the last results tab body (e.g. after the `activeTab === 'map'`/`'graph'` block), add:

```jsx
                        {activeTab === 'explain' && (
                            <div style={{ flex:1, minHeight:0, overflowY:'auto', padding:'8px 0' }}>
                                <div style={{ display:'flex', gap:6, alignItems:'center', marginBottom:8 }}>
                                    <input value={explainFocus} onChange={function(e){ setExplainFocus(e.target.value); }}
                                        onKeyDown={function(e){ if (e.key === 'Enter') { e.preventDefault(); handleExplain(); } }}
                                        placeholder="What to focus on (optional)"
                                        style={{ flex:1, border:'1px solid #dfe6e9', borderRadius:6, padding:'6px 8px', fontFamily:'inherit', fontSize:12, outline:'none' }}/>
                                    <button onClick={handleExplain} disabled={explainBusy || !result} style={{ padding:'6px 14px', border:'none', borderRadius:6, cursor:(explainBusy||!result)?'not-allowed':'pointer', fontWeight:700, color:'#fff', background:(explainBusy||!result)?'#b2bec3':'#6c5ce7', fontSize:12, fontFamily:'inherit', whiteSpace:'nowrap' }}>{explainBusy ? 'Explaining…' : 'Explain in plain English'}</button>
                                </div>
                                {!result && <p style={{ fontSize:12, color:'#b2bec3' }}>Run a query (or Ask) first, then Explain its results.</p>}
                                {explainError && <p style={{ fontSize:12, color:'#d63031' }}>{explainError}</p>}
                                {explainResp && explainResp.hydrated && explainResp.join_sql && (
                                    <div style={{ marginBottom:8 }}>
                                        <label style={{ fontSize:11, fontWeight:700, color:'#636e72' }}>Post-join SQL</label>
                                        <pre style={{ margin:'2px 0', padding:8, background:'#f8fafc', border:'1px solid #eef1f4', borderRadius:6, fontSize:11, whiteSpace:'pre-wrap', overflowX:'auto' }}>{explainResp.join_sql}</pre>
                                    </div>
                                )}
                                {explainResp && explainResp.hydrated && (function(){
                                    var t = window.xgraphGateway.tableFromGateway(explainResp);
                                    return (
                                        <div style={{ overflowX:'auto', marginBottom:8 }}>
                                            <table style={{ borderCollapse:'collapse', fontSize:11, width:'100%' }}>
                                                <thead><tr>{(t.headers||[]).map(function(h){ return <th key={h} style={{ textAlign:'left', padding:'4px 8px', borderBottom:'1px solid #dfe6e9', color:'#636e72' }}>{h}</th>; })}</tr></thead>
                                                <tbody>{(t.rows||[]).map(function(row, ri){ return <tr key={ri}>{(t.headers||[]).map(function(h){ return <td key={h} style={{ padding:'4px 8px', borderBottom:'1px solid #f1f2f6' }}>{String(row[h] === undefined || row[h] === null ? '' : row[h])}</td>; })}</tr>; })}</tbody>
                                            </table>
                                        </div>
                                    );
                                })()}
                                {explainResp && explainResp.answer && (
                                    <div style={{ padding:'8px 12px', background:'#f3f0ff', border:'1px solid #e0d8ff', borderRadius:6, fontSize:12, color:'#2d3436' }}>{explainResp.answer}</div>
                                )}
                            </div>
                        )}
```

(If `tableFromGateway`'s output shape differs from `{headers, rows}`, match how the existing Results tab renders `result` — reuse that exact rendering rather than this generic table.)

- [ ] **Step 5: esbuild check**

Run the esbuild JSX check → `ESBUILD_OK`.

- [ ] **Step 6: Commit**

```bash
cd /home/kkaramete/xgraph
git add frontend/XGraph.html
git commit -m "feat(query): Explain tab in QueryPanel (focus + explain over the current results)"
```

---

## Task 3: Remove Ask/Explain tabs + panels + state; version bump

**Files:** Modify `frontend/XGraph.html`.

**Interfaces:** Consumes nothing new. Produces the final action bar `Setup · Build · List · Query · Visualize · Ontology · Storage`.

**Context:** `InteractAskPanel`/`InteractExplainPanel` are now fully replaced by QueryPanel's Ask row + Explain tab. Removing them, their ACTIONS entries, render blocks, and the `askHistory` state they used cannot affect Query/Visualize/etc. Verify no other references remain.

- [ ] **Step 1: Remove the ACTIONS entries**

Delete the `{ key: 'interact1', label: 'Ask', … }` and `{ key: 'interact2', label: 'Explain', … }` lines from `ACTIONS` (~6373). Leave `query` and all others.

- [ ] **Step 2: Remove the render-dispatch blocks**

Delete the `{activeAction === 'interact1' && ( <InteractAskPanel … /> )}` block (~9643–9650) and the `{activeAction === 'interact2' && ( <InteractExplainPanel … /> )}` block (~9724–9729).

- [ ] **Step 3: Delete the two components**

Delete `function InteractAskPanel(props) { … }` (~7642–7876) and `function InteractExplainPanel(props) { … }` (~7883–7990), plus their banner comments.

- [ ] **Step 4: Remove `askHistory` App state + the `onRunInQuery` handoff**

Delete `const [askHistory, setAskHistory] = useState(...)` (~8157) and any remaining references (the `onRunInQuery` closure lived only in the removed interact1 block). Verify:

```bash
cd /home/kkaramete/xgraph/frontend
grep -n "askHistory\|InteractAskPanel\|InteractExplainPanel\|onRunInQuery\|interact1\|interact2" XGraph.html || echo "NO_STALE_ASK_EXPLAIN_REFS"
```
Expected: `NO_STALE_ASK_EXPLAIN_REFS`.

- [ ] **Step 5: Bump the version badge**

`EXPLORER_VERSION` (L50) `"0.11.0"` → `"0.12.0"`.

- [ ] **Step 6: esbuild + gateway 200**

```bash
cd /home/kkaramete/xgraph/frontend
end=$(grep -n '</script>' XGraph.html | tail -1 | cut -d: -f1)
sed -n "47,$((end-1))p" XGraph.html | ./node_modules/.bin/esbuild --loader=jsx > /dev/null && echo ESBUILD_OK || echo ESBUILD_FAIL
cd /home/kkaramete/xgraph && (./xgraph status >/dev/null 2>&1 || ./xgraph start) && sleep 1 && curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8090/
```
Expected: `ESBUILD_OK` then `200`.

- [ ] **Step 7: Commit**

```bash
cd /home/kkaramete/xgraph
git add frontend/XGraph.html
git commit -m "feat(query): fold Ask+Explain into Query; remove the two tabs; v0.12.0"
```

---

## Manual (browser) acceptance — run after Task 3

Hard-reload, confirm `v0.12.0`, connect + select a graph, open **Query**:

1. Action bar shows no separate **Ask**/**Explain** — just **Query** (bar: Setup · Build · List · Query · Visualize · Ontology · Storage).
2. **Ask row:** type an NL question → **Ask** → the editor fills with the generated Cypher, results render in **View Results**/**Visualization**, and the answer bubble shows above the tabs.
3. **Query Helper:** build a query → **Generate Query** still fills the editor (unchanged).
4. **Paste:** type/paste Cypher → **Run** works (unchanged).
5. **Explain tab:** after any of the above, the **Explain** tab appears → enter a focus (optional) → **Explain** → answer + post-join SQL + hydrated table render.
6. Query tabs (`+`/close) still work; each tab keeps its own Ask/Explain state.

---

## Self-Review

- **Spec coverage:** Ask-runs-fully-and-fills-editor (Task 1); Explain-as-a-tab (Task 2); remove the two tabs/panels + `askHistory`, keep Storage (Task 3). No backend / `gateway.js` change. Matches the approved design.
- **Placeholder scan:** the JSX steps give complete code; the two "match the existing pattern" notes (tab-button style, results-table rendering) are explicit instructions to reuse verbatim sibling code found in the read-first steps — not TODOs.
- **Type/name consistency:** `handleAsk` uses `setSqlAndNotify`/`setResult`/`setActiveTab`/`props.onResultChange` (all existing in QueryPanel); `handleExplain` uses `result`/`sql`/`HYDRATE_SOURCE`/`props.graphName` and `gwClient.explain(question, columns, rows, cypher, source, graph)` (matches gateway.js). Explain tab value `'explain'` is new alongside `results`/`graph`/`map`.
- **Scope:** Query consolidation only. Table Views, Ontology-under-Visualize, Storage folding are deferred.
- **Risk sequencing:** additive first (Ask row, Explain tab — Query keeps working with the old tabs still present), removal last (Task 3), so a half-done state still runs. The query-run path and Query Helper are untouched.
