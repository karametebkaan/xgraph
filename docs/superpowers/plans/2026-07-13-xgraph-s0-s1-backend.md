# xgraph S0+S1 Backend (gateway) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `xgraph` FastAPI gateway with three contracts (GraphEngineAdapter, ComputeEngine, SourceReader), a working FalkorDB adapter, and a DuckDB hydration pass — exercisable end-to-end over HTTP against the live FalkorDB `banking_graph`, with no frontend and no Kinetica.

**Architecture:** A FastAPI app exposes xgraph's own uniform HTTP API. Requests carry an `engine` selector; the app routes to a `GraphEngineAdapter` implementation (FalkorDB for S1). A single embedded DuckDB `ComputeEngine` performs post-traversal hydration. The `falkor` project's `graph_loader` package is reused as a library (hydration + DuckDB source) rather than reimplemented.

**Tech Stack:** Python 3.10+, FastAPI, uvicorn, `falkordb` client, `duckdb`, `python-dotenv`, `pytest`, `httpx` (TestClient). Reuses `graph_loader` from the sibling `falkor` project.

## Global Constraints

- Python **3.10+**.
- **Do NOT `git commit` anything under `xgraph/`** — develop locally only until explicitly told otherwise. Every "checkpoint" step below means *save/verify locally, do not commit*.
- Reuse the sibling `falkor` project's `graph_loader` package as a library (via `XGRAPH_FALKOR_PATH`, default `../falkor`, inserted on `sys.path`). Do not copy its code.
- Config comes from `xgraph/backend/.env` (same pattern as `falkor`): `FALKORDB_HOST` (default `localhost`), `FALKORDB_PORT` (default `6379`), `FALKORDB_PASSWORD`, `XGRAPH_FALKOR_PATH` (default `../falkor`), and Kinetica: `KINETICA_URL`, `KINETICA_USER`, `KINETICA_PASS`.
- **Kinetica is a first-class validation route from S1** (not deferred to S4). It is registered as a switchable `engine=kinetica` so the FalkorDB/DuckDB pipeline can be validated against Kinetica ground truth at every step (mirrors the `falkor` build-both-and-compare verification). For this backend slice the Kinetica adapter implements `list_graphs` + `run_query` fully (the validation path); `fetch_entities`/`get_record` raise a clear "S4" `NotImplementedError` and `get_schema` is best-effort (rich Kinetica ontology DOT stays S4).
- Numeric values returned from DuckDB MUST be coerced `Decimal → float` (reuse `graph_loader.duckdb_source.coerce_row`).
- Integration tests that need live FalkorDB MUST **skip** (not fail) when it is unreachable.
- Uniform error envelope for all HTTP errors: `{"error": {"code", "message", "engine", "detail"}}`.
- All graph queries pass a `timeout` (ms) to the FalkorDB client; default 60000.

---

### Task 1: Backend scaffold + config

**Files:**
- Create: `xgraph/backend/xgraph_gateway/__init__.py`
- Create: `xgraph/backend/xgraph_gateway/config.py`
- Create: `xgraph/backend/requirements.txt`
- Create: `xgraph/backend/.env.example`
- Create: `xgraph/backend/conftest.py`
- Test: `xgraph/backend/tests/test_config.py`

**Interfaces:**
- Produces: `config.load_settings() -> Settings` dataclass with fields `falkordb_host: str`, `falkordb_port: int`, `falkordb_password: str | None`, `falkor_path: str`. `config.ensure_falkor_on_path(settings) -> None` inserts `settings.falkor_path` (resolved absolute) at `sys.path[0]` if not already present.

- [ ] **Step 1: Write requirements.txt**

```
fastapi
uvicorn
duckdb
falkordb
python-dotenv
pytest
httpx
```

- [ ] **Step 2: Write .env.example**

```
FALKORDB_HOST=localhost
FALKORDB_PORT=6379
FALKORDB_PASSWORD=
XGRAPH_FALKOR_PATH=../falkor
KINETICA_URL=http://127.0.0.1:9191
KINETICA_USER=admin
KINETICA_PASS=
```

- [ ] **Step 3: Write the failing test**

```python
# xgraph/backend/tests/test_config.py
import os
from xgraph_gateway import config

def test_load_settings_reads_env(monkeypatch):
    monkeypatch.setenv("FALKORDB_HOST", "h")
    monkeypatch.setenv("FALKORDB_PORT", "7000")
    monkeypatch.setenv("FALKORDB_PASSWORD", "pw")
    monkeypatch.setenv("XGRAPH_FALKOR_PATH", "/tmp/falkor")
    s = config.load_settings()
    assert (s.falkordb_host, s.falkordb_port, s.falkordb_password) == ("h", 7000, "pw")
    assert s.falkor_path == "/tmp/falkor"

def test_load_settings_defaults(monkeypatch):
    for k in ("FALKORDB_HOST", "FALKORDB_PORT", "XGRAPH_FALKOR_PATH"):
        monkeypatch.delenv(k, raising=False)
    s = config.load_settings()
    assert s.falkordb_host == "localhost"
    assert s.falkordb_port == 6379
    assert s.falkor_path == "../falkor"
```

