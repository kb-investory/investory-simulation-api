import unittest

from app.modules.simulation.persistence.repository import (
    SimulationDataError,
    SimulationRepository,
)


class FakeCursor:
    """Stands in for a pymysql cursor across the reconstruction's 3 queries.

    Distinguishes which query just ran by a distinctive substring, since the
    real code issues them sequentially on one cursor (execute immediately
    followed by its own fetch, never interleaved).
    """

    def __init__(self, snapshot_max_date=None, buy_lot_rows=None, unmatched_sell_count=0, price_rows=None):
        self.snapshot_max_date = snapshot_max_date
        self.buy_lot_rows = buy_lot_rows or []
        self.unmatched_sell_count = unmatched_sell_count
        self.price_rows = price_rows or []
        self.last_query = ""
        self.executions = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        self.last_query = " ".join(str(query).split())
        self.executions.append((self.last_query, params))

    def fetchone(self):
        if "MAX(snapshot_date)" in self.last_query:
            return (self.snapshot_max_date,)
        if "COUNT(*)" in self.last_query and "trade_side = 'SELL'" in self.last_query:
            return (self.unmatched_sell_count,)
        return None

    def fetchall(self):
        if "trade_side = 'BUY'" in self.last_query:
            return self.buy_lot_rows
        if "security_daily_prices" in self.last_query:
            return self.price_rows
        return []


class FakeConnection:
    def __init__(self, **cursor_kwargs):
        self.fake_cursor = FakeCursor(**cursor_kwargs)

    def cursor(self):
        return self.fake_cursor

    def close(self):
        pass


class InitialSnapshotReconstructionTests(unittest.TestCase):
    def _repository(self, **cursor_kwargs) -> SimulationRepository:
        connection = FakeConnection(**cursor_kwargs)
        return SimulationRepository(connection_factory=lambda: connection)

    def test_reconstructs_from_complete_trade_history(self):
        repository = self._repository(
            snapshot_max_date=None,
            buy_lot_rows=[(101, "005930", "삼성전자", 10.0, 70000.0, 10.0)],
            unmatched_sell_count=0,
            price_rows=[(101, 75000.0)],
        )

        result = repository.load_initial_snapshot(12, "2026-05-12")

        self.assertEqual(result["calculationPolicy"], "RECONSTRUCTED_FROM_TRADE_MATCHES")
        self.assertEqual(result["unmatchedSellCount"], 0)
        self.assertEqual(result["initialCapital"], 750000.0)
        self.assertEqual(len(result["holdings"]), 1)
        holding = result["holdings"][0]
        self.assertEqual(holding["marketValueQuality"], "ACTUAL_CLOSE_PRICE")
        self.assertEqual(holding["quantity"], 10.0)
        self.assertEqual(holding["averageCost"], 70000.0)
        self.assertEqual(holding["marketValue"], 750000.0)
        self.assertEqual(holding["unrealizedPnl"], 50000.0)
        # Not a real snapshot date, but must be a valid ISO date strictly
        # before period_start so capital_calculator's own check still holds.
        self.assertEqual(result["snapshotDate"], "2026-05-11")

    def test_multiple_lots_of_the_same_security_are_weighted_averaged(self):
        repository = self._repository(
            snapshot_max_date=None,
            buy_lot_rows=[
                (101, "005930", "삼성전자", 5.0, 60000.0, 5.0),
                (101, "005930", "삼성전자", 5.0, 80000.0, 5.0),
            ],
            unmatched_sell_count=0,
            price_rows=[(101, 75000.0)],
        )

        result = repository.load_initial_snapshot(12, "2026-05-12")

        holding = result["holdings"][0]
        self.assertEqual(holding["quantity"], 10.0)
        self.assertEqual(holding["averageCost"], 70000.0)  # (5*60000 + 5*80000) / 10

    def test_flags_unmatched_sells_without_blocking(self):
        repository = self._repository(
            snapshot_max_date=None,
            buy_lot_rows=[(101, "005930", "삼성전자", 10.0, 70000.0, 10.0)],
            unmatched_sell_count=3,
            price_rows=[(101, 75000.0)],
        )

        result = repository.load_initial_snapshot(12, "2026-05-12")

        self.assertEqual(result["unmatchedSellCount"], 3)
        self.assertEqual(result["calculationPolicy"], "RECONSTRUCTED_FROM_TRADE_MATCHES")

    def test_falls_back_to_cost_basis_when_no_price_available(self):
        repository = self._repository(
            snapshot_max_date=None,
            buy_lot_rows=[(202, "000660", "SK하이닉스", 5.0, 30000.0, 5.0)],
            unmatched_sell_count=0,
            price_rows=[],
        )

        result = repository.load_initial_snapshot(12, "2026-05-12")

        holding = result["holdings"][0]
        self.assertEqual(holding["marketValueQuality"], "PRICE_UNAVAILABLE_COST_BASIS_FALLBACK")
        self.assertEqual(holding["marketValue"], 150000.0)
        self.assertEqual(holding["unrealizedPnl"], 0.0)

    def test_no_trades_at_all_raises_initial_holdings_not_found(self):
        repository = self._repository(
            snapshot_max_date=None,
            buy_lot_rows=[],
        )

        with self.assertRaises(SimulationDataError) as context:
            repository.load_initial_snapshot(12, "2026-05-12")

        self.assertEqual(context.exception.code, "INITIAL_HOLDINGS_NOT_FOUND")

    def test_fully_sold_lots_raise_initial_capital_empty(self):
        repository = self._repository(
            snapshot_max_date=None,
            buy_lot_rows=[(101, "005930", "삼성전자", 10.0, 70000.0, 0.0)],
        )

        with self.assertRaises(SimulationDataError) as context:
            repository.load_initial_snapshot(12, "2026-05-12")

        self.assertEqual(context.exception.code, "INITIAL_CAPITAL_EMPTY")

    def test_stored_snapshot_takes_priority_over_reconstruction(self):
        connection = FakeConnection(snapshot_max_date="2026-05-10")
        # A holding row for the snapshot-found branch's second query.
        connection.fake_cursor.buy_lot_rows = [(101, "005930", "삼성전자", 10.0, 70000.0, 700000.0, 50000.0)]
        repository = SimulationRepository(connection_factory=lambda: connection)

        def fetchall_for_snapshot():
            if "FROM holding_snapshots h" in connection.fake_cursor.last_query:
                return [(101, "005930", "삼성전자", 10.0, 70000.0, 750000.0, 50000.0)]
            return connection.fake_cursor.buy_lot_rows

        connection.fake_cursor.fetchall = fetchall_for_snapshot

        result = repository.load_initial_snapshot(12, "2026-05-12")

        self.assertEqual(result["calculationPolicy"], "PREVIOUS_TRADING_DAY_HOLDING_SNAPSHOT")
        self.assertEqual(result["snapshotDate"], "2026-05-10")
        self.assertEqual(result["holdings"][0]["marketValueQuality"], "STORED_SNAPSHOT")


if __name__ == "__main__":
    unittest.main()
