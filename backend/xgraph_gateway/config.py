from __future__ import annotations
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

# Repo data directory: <repo>/data (holds the banking demo Parquet after unzip).
# Override with XGRAPH_DATA_DIR. config.py lives at <repo>/backend/xgraph_gateway/,
# so the default is two levels up + /data.
_DEFAULT_DATA_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "data"))


@dataclass
class Settings:
    falkordb_host: str
    falkordb_port: int
    falkordb_password: str | None
    data_dir: str
    kinetica_url: str | None = None
    kinetica_user: str | None = None
    kinetica_pass: str | None = None


def load_settings() -> Settings:
    return Settings(
        falkordb_host=os.environ.get("FALKORDB_HOST", "localhost"),
        falkordb_port=int(os.environ.get("FALKORDB_PORT", "6379")),
        falkordb_password=os.environ.get("FALKORDB_PASSWORD"),
        data_dir=os.environ.get("XGRAPH_DATA_DIR", _DEFAULT_DATA_DIR),
        kinetica_url=os.environ.get("KINETICA_URL"),
        kinetica_user=os.environ.get("KINETICA_USER"),
        kinetica_pass=os.environ.get("KINETICA_PASS"),
    )


def resolve_meta_path() -> str:
    """Absolute path to the persistent DuckDB metadata database (documents
    ledger + ontology). Override with XGRAPH_META_DB; defaults to
    `<data_dir>/xgraph_meta.duckdb`."""
    override = os.environ.get("XGRAPH_META_DB")
    if override:
        return os.path.abspath(override)
    return os.path.join(load_settings().data_dir, "xgraph_meta.duckdb")


def resolve_data_path(path: str) -> str:
    """Resolve a data-file path portably.

    Absolute paths (and remote URLs like s3://, http(s)://) are used verbatim;
    a bare/relative path is resolved against `data_dir` so the frontend can send
    `vertexes.parquet` without hardcoding an absolute host path.
    """
    if not path:
        return path
    if "://" in path or os.path.isabs(path):
        return path
    # normpath: DuckDB treats the path as a glob and does not resolve ".."
    return os.path.normpath(os.path.join(load_settings().data_dir, path))