- [ ] **Step 4: Run test to verify it fails**

Run: `cd xgraph/backend && python -m pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'xgraph_gateway'`

- [ ] **Step 5: Write config.py and __init__.py**

```python
# xgraph/backend/xgraph_gateway/__init__.py
```
```python
# xgraph/backend/xgraph_gateway/config.py
from __future__ import annotations
import os, sys
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass
class Settings:
    falkordb_host: str
    falkordb_port: int
    falkordb_password: str | None
    falkor_path: str
    kinetica_url: str | None = None
    kinetica_user: str | None = None
    kinetica_pass: str | None = None

def load_settings() -> Settings:
    return Settings(
        falkordb_host=os.environ.get("FALKORDB_HOST", "localhost"),
        falkordb_port=int(os.environ.get("FALKORDB_PORT", "6379")),
        falkordb_password=os.environ.get("FALKORDB_PASSWORD"),
        falkor_path=os.environ.get("XGRAPH_FALKOR_PATH", "../falkor"),
        kinetica_url=os.environ.get("KINETICA_URL"),
        kinetica_user=os.environ.get("KINETICA_USER"),
        kinetica_pass=os.environ.get("KINETICA_PASS"),
    )

def ensure_falkor_on_path(settings: Settings) -> None:
    p = os.path.abspath(settings.falkor_path)
    if p not in sys.path:
        sys.path.insert(0, p)
```

- [ ] **Step 6: Write conftest.py so tests import the package**

```python
# xgraph/backend/conftest.py
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
```

- [ ] **Step 7: Run test to verify it passes**

Run: `cd xgraph/backend && python -m pytest tests/test_config.py -v`
Expected: PASS (2 passed)

- [ ] **Step 8: Checkpoint (no commit — local only per Global Constraints)**

Run: `cd xgraph/backend && python -c "from xgraph_gateway import config; print(config.load_settings())"`
Expected: prints a `Settings(...)` line.

---

### Task 2: GraphEngineAdapter contract + fake adapter

**Files:**
- Create: `xgraph/backend/xgraph_gateway/adapters/__init__.py`
- Create: `xgraph/backend/xgraph_gateway/adapters/base.py`
- Create: `xgraph/backend/xgraph_gateway/adapters/fake.py`
- Test: `xgraph/backend/tests/test_fake_adapter.py`

**Interfaces:**
- Produces: `base.GraphEngineAdapter` ABC with methods `list_graphs() -> list[str]`, `get_schema(graph: str) -> dict` (`{"labels": list[str], "rel_types": list[str], "dot": str}`), `run_query(graph: str, cypher: str, timeout: int = 60000) -> dict` (`{"columns": list[str], "rows": list[list]}`), `fetch_entities(graph: str, limit: int) -> dict` (`{"nodes": [{"id","label","props"}], "edges": [{"id","source","target","type"}]}`), `get_record(graph: str, node_id: str) -> dict`.
- Produces: `fake.FakeAdapter` implementing all methods from in-memory data, for gateway tests.

- [ ] **Step 1: Write the failing test**

```python
# xgraph/backend/tests/test_fake_adapter.py
from xgraph_gateway.adapters.fake import FakeAdapter
from xgraph_gateway.adapters.base import GraphEngineAdapter

def test_fake_is_adapter():
    assert isinstance(FakeAdapter(), GraphEngineAdapter)

def test_fake_query_and_schema():
    a = FakeAdapter()
    assert a.list_graphs() == ["demo_graph"]
    q = a.run_query("demo_graph", "MATCH (n) RETURN n.NODE AS NODE")
    assert q["columns"] == ["NODE"]
    assert ["b1"] in q["rows"]
    sch = a.get_schema("demo_graph")
    assert "bank" in sch["labels"]
    assert sch["dot"].startswith("digraph")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd xgraph/backend && python -m pytest tests/test_fake_adapter.py -v`
Expected: FAIL with `ModuleNotFoundError` for `xgraph_gateway.adapters.fake`

- [ ] **Step 3: Write base.py**

```python
# xgraph/backend/xgraph_gateway/adapters/base.py
from __future__ import annotations
from abc import ABC, abstractmethod

class GraphEngineAdapter(ABC):
    @abstractmethod
    def list_graphs(self) -> list[str]: ...
    @abstractmethod
    def get_schema(self, graph: str) -> dict: ...
    @abstractmethod
    def run_query(self, graph: str, cypher: str, timeout: int = 60000) -> dict: ...
    @abstractmethod
    def fetch_entities(self, graph: str, limit: int) -> dict: ...
    @abstractmethod
    def get_record(self, graph: str, node_id: str) -> dict: ...
```

- [ ] **Step 4: Write fake.py**

