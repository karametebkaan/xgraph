from __future__ import annotations
from . import config
from .adapters.fake import FakeAdapter
from .adapters.falkordb_adapter import FalkorDBAdapter
from .adapters.kinetica_adapter import KineticaAdapter
from .compute.duckdb_engine import DuckDBComputeEngine
from .compute.kinetica_engine import KineticaComputeEngine

_SETTINGS = config.load_settings()

def get_adapter(engine: str, conn: dict | None = None):
    if engine == "fake":
        return FakeAdapter()
    if engine == "falkordb":
        return FalkorDBAdapter(_SETTINGS, conn=conn)
    if engine == "kinetica":
        return KineticaAdapter(_SETTINGS, conn=conn)
    raise ValueError(f"unknown engine: {engine}")

def get_compute(engine: str, conn: dict | None = None):
    if engine == "duckdb":
        return DuckDBComputeEngine()
    if engine == "kinetica":
        return KineticaComputeEngine(conn)
    raise ValueError(f"unknown compute engine: {engine}")
