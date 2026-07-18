from __future__ import annotations
import hashlib
import os
from fastapi import FastAPI, Body, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from . import registry
from . import nlcypher
from . import extract
from . import extract_fold
from .compute.duckdb_engine import ComputeEngine
from .sessions import SessionStore

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

def create_app(adapter_factory=registry.get_adapter, compute=None, store=None) -> FastAPI:
    compute = compute or ComputeEngine()
    store = store if store is not None else SessionStore()
    app = FastAPI(title="xgraph gateway")
    # The frontend is served from file:// or a different localhost port, so every
    # gateway call is cross-origin. Allow all origins (local dev tool, no cookies/creds).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def _no_cache_frontend(request, call_next):
        # The gateway serves the single-file frontend; without this a browser
        # caches XGraph.html/gateway.js and silently runs a stale build after a
        # deploy. Force revalidation for the static assets (dev tool, local only).
        resp = await call_next(request)
        path = request.url.path
        if path == "/" or path.endswith((".html", ".js", ".css")):
            resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp

    def _resolve_adapter(session, engine):
        if session:
            return store.get(session)["adapter"]
        return adapter_factory(engine)

    def _resolve_compute(session):
        if session:
            return store.get(session)["compute"]
        return compute

    def _resolve_engine(session, engine):
        if session:
            return store.get(session)["graph_engine"]
        return engine

    def _resolve_extract_mode(session):
        # Reads the SessionStore (not the compute store that extract_endpoint
        # locally shadows as `store`); defaults to the conservative sequential.
        if session:
            return store.get(session).get("extract_mode", "sequential")
        return "sequential"

    @app.get("/engines")
    def engines():
        return {"graph_engines": ["falkordb", "kinetica", "fake"], "sources": ["duckdb"]}

    @app.post("/connect")
    def connect(payload: dict = Body(...)):
        graph = payload.get("graph", {})
        compute_cfg = payload.get("compute", {})
        llm_cfg = payload.get("llm", {})
        try:
            session_id = store.create(
                graph.get("engine"), graph.get("conn"),
                compute_cfg.get("engine"), compute_cfg.get("conn"),
                extract_mode=llm_cfg.get("extract_mode"))
            adapter = store.get(session_id)["adapter"]
            return {"session": session_id, "graphs": adapter.list_graphs()}
        except Exception as e:
            return _err(graph.get("engine", ""), e)

    @app.get("/graphs")
    def graphs(engine: str = "", session: str | None = None):
        try:
            return _resolve_adapter(session, engine).list_graphs()
        except Exception as e:
            return _err(engine, e)

    @app.get("/graph_sizes")
    def graph_sizes(engine: str = "", session: str | None = None):
        try:
            return _resolve_adapter(session, engine).graph_sizes()
        except Exception as e:
            return _err(engine, e)

    @app.get("/schema")
    def schema(graph: str, engine: str = "", session: str | None = None,
               full: bool = False, nkey: bool = False, ekey: bool = False):
        try:
            result = _resolve_adapter(session, engine).get_schema(
                graph, options={"full": full, "nkey": nkey, "ekey": ekey})
            try:
                amap = _resolve_compute(session).axis_map(graph, "entity")
                if amap:
                    axes = {}
                    for label in result.get("labels", []):
                        axes.setdefault(amap.get(label, "EntityType"), []).append(label)
                    result["axes"] = axes
            except Exception:
                pass  # keep the adapter's default axes on any store error
            return result
        except Exception as e:
            return _err(engine, e)

    @app.post("/query")
    def query(payload: dict = Body(...)):
        engine = payload.get("engine", "")
        session = payload.get("session")
        try:
            return _resolve_adapter(session, engine).run_query(
                payload["graph"], payload["cypher"], payload.get("timeout", 60000))
        except Exception as e:
            return _err(engine, e)

    @app.get("/entities")
    def entities(graph: str, engine: str = "", limit: int = 1000, offset: int = 0,
                 session: str | None = None):
        try:
            return _resolve_adapter(session, engine).fetch_entities(graph, limit, offset)
        except Exception as e:
            return _err(engine, e)

    @app.get("/record")
    def record(graph: str, id: str, engine: str = "", session: str | None = None):
        try:
            return _resolve_adapter(session, engine).get_record(graph, id)
        except Exception as e:
            return _err(engine, e)

    @app.post("/create")
    def create(payload: dict = Body(...)):
        engine = payload.get("engine", "")
        session = payload.get("session")
        try:
            return _resolve_adapter(session, engine).load_graph(payload["spec"])
        except Exception as e:
            return _err(engine, e)

    @app.get("/storage")
    def storage(graph: str, engine: str = "", session: str | None = None):
        try:
            return _resolve_adapter(session, engine).storage(graph)
        except Exception as e:
            return _err(engine, e)

    @app.get("/graph_ddl")
    def graph_ddl(graph: str, engine: str = "", session: str | None = None):
        try:
            return _resolve_adapter(session, engine).creation_statement(graph)
        except Exception as e:
            return _err(engine, e)

    @app.get("/source_preview")
    def source_preview(source: str, session: str | None = None):
        try:
            return _resolve_compute(session).preview_source(source)
        except Exception as e:
            return _err("duckdb", e)

    @app.post("/delete_graph")
    def delete_graph(payload: dict = Body(...)):
        engine = payload.get("engine", "")
        session = payload.get("session")
        try:
            graph = payload["graph"]
            result = _resolve_adapter(session, engine).delete_graph(graph)
            # Clear the ledger + ontology rows too, or a deleted-then-re-
            # extracted identical document would be silently short-circuited
            # as "unchanged" (0 entities) by the ledger's stale sha256 row.
            _resolve_compute(session).clear_graph_metadata(graph)
            return result
        except Exception as e:
            return _err(engine, e)

    @app.post("/extract")
    async def extract_endpoint(file: UploadFile = File(None), text: str = Form(None),
                                graph: str = Form(...), hint: str = Form(None),
                                session: str = Form(None), engine: str = Form("")):
        try:
            if file is not None and file.filename:
                content = await file.read()
                doc = extract.read_document(file.filename, content)
                doc_uri, source_type = file.filename, "file"
            else:
                doc = text
                doc_uri, source_type = None, "text"
            if not doc or not doc.strip():
                raise ValueError("extract requires a non-empty file or text")

            sha = hashlib.sha256(doc.encode("utf-8")).hexdigest()
            if doc_uri is None:
                doc_uri = f"text:{sha[:12]}"

            store = _resolve_compute(session)
            existing = store.get_document(graph, doc_uri)

            # Idempotent short-circuit: identical bytes already ingested for
            # this graph. Only bumps last_ingested_ts -- no re-extraction/
            # re-ingest, no ledger row for bytes that were never actually
            # processed. Checked via a read (get_document) BEFORE any commit,
            # so a graph that was deleted (which clears the ledger) or a
            # prior extraction that failed (which never committed a ledger
            # row -- see below) both correctly fall through to re-extraction.
            if existing is not None and existing.get("sha256") == sha:
                record = store.record_document(graph, doc_uri, sha, source_type)
                doc_info = {"doc_uri": doc_uri, "sha256": sha, **record}
                return {"graph": graph, "entities": 0, "relations": 0,
                        "entities_new": 0, "relations_new": 0,
                        "labels": {"node_labels": [], "edge_labels": []},
                        "truncated": False, "folded": [],
                        "document": {**doc_info, "reused": True}}

            res = extract.extract_document(doc, hint, mode=_resolve_extract_mode(session))
            folded = extract_fold.fold_labels(store, graph, res["entities"],
                                              res["relations"], doc_uri)
            adapter = _resolve_adapter(session, engine)
            out = adapter.ingest_elements(graph, res["entities"], res["relations"])
            # Ledger is committed ONLY after a successful ingest -- if
            # extraction/ingest throws above, this line never runs and no
            # ledger row exists, so resubmitting the same bytes retries
            # cleanly instead of being a permanent no-op.
            record = store.record_document(graph, doc_uri, sha, source_type)
            doc_info = {"doc_uri": doc_uri, "sha256": sha, **record}
            return {"graph": graph, "entities": out["nodes"], "relations": out["edges"],
                    "entities_new": out.get("nodes_created", out["nodes"]),
                    "relations_new": out.get("edges_created", out["edges"]),
                    "labels": out["labels"], "truncated": res["truncated"],
                    "folded": folded,
                    "document": {**doc_info, "reused": False}}
        except Exception as e:
            return _err(engine, e)

    @app.get("/documents")
    def documents(graph: str, engine: str = "", session: str | None = None):
        try:
            return {"documents": _resolve_compute(session).list_documents(graph)}
        except Exception as e:
            return _err(engine, e)

    @app.post("/ask")
    def ask(payload: dict = Body(...)):
        session = payload.get("session")
        engine = payload.get("engine", "")
        try:
            adapter = _resolve_adapter(session, engine)
            eng = _resolve_engine(session, engine)
            graph = payload["graph"]
            question = payload["question"]
            schema = adapter.get_schema(graph)
            cypher = nlcypher.generate_cypher(schema, eng, question, graph=graph)
            ok, reason = nlcypher.validate_cypher(cypher, schema)
            if not ok:
                return _err(engine, ValueError(reason))
            res = adapter.run_query(graph, cypher)
            answer = nlcypher.synthesize(question, res["columns"], res["rows"], cypher=cypher)
            return {"question": question, "cypher": cypher, "columns": res["columns"],
                    "rows": res["rows"], "graph": res.get("graph", {}), "answer": answer}
        except Exception as e:
            return _err(engine, e)

    @app.post("/nl2cypher")
    def nl2cypher(payload: dict = Body(...)):
        session = payload.get("session")
        engine = payload.get("engine", "")
        try:
            adapter = _resolve_adapter(session, engine)
            eng = _resolve_engine(session, engine)
            graph = payload["graph"]
            schema = adapter.get_schema(graph)
            cypher = nlcypher.generate_cypher(schema, eng, payload["question"], graph=graph)
            return {"cypher": cypher}
        except Exception as e:
            return _err(engine, e)

    @app.post("/synthesize")
    def synthesize_endpoint(payload: dict = Body(...)):
        engine = payload.get("engine", "")
        try:
            answer = nlcypher.synthesize(payload["question"], payload["columns"], payload["rows"],
                                          cypher=payload.get("cypher"))
            return {"answer": answer}
        except Exception as e:
            return _err(engine, e)

    @app.post("/hydrate")
    def hydrate(payload: dict = Body(...)):
        session = payload.get("session")
        try:
            return _resolve_compute(session).hydrate(
                payload["rows"], payload["source"],
                key=payload.get("key", "NODE"),
                columns=payload.get("columns", "*"))
        except Exception as e:
            return _err("duckdb", e)

    @app.post("/explain")
    def explain(payload: dict = Body(...)):
        try:
            focus = (payload.get("question") or "").strip()
            source = payload.get("source")
            cols, rows, cypher = payload["columns"], payload["rows"], payload.get("cypher")
            # File-based post-join hydration reads the Parquet/CSV hydrate file, which
            # is intrinsically a DuckDB operation — use DuckDB here regardless of the
            # session's OLAP engine (Kinetica compute can't post-join a local file).
            duck = ComputeEngine()
            join_sql, hydrated = None, False
            out_cols, out_rows = cols, rows
            if focus and source:
                wide_cols = duck.describe_source(source)
                join_sql = nlcypher.generate_join_sql(focus, cypher, cols, wide_cols) or None
                if join_sql:
                    ok, reason = nlcypher.validate_sql(join_sql)
                    if not ok:
                        return _err("duckdb", ValueError(reason))
                    dict_rows = [dict(zip(cols, r)) for r in rows]
                    agg = duck.run_join(dict_rows, source, join_sql)
                    out_cols = list(agg[0].keys()) if agg else []
                    out_rows = [[d.get(c) for c in out_cols] for d in agg]
                    hydrated = True
            q = focus or "Explain these results"
            answer = nlcypher.synthesize(q, out_cols, out_rows,
                                          cypher=(join_sql if hydrated else cypher))
            return {"answer": answer, "join_sql": join_sql, "columns": out_cols,
                    "rows": out_rows, "hydrated": hydrated}
        except Exception as e:
            return _err(payload.get("engine", ""), e)

    @app.post("/sql")
    def sql(payload: dict = Body(...)):
        session = payload.get("session")
        try:
            return _resolve_compute(session).run_sql(payload["sql"])
        except Exception as e:
            return _err("duckdb", e)

    # Serve the single-file frontend so `http://localhost:8090/` IS the app —
    # one process, same-origin (no CORS needed when loaded this way). Registered
    # AFTER every API route + mounted last, so it never shadows the API.
    _frontend = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "frontend"))
    if os.path.isdir(_frontend):
        @app.get("/")
        def _index():
            return FileResponse(os.path.join(_frontend, "XGraph.html"))
        app.mount("/", StaticFiles(directory=_frontend), name="frontend")

    return app

app = create_app()