```python
# xgraph/backend/xgraph_gateway/adapters/fake.py
from __future__ import annotations
from .base import GraphEngineAdapter

_NODES = [{"id": "b1", "label": "bank", "props": {"bank_name": "Acme"}},
          {"id": "w1", "label": "wire_message", "props": {"risk": 90}}]
_EDGES = [{"id": "e1", "source": "b1", "target": "w1", "type": "performed"}]

class FakeAdapter(GraphEngineAdapter):
    def list_graphs(self):
        return ["demo_graph"]
    def get_schema(self, graph):
        return {"labels": ["bank", "wire_message"], "rel_types": ["performed"],
                "dot": 'digraph { "bank" -> "wire_message" [label="performed"]; }'}
    def run_query(self, graph, cypher, timeout=60000):
        return {"columns": ["NODE"], "rows": [[n["id"]] for n in _NODES]}
    def fetch_entities(self, graph, limit):
        return {"nodes": _NODES[:limit], "edges": _EDGES[:limit]}
    def get_record(self, graph, node_id):
        for n in _NODES:
            if n["id"] == node_id:
                return n
        return {}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd xgraph/backend && python -m pytest tests/test_fake_adapter.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: Checkpoint (no commit)** — `python -m pytest tests/ -q` shows all green.

---

### Task 3: FalkorDB adapter

**Files:**
- Create: `xgraph/backend/xgraph_gateway/adapters/falkordb_adapter.py`
- Test: `xgraph/backend/tests/test_falkordb_adapter.py`

**Interfaces:**
- Consumes: `config.Settings`, `base.GraphEngineAdapter`.
- Produces: `falkordb_adapter.FalkorDBAdapter(settings)` implementing the contract against a live FalkorDB. `run_query` returns `{"columns", "rows"}` from `QueryResult.header` (`[type, name]` pairs; names may be bytes) and `.result_set`. `get_schema` builds a DOT of distinct `(a.LABEL, type(r), b.LABEL)` triples. `_column_names(qr) -> list[str]` helper decodes header names.

- [ ] **Step 1: Write the failing unit test (header parsing, no service)**

```python
# xgraph/backend/tests/test_falkordb_adapter.py
import pytest
from xgraph_gateway.adapters.falkordb_adapter import _column_names, _dot_from_triples

def test_column_names_decodes_header():
    header = [[1, b"NODE"], [1, "risk"]]
    assert _column_names(header) == ["NODE", "risk"]

def test_dot_from_triples():
    dot = _dot_from_triples([("bank", "performed", "wire_message")])
    assert dot.startswith("digraph")
    assert '"bank" -> "wire_message"' in dot
    assert 'label="performed"' in dot
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd xgraph/backend && python -m pytest tests/test_falkordb_adapter.py -v`
Expected: FAIL with `ModuleNotFoundError` for the adapter module.

- [ ] **Step 3: Write falkordb_adapter.py**

```python
# xgraph/backend/xgraph_gateway/adapters/falkordb_adapter.py
from __future__ import annotations
from falkordb import FalkorDB
from .base import GraphEngineAdapter

def _column_names(header) -> list[str]:
    names = []
    for col in header:
        name = col[1] if isinstance(col, (list, tuple)) and len(col) > 1 else col
        names.append(name.decode() if isinstance(name, bytes) else name)
    return names

def _dot_from_triples(triples) -> str:
    lines = ["digraph {"]
    for src, rel, dst in triples:
        lines.append(f'  "{src}" -> "{dst}" [label="{rel}"];')
    lines.append("}")
    return "\n".join(lines)

class FalkorDBAdapter(GraphEngineAdapter):
    def __init__(self, settings):
        self._db = FalkorDB(host=settings.falkordb_host, port=settings.falkordb_port,
                            password=settings.falkordb_password)

    def _graph(self, graph):
        return self._db.select_graph(graph)

    def list_graphs(self):
        return list(self._db.list_graphs())

    def run_query(self, graph, cypher, timeout=60000):
        qr = self._graph(graph).query(cypher, timeout=timeout)
        return {"columns": _column_names(qr.header), "rows": qr.result_set}

    def get_schema(self, graph):
        g = self._graph(graph)
        labels = [r[0] for r in g.query("MATCH (n) RETURN DISTINCT n.LABEL", timeout=60000).result_set if r[0]]
        rels = [r[0] for r in g.query("MATCH ()-[r]->() RETURN DISTINCT type(r)", timeout=60000).result_set if r[0]]
        triples = [(r[0], r[1], r[2]) for r in g.query(
            "MATCH (a)-[r]->(b) RETURN DISTINCT a.LABEL, type(r), b.LABEL", timeout=60000).result_set
            if r[0] and r[2]]
        return {"labels": labels, "rel_types": rels, "dot": _dot_from_triples(triples)}

    def fetch_entities(self, graph, limit):
        g = self._graph(graph)
        nodes = [{"id": r[0], "label": r[1], "props": r[2]} for r in g.query(
            "MATCH (n) RETURN n.NODE, n.LABEL, properties(n) LIMIT $l",
            {"l": limit}, timeout=60000).result_set]
        edges = [{"id": r[0], "source": r[1], "target": r[2], "type": r[3]} for r in g.query(
            "MATCH (a)-[r]->(b) RETURN r.ID, a.NODE, b.NODE, type(r) LIMIT $l",
            {"l": limit}, timeout=60000).result_set]
        return {"nodes": nodes, "edges": edges}

    def get_record(self, graph, node_id):
        rs = self._graph(graph).query(
            "MATCH (n {NODE:$id}) RETURN n.NODE, n.LABEL, properties(n)",
            {"id": node_id}, timeout=60000).result_set
        if not rs:
            return {}
        return {"id": rs[0][0], "label": rs[0][1], "props": rs[0][2]}
