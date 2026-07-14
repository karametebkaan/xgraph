import pytest

from xgraph_gateway import registry
from xgraph_gateway.compute.duckdb_engine import DuckDBComputeEngine
from xgraph_gateway.compute.kinetica_engine import KineticaComputeEngine
from xgraph_gateway.adapters.fake import FakeAdapter


def test_get_compute_duckdb_returns_duckdb_engine():
    eng = registry.get_compute("duckdb")
    assert isinstance(eng, DuckDBComputeEngine)

def test_get_compute_duckdb_ignores_conn():
    eng = registry.get_compute("duckdb", conn={"url": "ignored"})
    assert isinstance(eng, DuckDBComputeEngine)

def test_get_compute_kinetica_returns_kinetica_engine():
    eng = registry.get_compute("kinetica", conn={"url": "h", "user": "u", "password": "p"},)
    assert isinstance(eng, KineticaComputeEngine)

def test_get_compute_unknown_raises():
    with pytest.raises(ValueError):
        registry.get_compute("x")

def test_get_adapter_fake_ignores_conn():
    a = registry.get_adapter("fake", conn={"host": "ignored"})
    assert isinstance(a, FakeAdapter)

def test_get_adapter_unknown_raises():
    with pytest.raises(ValueError):
        registry.get_adapter("nope")
