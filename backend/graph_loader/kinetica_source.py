from __future__ import annotations

from typing import Iterator

import gpudb

# Default page size for paging through Kinetica SQL results. Kinetica's
# server-side cap (max_get_records_size) is commonly ~20000; pages smaller
# than that keep individual requests light while still paging efficiently.
DEFAULT_PAGE_SIZE = 10000


class KineticaSource:
    """Runs SQL against Kinetica and yields rows as plain dicts."""

    def __init__(self, db, page_size: int = DEFAULT_PAGE_SIZE):
        self._db = db
        self._page_size = page_size

    @classmethod
    def connect(cls, url: str, username: str, password: str) -> "KineticaSource":
        return cls(gpudb.GPUdb(host=url, username=username, password=password))

    def rows(self, sql: str) -> Iterator[dict]:
        # NOTE: limit=-9999 (Kinetica's END_OF_SET convention) does NOT mean
        # "return all rows" -- it means "return as many rows as the server's
        # max_get_records_size cap allows in a single response" (commonly
        # ~20000). A single call silently truncates any result set larger
        # than that cap, setting `has_more_records=True` with no error.
        #
        # To read the FULL result set we page with `offset`/`limit`,
        # continuing while the server reports `has_more_records`.
        #
        # We use `execute_sql_and_decode` with `get_column_major=False`:
        # - `execute_sql` leaves the response encoded (no usable `.records`).
        # - the default `get_column_major=True` returns `.records` as an
        #   OrderedDict of column -> value-list (column-major), which is not
        #   a per-row structure. `get_column_major=False` returns `.records`
        #   as a list of row Record objects that convert cleanly via dict().
        # `has_more_records` is a response *key* (dict-style access), not an
        # attribute.
        offset = 0
        while True:
            resp = self._db.execute_sql_and_decode(
                sql, offset=offset, limit=self._page_size, get_column_major=False
            )
            # Some responses (notably GRAPH ... MATCH queries) don't carry a
            # `records` attribute or a `has_more_records` key -- accessing them
            # raised AttributeError and crashed the whole query (and made the
            # live NL->query test flaky). Access defensively: no records => empty
            # result, no has_more => stop. (Diverges from falkor's graph_loader.)
            records = getattr(resp, "records", None)
            if records is None:
                records = resp["records"] if "records" in resp else []
            for rec in records:
                yield dict(rec)

            offset += len(records)

            has_more = resp["has_more_records"] if "has_more_records" in resp else False
            if not records or not has_more:
                break