```

- [ ] **Step 4: Run unit test to verify it passes**

Run: `cd xgraph/backend && python -m pytest tests/test_falkordb_adapter.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Add live integration test (skips if FalkorDB down)**

```python
# append to tests/test_falkordb_adapter.py
from xgraph_gateway import config
from xgraph_gateway.adapters.falkordb_adapter import FalkorDBAdapter

def _adapter_or_skip():
    try:
        a = FalkorDBAdapter(config.load_settings())
        a.list_graphs()
        return a
    except Exception as e:
        pytest.skip(f"FalkorDB unreachable: {e}")

def test_live_banking_graph_query():
    a = _adapter_or_skip()
    if "banking_graph" not in a.list_graphs():
        pytest.skip("banking_graph not loaded")
    out = a.run_query("banking_graph", "MATCH (b:bank) RETURN b.NODE AS NODE LIMIT 3")
    assert out["columns"] == ["NODE"]
    assert len(out["rows"]) == 3

def test_live_schema_has_bank_label():
    a = _adapter_or_skip()
    if "banking_graph" not in a.list_graphs():
        pytest.skip("banking_graph not loaded")
    sch = a.get_schema("banking_graph")
    assert "bank" in sch["labels"]
    assert sch["dot"].startswith("digraph")
```

- [ ] **Step 6: Run integration test against live FalkorDB**

Run: `cd xgraph/backend && python -m pytest tests/test_falkordb_adapter.py -v`
Expected: PASS or SKIP for the two `test_live_*` (PASS when FalkorDB + `banking_graph` are up).

- [ ] **Step 7: Checkpoint (no commit)**

---

### Task 3B: Kinetica adapter (validation baseline)

**Files:**
- Create: `xgraph/backend/xgraph_gateway/adapters/kinetica_adapter.py`
- Test: `xgraph/backend/tests/test_kinetica_adapter.py`

**Interfaces:**
- Consumes: `config.Settings`, `graph_loader.kinetica_source.KineticaSource` from `falkor`, `gpudb.GPUdb`.
- Produces: `kinetica_adapter.KineticaAdapter(settings)` implementing the contract. `list_graphs()` via `GPUdb.show_graph(graph_name='')`; `run_query(graph, query, timeout)` runs the query through `KineticaSource.rows` (paged, decoded) and returns `{"columns", "rows"}`. `get_schema` best-effort (labels from `show_graph`, minimal DOT). `fetch_entities`/`get_record` raise `NotImplementedError` (S4). The `graph`/`query` here are Kinetica SQL/GQL — validation uses engine-appropriate queries, not identical Cypher.

- [ ] **Step 1: Write the failing unit test (row-shaping helper, no service)**

```python
# xgraph/backend/tests/test_kinetica_adapter.py
import pytest
from xgraph_gateway.adapters.kinetica_adapter import _rows_to_result

def test_rows_to_result_shapes_columns_and_rows():
    rows = [{"NODE": "b1", "risk": 90}, {"NODE": "b2", "risk": 40}]
    assert _rows_to_result(rows) == {"columns": ["NODE", "risk"], "rows": [["b1", 90], ["b2", 40]]}

def test_rows_to_result_empty():
    assert _rows_to_result([]) == {"columns": [], "rows": []}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd xgraph/backend && python -m pytest tests/test_kinetica_adapter.py -v`
Expected: FAIL with `ModuleNotFoundError` for the kinetica adapter module.

- [ ] **Step 3: Write kinetica_adapter.py**

```python
# xgraph/backend/xgraph_gateway/adapters/kinetica_adapter.py
from __future__ import annotations
from gpudb import GPUdb
from xgraph_gateway import config
from .base import GraphEngineAdapter

config.ensure_falkor_on_path(config.load_settings())
from graph_loader.kinetica_source import KineticaSource   # noqa: E402

def _rows_to_result(rows: list[dict]) -> dict:
    cols = list(rows[0].keys()) if rows else []
    return {"columns": cols, "rows": [list(r.values()) for r in rows]}

class KineticaAdapter(GraphEngineAdapter):
    def __init__(self, settings):
        self._db = GPUdb(host=settings.kinetica_url, username=settings.kinetica_user,
                         password=settings.kinetica_pass)
        self._src = KineticaSource(self._db)

    def list_graphs(self):
        resp = self._db.show_graph(graph_name="")
        return list(resp.get("graph_names", []))

    def run_query(self, graph, cypher, timeout=60000):
        # `cypher` here is Kinetica SQL/GQL (engine-appropriate validation query).
        return _rows_to_result(list(self._src.rows(cypher)))

    def get_schema(self, graph):
        resp = self._db.show_graph(graph_name=graph)
        labels = list(resp.get("graph_labels", []) or [])
        return {"labels": labels, "rel_types": [], "dot": "digraph {}"}

    def fetch_entities(self, graph, limit):
        raise NotImplementedError("Kinetica entity fetch is S4; use run_query for validation")

    def get_record(self, graph, node_id):
        raise NotImplementedError("Kinetica record fetch is S4; use run_query for validation")
```

