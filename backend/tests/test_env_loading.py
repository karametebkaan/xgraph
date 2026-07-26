import os


def test_load_backend_env_reads_file(tmp_path, monkeypatch):
    monkeypatch.delenv("XG_TEST_ENV_VAR", raising=False)
    f = tmp_path / ".env"
    f.write_text("XG_TEST_ENV_VAR=from_dotenv\n")
    from xgraph_gateway.app import _load_backend_env
    _load_backend_env(str(f))
    assert os.environ["XG_TEST_ENV_VAR"] == "from_dotenv"


def test_load_backend_env_missing_is_noop(tmp_path):
    from xgraph_gateway.app import _load_backend_env
    p = _load_backend_env(str(tmp_path / "nope.env"))
    assert str(p).endswith("nope.env")  # returns path, does not raise


def test_load_backend_env_does_not_override_existing(tmp_path, monkeypatch):
    monkeypatch.setenv("XG_TEST_ENV_VAR2", "already_set")
    f = tmp_path / ".env"
    f.write_text("XG_TEST_ENV_VAR2=from_dotenv\n")
    from xgraph_gateway.app import _load_backend_env
    _load_backend_env(str(f))
    assert os.environ["XG_TEST_ENV_VAR2"] == "already_set"  # override=False
