from __future__ import annotations

import argparse
import os

from dotenv import load_dotenv

from . import mapper
from .config import load_mapping
from .duckdb_source import DuckDBSource
from .falkordb_sink import FalkorDBSink
from .kinetica_source import KineticaSource


def run_build(mapping, source, sink) -> dict:
    # Counts reflect what was actually written to the graph (from the query
    # result stats), NOT the number of source rows -- an edge whose endpoint
    # is missing creates nothing, and a duplicate node id MERGEs once, so
    # row counts would overstate the real graph.
    counts = {"nodes": {}, "edges": {}}
    sink.wipe()
    sink.run(mapper.entity_index_statement(mapping.node_key_property))

    labels = set()
    for spec in mapping.nodes:
        rows = list(source.rows(spec.sql))
        for batch in mapper.node_batches(spec, rows):
            result = sink.run(batch.query, batch.params)
            label = batch.params["label"]
            labels.add(label)
            counts["nodes"][label] = counts["nodes"].get(label, 0) + int(result.nodes_created)

    for stmt in mapper.label_index_statements(mapping.node_key_property, sorted(labels)):
        sink.run(stmt)

    for spec in mapping.edges:
        rows = list(source.rows(spec.sql))
        for batch in mapper.edge_batches(spec, rows, mapping.node_key_property):
            result = sink.run(batch.query, batch.params)
            etype = batch.params["type"]
            counts["edges"][etype] = counts["edges"].get(etype, 0) + int(result.relationships_created)

    return counts


def build(mapping_path: str, source_kind: str = "kinetica") -> dict:
    load_dotenv()
    mapping = load_mapping(mapping_path)
    if source_kind == "duckdb":
        # Kinetica-free route: read node/edge rows from Parquet/CSV files.
        if mapping.duckdb is None:
            raise SystemExit(
                "mapping has no 'duckdb:' section; add a table -> file map "
                "to build with --source duckdb")
        source = DuckDBSource.connect(mapping.duckdb.tables)
    else:
        source = KineticaSource.connect(
            os.environ["KINETICA_URL"],
            os.environ["KINETICA_USER"],
            os.environ["KINETICA_PASS"],
        )
    sink = FalkorDBSink.connect(
        mapping.graph,
        host=os.environ.get("FALKORDB_HOST", "localhost"),
        port=int(os.environ.get("FALKORDB_PORT", "6379")),
        password=os.environ.get("FALKORDB_PASSWORD"),
    )
    return run_build(mapping, source, sink)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build a FalkorDB graph from Kinetica tables")
    parser.add_argument("--config", default="mapping.yaml",
                        help="Path to the YAML mapping (default: mapping.yaml)")
    parser.add_argument("--source", choices=("kinetica", "duckdb"),
                        default="kinetica",
                        help="Row source: 'kinetica' (default) or 'duckdb' "
                             "(reads Parquet/CSV files; no Kinetica)")
    args = parser.parse_args(argv)
    counts = build(args.config, source_kind=args.source)
    print("Loaded nodes:", counts["nodes"])
    print("Loaded edges:", counts["edges"])


if __name__ == "__main__":
    main()