- [ ] **Step 4: Run unit test to verify it passes**

Run: `cd xgraph/backend && python -m pytest tests/test_kinetica_adapter.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Add live integration test (skips if Kinetica down)**

```python
# append to tests/test_kinetica_adapter.py
from xgraph_gateway import config
from xgraph_gateway.adapters.kinetica_adapter import KineticaAdapter

def _adapter_or_skip():
    s = config.load_settings()
    if not s.kinetica_url:
        pytest.skip("KINETICA_URL not set")
    try:
        a = KineticaAdapter(s); a.list_graphs(); return a
    except Exception as e:
        pytest.skip(f"Kinetica unreachable: {e}")

def test_live_kinetica_count_query():
    a = _adapter_or_skip()
    out = a.run_query("", "SELECT COUNT(*) AS c FROM expero.vertexes WHERE label = 'bank'")
    assert out["columns"] == ["c"]
    assert out["rows"][0][0] > 0
```

- [ ] **Step 6: Run integration test against live Kinetica**

Run: `cd xgraph/backend && python -m pytest tests/test_kinetica_adapter.py -v`
Expected: PASS or SKIP for `test_live_*`.

- [ ] **Step 7: Checkpoint (no commit)**

---

### Task 4: DuckDB ComputeEngine (hydration)

**Files:**
- Create: `xgraph/backend/xgraph_gateway/compute/__init__.py`
- Create: `xgraph/backend/xgraph_gateway/compute/duckdb_engine.py`
- Test: `xgraph/backend/tests/test_compute.py`

**Interfaces:**
- Consumes: `graph_loader.hydrate.hydrate` and `graph_loader.duckdb_source.coerce_row` from the `falkor` project (via `config.ensure_falkor_on_path`).
- Produces: `duckdb_engine.ComputeEngine()` with `hydrate(rows: list[dict], source: str, key: str = "NODE", columns: str = "*") -> list[dict]` (delegates to `graph_loader.hydrate.hydrate`) and `run_sql(sql: str) -> list[dict]` (raw DuckDB, `Decimal→float` coerced).

- [ ] **Step 1: Write the failing test (writes a tmp parquet, no services)**

```python
# xgraph/backend/tests/test_compute.py
import duckdb
from decimal import Decimal
from xgraph_gateway import config
from xgraph_gateway.compute.duckdb_engine import ComputeEngine

def _wide(tmp_path):
    p = tmp_path / "v.parquet"
    con = duckdb.connect()
    con.execute("""CREATE TABLE t AS SELECT * FROM (VALUES
        ('b1','Acme', 10.5),('b2','Beta', 3.0)) AS v(NODE, name, amount)""")
    con.execute(f"COPY t TO '{p}' (FORMAT parquet)"); con.close()
    return str(p)

def test_hydrate_attaches_and_coerces(tmp_path):
    eng = ComputeEngine()
    out = eng.hydrate([{"NODE": "b1", "risk": 1}], _wide(tmp_path), key="NODE")
    assert out[0]["name"] == "Acme"
    assert out[0]["risk"] == 1
    assert isinstance(out[0]["amount"], float) and not isinstance(out[0]["amount"], Decimal)

def test_run_sql_coerces(tmp_path):
    eng = ComputeEngine()
    rows = eng.run_sql(f"SELECT * FROM '{_wide(tmp_path)}' ORDER BY NODE")
    assert rows[0]["NODE"] == "b1"
    assert isinstance(rows[0]["amount"], float)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd xgraph/backend && python -m pytest tests/test_compute.py -v`
Expected: FAIL with `ModuleNotFoundError` for the compute module.

- [ ] **Step 3: Write duckdb_engine.py**

```python
# xgraph/backend/xgraph_gateway/compute/duckdb_engine.py
from __future__ import annotations
import duckdb
from xgraph_gateway import config

config.ensure_falkor_on_path(config.load_settings())
from graph_loader.hydrate import hydrate as _falkor_hydrate      # noqa: E402
from graph_loader.duckdb_source import coerce_row                 # noqa: E402

class ComputeEngine:
    def hydrate(self, rows, source, key="NODE", columns="*"):
        return _falkor_hydrate(rows, source, key=key, columns=columns)

    def run_sql(self, sql):
        con = duckdb.connect()
        try:
            cur = con.execute(sql)
            cols = [d[0] for d in cur.description]
            return [coerce_row(cols, r) for r in cur.fetchall()]
        finally:
            con.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd xgraph/backend && python -m pytest tests/test_compute.py -v`
Expected: PASS (2 passed). If it errors importing `graph_loader`, verify `XGRAPH_FALKOR_PATH` points at the sibling `falkor` dir.

- [ ] **Step 5: Checkpoint (no commit)**

---

### Task 5: SourceReader contract + DuckDB source

**Files:**
- Create: `xgraph/backend/xgraph_gateway/sources/__init__.py`
- Create: `xgraph/backend/xgraph_gateway/sources/base.py`
- Create: `xgraph/backend/xgraph_gateway/sources/duckdb_source.py`
- Test: `xgraph/backend/tests/test_sources.py`

**Interfaces:**
- Consumes: `graph_loader.duckdb_source.DuckDBSource` from `falkor`.
- Produces: `base.SourceReader` ABC with `read(spec: dict) -> list[dict]`. `duckdb_source.DuckDBSourceReader()` where `spec = {"tables": {name: path}, "sql": "..."}`; `read` registers tables and runs `sql`, yielding row dicts.

- [ ] **Step 1: Write the failing test**

```python
# xgraph/backend/tests/test_sources.py
import duckdb
from xgraph_gateway.sources.base import SourceReader
from xgraph_gateway.sources.duckdb_source import DuckDBSourceReader

