#!/usr/bin/env bash
# Unzip the banking demo Parquet from the repo's tracked archives.
#
# The full Parquet (~138 MB) is stored zipped under data/ (vertexes 61 MB,
# edges 49 MB compressed — each under GitHub's 100 MB file limit). This script
# extracts them into data/ so the workbench's default HYDRATE_SOURCE and Create
# panel (which point at data/vertexes.parquet and data/edges.parquet) work.
#
# Run once after cloning:  ./scripts/unzip-data.sh
set -euo pipefail

DATA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/data"

for name in vertexes edges; do
    zip="$DATA_DIR/$name.parquet.zip"
    out="$DATA_DIR/$name.parquet"
    if [[ -f "$out" ]]; then
        echo "already present: $out"
        continue
    fi
    if [[ ! -f "$zip" ]]; then
        echo "missing archive: $zip" >&2
        exit 1
    fi
    echo "unzipping $zip -> $out"
    unzip -o -j "$zip" -d "$DATA_DIR" >/dev/null
done

echo "done. data/:"
ls -la "$DATA_DIR"/*.parquet
