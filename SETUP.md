# xGraph — Setup Guide

This is the practical "fresh checkout → running" guide. For what the project *is*,
see [`README.md`](README.md).

xGraph is a **FastAPI/Python gateway** (`backend/`) that also serves a single-file
React UI (`frontend/XGraph.html`). The gateway talks to external graph engines
(**FalkorDB**, **Kinetica**) and an **LLM** (Claude, via Vertex / API key / the
`claude` CLI). Those are **not** bundled — this guide covers wiring them up.

---

## 1. Happy path (viz + query on FalkorDB)

This gets the FalkorDB + DuckDB path working — Connect · List · Load · Query ·
Visualize · Explain post-join. LLM and Kinetica are separate (sections 3 & 4).

```bash
git clone git@github-personal:karametebkaan/xgraph.git
cd xgraph

# a) Extract the demo Parquet from the tracked .zip archives (needs `unzip`)
./scripts/unzip-data.sh

# b) Backend virtualenv (standalone — includes gpudb, a hard import even for
#    FalkorDB-only use)
cd backend && python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
cd ..

# c) Start the gateway (serves the UI too) and open it
./xgraph start          # also: ./xgraph stop | status | restart | logs
# → http://localhost:8090/
```

The frontend is loaded from CDN (React + Babel), so there is **no `npm`/build
step** and no `node_modules` needed to run the app.

> You still need a reachable **FalkorDB** before Connect works — see section 2.

### Prerequisites

- **Python 3** (with `venv`).
- **`unzip`** on PATH (used by `scripts/unzip-data.sh`).
- **Port 8090 free** (8088 is assumed taken by Kinetica Graph). Override with
  `XGRAPH_PORT`. `./xgraph start` refuses if the port is busy and tells you.
- A running **FalkorDB** (section 2). Kinetica (section 4) and the LLM
  (section 3) are optional depending on which features you use.

---

## 2. FalkorDB setup

FalkorDB is the S1 reference engine. A clean machine has none, so **Connect fails
until one is running.** The quickest way is Docker:

```bash
docker run -d --name falkordb -p 6379:6379 falkordb/falkordb:latest
```

Connection defaults (host `localhost`, port `6379`, no password) live in
`backend/.env.example`. To change them, copy that file and edit:

```bash
cp backend/.env.example backend/.env
# edit FALKORDB_HOST / FALKORDB_PORT / FALKORDB_PASSWORD as needed
```

The gateway loads `backend/.env` at startup, so you don't need to export these in
your shell.

### Point the UI at your engine

In the app's **Setup** panel, pick **FalkorDB** as the graph engine and
**DuckDB** for OLAP/ingest, then **Connect**. The gateway URL defaults to
`http://localhost:8090`. Hydrate/Create use the demo Parquet you extracted in
step 1a (`HYDRATE_SOURCE = vertexes.parquet`, resolved server-side against
`XGRAPH_DATA_DIR`, which defaults to the repo's `data/`).

---

## 3. LLM setup (Ask / Explain / Extract)

The LLM-backed panels (Ask, Explain, Extract) need a working Claude route. This is
**not shipped** — `.env` files are git-ignored, so a fresh clone has nothing
configured. Viz and query work without it; only the LLM features glitch until you
set one of the three routes below.

Start from the template:

```bash
cp backend/.env.example backend/.env      # if you haven't already
```

Then pick **one** of these auth routes and fill the matching vars in
`backend/.env`:

### Option A — GCP Vertex (recommended default)

```bash
# One-time on the gateway host: server-side GCP Application Default Credentials
gcloud auth application-default login
```

In `backend/.env`:

```ini
CLAUDE_CODE_USE_VERTEX=1
ANTHROPIC_VERTEX_PROJECT_ID=<your-gcp-project>
CLOUD_ML_REGION=global
ANTHROPIC_DEFAULT_OPUS_MODEL=claude-opus-4-8
```

For the faster **in-process SDK** mechanism (~3s Ask/Explain vs ~13s on the CLI),
also install the Vertex extra and set the mechanism:

```bash
cd backend && ./.venv/bin/pip install -r requirements-vertex.txt   # anthropic[vertex]
```

```ini
XGRAPH_LLM_MECHANISM=sdk
```

### Option B — Direct Anthropic API key

In `backend/.env` (leave the Vertex vars unset):

```ini
ANTHROPIC_API_KEY=sk-ant-...
```

### Option C — `claude` CLI

Have the [`claude` CLI](https://claude.com/claude-code) installed on PATH and
logged in; the default mechanism (`cli`) spawns it per call. No extra env needed.

### Notes

- The route can also be changed live in the UI **Setup** panel (Mechanism × Auth
  picker); the footer status bar shows the active route and both model tiers.
- Two model tiers: **Build** (extract/fold) runs on Opus; **Ask/Explain** run on
  the fast Haiku tier. Override with `XGRAPH_EXTRACT_MODEL` / `XGRAPH_LLM_MODEL`.
- On startup the gateway fires a tiny warmup call so the first Ask/Explain is
  fast. A **failed LLM route only logs — it does not block startup**, so the app
  still comes up for viz/query. Disable warmup with `XGRAPH_LLM_WARMUP=0`.

---

## 4. Kinetica setup (optional — validation route)

Kinetica is a first-class validation engine but optional. If you have one
reachable, set in `backend/.env`:

```ini
KINETICA_URL=http://127.0.0.1:9191
KINETICA_USER=admin
KINETICA_PASS=
```

Then pick **Kinetica** as the graph engine in Setup. Kinetica-only UI (grammar
helpers, geo/WMS) appears only when the *graph* engine is Kinetica.

---

## 5. Verifying the install

```bash
# Backend tests (own venv; from backend/). Live engine tests SKIP if the engine
# is unreachable — they do not fail.
cd backend && ./.venv/bin/python -m pytest tests/ -q

# Frontend pure-JS unit tests (gateway client + transforms; no browser needed)
cd ../frontend && node tests/test_transforms.mjs && node tests/test_client.mjs
```

The React app itself can't be verified headlessly — real acceptance is
browser-driven (open `http://localhost:8090/` and reload).

---

## Troubleshooting quick reference

| Symptom | Cause / fix |
|---|---|
| `Connect` fails | No FalkorDB/Kinetica reachable — start one (section 2). |
| Ask/Explain/Extract error | LLM route not configured — set one in `backend/.env` (section 3). |
| `unzipping … missing archive` | `unzip` not installed, or run from the wrong dir — run `./scripts/unzip-data.sh` from the repo root. |
| Gateway won't start, `no venv` | Create it: `cd backend && python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt`. |
| `port 8090 already in use` | Another process (or a stale gateway) — `./xgraph stop`, or set `XGRAPH_PORT`. |
| Import error on `gpudb` | The venv wasn't created/installed — `gpudb` is a hard import even for FalkorDB-only use. |
| Hydrate/Create find no data | Demo Parquet not extracted — run `./scripts/unzip-data.sh`. |