def _parquet(tmp_path):
    p = tmp_path / "e.parquet"
    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT * FROM (VALUES ('b1','bank'),('w1','wire')) AS v(id,label)")
    con.execute(f"COPY t TO '{p}' (FORMAT parquet)"); con.close()
    return str(p)

def test_duckdb_source_reads_via_view(tmp_path):
    r = DuckDBSourceReader()
    assert isinstance(r, SourceReader)
    rows = r.read({"tables": {"expero.vertexes": _parquet(tmp_path)},
                   "sql": "SELECT id AS node_id, label FROM expero.vertexes ORDER BY id"})
    assert rows == [{"node_id": "b1", "label": "bank"}, {"node_id": "w1", "label": "wire"}]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd xgraph/backend && python -m pytest tests/test_sources.py -v`
Expected: FAIL with `ModuleNotFoundError` for the sources module.

- [ ] **Step 3: Write base.py and duckdb_source.py**

```python
# xgraph/backend/xgraph_gateway/sources/base.py
from __future__ import annotations
from abc import ABC, abstractmethod

class SourceReader(ABC):
    @abstractmethod
    def read(self, spec: dict) -> list[dict]: ...
```
```python
# xgraph/backend/xgraph_gateway/sources/duckdb_source.py
from __future__ import annotations
from xgraph_gateway import config
from .base import SourceReader

config.ensure_falkor_on_path(config.load_settings())
from graph_loader.duckdb_source import DuckDBSource   # noqa: E402

class DuckDBSourceReader(SourceReader):
    def read(self, spec):
        src = DuckDBSource.connect(spec["tables"])
        return list(src.rows(spec["sql"]))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd xgraph/backend && python -m pytest tests/test_sources.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Checkpoint (no commit)**

---

### Task 6: FastAPI gateway + registry + endpoints

**Files:**
- Create: `xgraph/backend/xgraph_gateway/registry.py`
- Create: `xgraph/backend/xgraph_gateway/app.py`
- Test: `xgraph/backend/tests/test_app.py`

**Interfaces:**
- Consumes: `FakeAdapter`, `FalkorDBAdapter`, `ComputeEngine`, `config`.
- Produces: `registry.get_adapter(engine: str) -> GraphEngineAdapter` (`"falkordb"` → real; `"fake"` → `FakeAdapter`, for tests). `app.create_app(adapter_factory=registry.get_adapter, compute=ComputeEngine())` → FastAPI app with endpoints from spec §5. `app.app` is the module-level ASGI app for uvicorn.
- Error envelope: any adapter exception → JSON `{"error": {...}}`, HTTP 502 (unreachable) / 504 (`"timed out"` in message) / 400 (otherwise).

- [ ] **Step 1: Write the failing test (uses fake engine via TestClient)**

```python
# xgraph/backend/tests/test_app.py
from fastapi.testclient import TestClient
from xgraph_gateway.app import create_app
from xgraph_gateway.adapters.fake import FakeAdapter

def _client():
    return TestClient(create_app(adapter_factory=lambda e: FakeAdapter()))

def test_graphs_endpoint():
    r = _client().get("/graphs", params={"engine": "fake"})
    assert r.status_code == 200
    assert r.json() == ["demo_graph"]

def test_query_endpoint():
    r = _client().post("/query", json={"engine": "fake", "graph": "demo_graph",
                                       "cypher": "MATCH (n) RETURN n.NODE AS NODE"})
    assert r.status_code == 200
    body = r.json()
    assert body["columns"] == ["NODE"]
    assert ["b1"] in body["rows"]

def test_schema_endpoint():
    r = _client().get("/schema", params={"engine": "fake", "graph": "demo_graph"})
    assert r.status_code == 200
    assert "bank" in r.json()["labels"]

def test_bad_query_returns_error_envelope():
    def boom(e):
        class A(FakeAdapter):
            def run_query(self, *a, **k): raise ValueError("bad cypher")
        return A()
    c = TestClient(create_app(adapter_factory=boom))
    r = c.post("/query", json={"engine": "fake", "graph": "g", "cypher": "x"})
    assert r.status_code == 400
    assert r.json()["error"]["message"] == "bad cypher"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd xgraph/backend && python -m pytest tests/test_app.py -v`
Expected: FAIL with `ModuleNotFoundError` for `xgraph_gateway.app`

- [ ] **Step 3: Write registry.py**

