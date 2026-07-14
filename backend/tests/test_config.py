import os
from xgraph_gateway import config


def test_load_settings_reads_env(monkeypatch):
    monkeypatch.setenv("FALKORDB_HOST", "h")
    monkeypatch.setenv("FALKORDB_PORT", "7000")
    monkeypatch.setenv("FALKORDB_PASSWORD", "pw")
    monkeypatch.setenv("XGRAPH_DATA_DIR", "/tmp/xgdata")
    s = config.load_settings()
    assert (s.falkordb_host, s.falkordb_port, s.falkordb_password) == ("h", 7000, "pw")
    assert s.data_dir == "/tmp/xgdata"


def test_load_settings_defaults(monkeypatch):
    for k in ("FALKORDB_HOST", "FALKORDB_PORT", "XGRAPH_DATA_DIR"):
        monkeypatch.delenv(k, raising=False)
    s = config.load_settings()
    assert s.falkordb_host == "localhost"
    assert s.falkordb_port == 6379
    # default data dir is the repo's <repo>/data, resolved absolutely
    assert s.data_dir.endswith("/data")
    assert os.path.isabs(s.data_dir)


def test_resolve_data_path_absolute_and_url_verbatim(monkeypatch):
    monkeypatch.setenv("XGRAPH_DATA_DIR", "/tmp/xgdata")
    assert config.resolve_data_path("/abs/vertexes.parquet") == "/abs/vertexes.parquet"
    assert config.resolve_data_path("s3://bucket/v.parquet") == "s3://bucket/v.parquet"


def test_resolve_data_path_relative_joins_data_dir(monkeypatch):
    monkeypatch.setenv("XGRAPH_DATA_DIR", "/tmp/xgdata")
    assert config.resolve_data_path("vertexes.parquet") == "/tmp/xgdata/vertexes.parquet"
    # ".." is normalized (DuckDB globs don't resolve it)
    assert config.resolve_data_path("../falkor/v.parquet") == "/tmp/falkor/v.parquet"


def test_resolve_data_path_empty_passthrough():
    assert config.resolve_data_path("") == ""
