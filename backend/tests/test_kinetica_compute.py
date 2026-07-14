from decimal import Decimal

from xgraph_gateway.compute.kinetica_engine import (
    KineticaComputeEngine, _ids_sql_list, _escape_sql_literal,
)


class FakeKineticaSource:
    """Stands in for graph_loader.kinetica_source.KineticaSource: canned
    `.rows(sql)` so tests never touch a live Kinetica instance."""

    def __init__(self, rows_by_sql=None, canned=None):
        self._rows_by_sql = rows_by_sql or {}
        self._canned = canned
        self.queries = []

    def rows(self, sql):
        self.queries.append(sql)
        if self._canned is not None:
            return iter(self._canned)
        return iter(self._rows_by_sql.get(sql, []))


def test_ids_sql_list_escapes_single_quote():
    assert _ids_sql_list(["b1", "o'brien"]) == "'b1', 'o''brien'"

def test_escape_sql_literal_wraps_and_escapes():
    assert _escape_sql_literal("b1") == "'b1'"
    assert _escape_sql_literal("o'brien") == "'o''brien'"

def test_hydrate_merges_wide_columns_and_coerces_decimal():
    fake = FakeKineticaSource(canned=[
        {"NODE": "b1", "name": "Acme", "amount": Decimal("10.5")},
        {"NODE": "b2", "name": "Beta", "amount": Decimal("3.0")},
    ])
    eng = KineticaComputeEngine(conn=None, _source_factory=lambda: fake)

    out = eng.hydrate([{"NODE": "b1", "risk": 1}, {"NODE": "b2", "risk": 2}],
                      "expero.vertexes", key="NODE")

    assert out[0]["NODE"] == "b1"
    assert out[0]["risk"] == 1          # original row field preserved
    assert out[0]["name"] == "Acme"     # hydrated field merged
    assert isinstance(out[0]["amount"], float)
    assert not isinstance(out[0]["amount"], Decimal)
    assert out[1]["name"] == "Beta"

def test_hydrate_escapes_id_with_single_quote_in_query():
    fake = FakeKineticaSource(canned=[])
    eng = KineticaComputeEngine(conn=None, _source_factory=lambda: fake)

    eng.hydrate([{"NODE": "o'brien", "risk": 1}], "expero.vertexes", key="NODE")

    assert len(fake.queries) == 1
    sql = fake.queries[0]
    assert "o''brien" in sql
    # the raw unescaped id must not appear verbatim (it would break out of
    # the string literal and be interpreted as SQL syntax)
    assert "'o'brien'" not in sql

def test_hydrate_drops_rows_with_null_key():
    fake = FakeKineticaSource(canned=[])
    eng = KineticaComputeEngine(conn=None, _source_factory=lambda: fake)

    out = eng.hydrate([{"NODE": None, "risk": 1}], "expero.vertexes", key="NODE")
    assert out == []

def test_hydrate_empty_rows_short_circuits():
    fake = FakeKineticaSource(canned=[])
    eng = KineticaComputeEngine(conn=None, _source_factory=lambda: fake)
    assert eng.hydrate([], "expero.vertexes") == []
    assert fake.queries == []

def test_run_sql_coerces_decimal():
    fake = FakeKineticaSource(canned=[{"c": Decimal("5")}])
    eng = KineticaComputeEngine(conn=None, _source_factory=lambda: fake)
    rows = eng.run_sql("SELECT COUNT(*) AS c FROM expero.vertexes")
    assert rows == [{"c": 5.0}]
    assert isinstance(rows[0]["c"], float)