```python
# xgraph/backend/xgraph_gateway/registry.py
from __future__ import annotations
from . import config
from .adapters.fake import FakeAdapter
from .adapters.falkordb_adapter import FalkorDBAdapter
from .adapters.kinetica_adapter import KineticaAdapter

_SETTINGS = config.load_settings()

def get_adapter(engine: str):
    if engine == "fake":
        return FakeAdapter()
    if engine == "falkordb":
        return FalkorDBAdapter(_SETTINGS)
    if engine == "kinetica":
        return KineticaAdapter(_SETTINGS)
    raise ValueError(f"unknown engine: {engine}")
```

- [ ] **Step 4: Write app.py**

```python
# xgraph/backend/xgraph_gateway/app.py
from __future__ import annotations
from fastapi import FastAPI, Body
from fastapi.responses import JSONResponse
from . import registry
from .compute.duckdb_engine import ComputeEngine

def _status_for(exc: Exception) -> int:
    msg = str(exc).lower()
    if "timed out" in msg or "timeout" in msg:
        return 504
    if "unreachable" in msg or "connection" in msg or "refused" in msg:
        return 502
    return 400

def _err(engine: str, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=_status_for(exc),
        content={"error": {"code": type(exc).__name__, "message": str(exc),
                           "engine": engine, "detail": None}})

def create_app(adapter_factory=registry.get_adapter, compute=None) -> FastAPI:
    compute = compute or ComputeEngine()
    app = FastAPI(title="xgraph gateway")

    @app.get("/engines")
    def engines():
        return {"graph_engines": ["falkordb", "kinetica", "fake"], "sources": ["duckdb"]}

    @app.get("/graphs")
    def graphs(engine: str):
        try:
            return adapter_factory(engine).list_graphs()
        except Exception as e:
            return _err(engine, e)

    @app.get("/schema")
    def schema(engine: str, graph: str):
        try:
            return adapter_factory(engine).get_schema(graph)
        except Exception as e:
            return _err(engine, e)

    @app.post("/query")
    def query(payload: dict = Body(...)):
        engine = payload.get("engine", "")
        try:
            return adapter_factory(engine).run_query(
                payload["graph"], payload["cypher"], payload.get("timeout", 60000))
        except Exception as e:
            return _err(engine, e)

    @app.get("/entities")
    def entities(engine: str, graph: str, limit: int = 1000):
        try:
            return adapter_factory(engine).fetch_entities(graph, limit)
        except Exception as e:
            return _err(engine, e)

    @app.get("/record")
    def record(engine: str, graph: str, id: str):
        try:
            return adapter_factory(engine).get_record(graph, id)
        except Exception as e:
            return _err(engine, e)

    @app.post("/hydrate")
    def hydrate(payload: dict = Body(...)):
        try:
            return compute.hydrate(payload["rows"], payload["source"],
                                   key=payload.get("key", "NODE"),
                                   columns=payload.get("columns", "*"))
        except Exception as e:
            return _err("duckdb", e)

    @app.post("/sql")
    def sql(payload: dict = Body(...)):
        try:
            return compute.run_sql(payload["sql"])
        except Exception as e:
            return _err("duckdb", e)

    return app

app = create_app()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd xgraph/backend && python -m pytest tests/test_app.py -v`
Expected: PASS (4 passed)

- [ ] **Step 6: Checkpoint (no commit)** — `python -m pytest tests/ -q` all green.

---

### Task 7: Live end-to-end verification of the gateway

**Files:**
- Create: `xgraph/backend/tests/test_e2e_live.py`

**Interfaces:**
- Consumes: the full app + real FalkorDB + a wide Parquet at `XGRAPH_VERTEXES_PARQUET` (default `../falkor/data/vertexes.parquet`, produced by the falkor verification work).

- [ ] **Step 1: Write the live e2e test (skips if FalkorDB or Parquet absent)**

```python
# xgraph/backend/tests/test_e2e_live.py
import os, pytest
from fastapi.testclient import TestClient
from xgraph_gateway.app import create_app
from xgraph_gateway import registry

PARQUET = os.environ.get("XGRAPH_VERTEXES_PARQUET", "../falkor/data/vertexes.parquet")

@pytest.fixture
def client():
    c = TestClient(create_app())
    if "banking_graph" not in c.get("/graphs", params={"engine": "falkordb"}).json():
        pytest.skip("banking_graph not available")
    return c

def test_query_then_hydrate_surfaces_ungraphed_column(client):
    if not os.path.exists(PARQUET):
        pytest.skip("vertexes.parquet not present")
    q = client.post("/query", json={"engine": "falkordb", "graph": "banking_graph",
        "cypher": "MATCH (b:bank) RETURN b.NODE AS NODE LIMIT 5"})
    assert q.status_code == 200
    ids = [{"NODE": row[0]} for row in q.json()["rows"]]
    h = client.post("/hydrate", json={"rows": ids, "source": PARQUET, "key": "NODE",
        "columns": 'NODE, "bank:bank_number" AS bank_number'})
    assert h.status_code == 200
    out = h.json()
    assert len(out) == 5
    assert all("bank_number" in r for r in out)   # column never stored in the graph
```

