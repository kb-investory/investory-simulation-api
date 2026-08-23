import unittest
from datetime import date, datetime

from app.modules.simulation.persistence.repository import SimulationRepository


class FakeCursor:
    """Stands in for a pymysql cursor across load_overview()'s queries.

    Distinguishes which query just ran by a distinctive substring, since the
    real code issues them sequentially on one cursor.
    """

    def __init__(
        self,
        journal_row=(None, None, 0),
        connected_count=1,
        simulation_count=0,
        price_summary=(date(2024, 1, 2), date(2026, 8, 21), 642, 30),
        first_snapshot=None,
        first_trade=None,
        first_runnable_after=None,
    ):
        self.journal_row = journal_row
        self.connected_count = connected_count
        self.simulation_count = simulation_count
        self.price_summary = price_summary
        self.first_snapshot = first_snapshot
        self.first_trade = first_trade
        self.first_runnable_after = first_runnable_after
        self.last_query = ""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        self.last_query = " ".join(str(query).split())

    def fetchone(self):
        q = self.last_query
        if "investment_journals" in q:
            return self.journal_row
        if "broker_connections" in q:
            return (self.connected_count,)
        if "FROM simulation_runs" in q:
            return (self.simulation_count,)
        if "COUNT(DISTINCT security_id)" in q:
            return self.price_summary
        if "holding_snapshots" in q:
            return (self.first_snapshot,)
        if "MIN(traded_at)" in q and "FROM trades" in q:
            return (self.first_trade,)
        if "price_date > %s" in q:
            return (self.first_runnable_after,)
        return None


class FakeConnection:
    def __init__(self, **cursor_kwargs):
        self.fake_cursor = FakeCursor(**cursor_kwargs)

    def cursor(self):
        return self.fake_cursor

    def close(self):
        pass


class OverviewEligibleStartTests(unittest.TestCase):
    def _repository(self, **cursor_kwargs) -> SimulationRepository:
        connection = FakeConnection(**cursor_kwargs)
        return SimulationRepository(connection_factory=lambda: connection)

    def test_no_journal_no_snapshot_but_has_trades_still_resolves_a_start_date(self):
        """The exact account 5221 shape from #16 — journal-less, snapshot-less, real trades."""
        repository = self._repository(
            journal_row=(None, None, 0),
            first_snapshot=None,
            first_trade=datetime(2026, 6, 1, 9, 30, 0),
            first_runnable_after=date(2026, 6, 2),
        )

        overview = repository.load_overview(user_id=1, account_id=5221)

        self.assertEqual(overview["eligibleStartDate"], "2026-06-02")

    def test_journal_only_account_is_unaffected(self):
        repository = self._repository(
            journal_row=(date(2026, 1, 5), date(2026, 8, 1), 40),
            first_snapshot=None,
            first_trade=None,
        )

        overview = repository.load_overview(user_id=1, account_id=1)

        self.assertEqual(overview["eligibleStartDate"], "2026-01-05")

    def test_no_journal_no_snapshot_no_trades_stays_none(self):
        repository = self._repository(
            journal_row=(None, None, 0),
            first_snapshot=None,
            first_trade=None,
        )

        overview = repository.load_overview(user_id=1, account_id=2)

        self.assertIsNone(overview["eligibleStartDate"])

    def test_earlier_of_snapshot_and_trade_wins_as_anchor(self):
        """Snapshot and trade dates are different column types (date vs datetime) —
        this exercises that the min() comparison normalizes both correctly."""
        repository = self._repository(
            journal_row=(None, None, 0),
            first_snapshot=date(2026, 7, 1),
            first_trade=datetime(2026, 5, 1, 10, 0, 0),  # earlier than the snapshot
            first_runnable_after=date(2026, 5, 4),
        )

        overview = repository.load_overview(user_id=1, account_id=3)

        self.assertEqual(overview["eligibleStartDate"], "2026-05-04")


if __name__ == "__main__":
    unittest.main()