- [ ] **Step 1b: Write the cross-engine validation test (FalkorDB vs Kinetica parity)**

This is the validation pattern used at every slice: the same fact checked on both engines through the one gateway. Skips if either engine is unavailable.

```python
# append to tests/test_e2e_live.py
def test_bank_count_matches_between_falkordb_and_kinetica():
    c = TestClient(create_app())
    engines = c.get("/engines").json()["graph_engines"]
    fk = c.get("/graphs", params={"engine": "falkordb"}).json()
    if "banking_graph" not in fk:
        pytest.skip("banking_graph not available")
    fq = c.post("/query", json={"engine": "falkordb", "graph": "banking_graph",
        "cypher": "MATCH (b:bank) RETURN count(b) AS c"})
    if fq.status_code != 200:
        pytest.skip(f"falkordb query failed: {fq.json()}")
    kq = c.post("/query", json={"engine": "kinetica", "graph": "",
        "cypher": "SELECT COUNT(DISTINCT id) AS c FROM expero.vertexes WHERE label = 'bank'"})
    if kq.status_code != 200:
        pytest.skip(f"kinetica unavailable: {kq.json()}")
    assert fq.json()["rows"][0][0] == kq.json()["rows"][0][0]
```

Note: FalkorDB `banking_graph` was built from Kinetica `expero.vertexes` (deduped by id), so the FalkorDB `bank` node count equals Kinetica's `COUNT(DISTINCT id) WHERE label='bank'` — the equivalence we confirmed in the falkor verification. If the two Kinetica-vs-FalkorDB data snapshots have drifted, rebuild the FalkorDB graph from current Kinetica data first.

- [ ] **Step 2: Ensure the wide Parquet exists**

Run: `ls -lh ../falkor/data/vertexes.parquet`
Expected: file present. If absent, regenerate it via the falkor DuckDB-route export (see `falkor` CLAUDE.md — export **without** `ORDER BY`).

- [ ] **Step 3: Run the live e2e test**

Run: `cd xgraph/backend && python -m pytest tests/test_e2e_live.py -v`
Expected: PASS (or SKIP if FalkorDB/Parquet unavailable).

- [ ] **Step 4: Manual smoke via HTTP (optional, requires uvicorn running)**

Run in one shell: `cd xgraph/backend && uvicorn xgraph_gateway.app:app --port 8088`
Run in another:
```bash
curl -s 'localhost:8088/graphs?engine=falkordb'
curl -s -X POST localhost:8088/query -H 'content-type: application/json' \
  -d '{"engine":"falkordb","graph":"banking_graph","cypher":"MATCH (b:bank) RETURN b.NODE AS NODE LIMIT 3"}'
```
Expected: JSON graph list; JSON `{"columns":["NODE"],"rows":[...]}`.

- [ ] **Step 5: Checkpoint (no commit).** Full suite: `cd xgraph/backend && python -m pytest -q`. All pass or skip.

---

## Self-Review

**Spec coverage (S0+S1):**
- GraphEngineAdapter contract → Task 2; FalkorDB impl → Task 3; Kinetica validation impl → Task 3B. ✓
- Kinetica preserved as switchable validation route (`engine=kinetica`) from S1 → Task 3B + registry (Task 6) + cross-engine parity test (Task 7 Step 1b). ✓
- ComputeEngine (hydrate + run_sql, Decimal coercion) → Task 4. ✓
- SourceReader contract + duckdb mechanism → Task 5. ✓
- Gateway HTTP surface (all §5 endpoints) → Task 6. ✓
- Error envelope + 400/502/504 → Task 6 (`_err`/`_status_for`). ✓
- S1 acceptance #1 (graphs) → Task 6/7; #2 (schema+DOT) → Task 3/6; #3 (query) → Task 3/6; #4 (hydrate surfaces `bank:bank_number`) → Task 7. ✓
- Testing philosophy (unit no-service; integration SKIP) → Tasks 3,4,7. ✓
- Frontend acceptance (#5) and `explorer/` untouched (#7) → **out of scope for this plan**, deferred to the frontend plan (Plan 2). Noted, not a gap.
- `load_graph` write side, `join_sql`/`query_ref` hydrate forms, Kinetica/PuppyGraph adapters → later slices per spec §12; intentionally absent.

**Placeholder scan:** no TBD/TODO; every code step has complete code. ✓

**Type consistency:** adapter methods (`list_graphs`/`get_schema`/`run_query`/`fetch_entities`/`get_record`) and `{"columns","rows"}` / `{"nodes","edges"}` shapes are identical across Tasks 2, 3, 6, 7; `ComputeEngine.hydrate(rows, source, key, columns)` signature matches Task 4 and its `/hydrate` caller in Task 6. ✓

## Notes for the frontend plan (Plan 2, to be written after reading explorer)

Plan 2 will: copy `explorer/KineticaGraphExplorer.html` → `xgraph/frontend/XGraph.html`; replace the `useKineticaApi` hook with a `useGatewayApi` hook that calls the endpoints above and returns the same `{columns, rows}` / `{nodes, edges}` shapes; add a Hydrate affordance calling `/hydrate`; and verify against the running gateway. It leaves `explorer/` untouched.
