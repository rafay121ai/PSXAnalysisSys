"""Focused tests for standalone trade logging."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from trade_log import (
    TradeStats,
    calculate_stats,
    format_stats,
    initialize_trade_log,
    list_trades,
    log_trade,
    validate_positive_number,
)


class TradeLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "trades.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_seed_is_idempotent_and_produces_six_closed_wins(self) -> None:
        initialize_trade_log(self.database_path)
        initialize_trade_log(self.database_path)

        with sqlite3.connect(self.database_path) as connection:
            count = connection.execute("SELECT COUNT(*) FROM trade_log").fetchone()[0]

        stats = calculate_stats(self.database_path)
        self.assertEqual(count, 12)
        self.assertEqual(stats.closed_trades, 6)
        self.assertEqual(stats.wins, 6)
        self.assertEqual(stats.losses, 0)
        self.assertIn("INSUFFICIENT DATA FOR KELLY", format_stats(stats))

    def test_fifo_matching_uses_oldest_buy_and_ignores_open_quantity(self) -> None:
        initialize_trade_log(self.database_path)
        log_trade("TEST", "BUY", 10, 10, "2026-06-01", database_path=self.database_path)
        log_trade("TEST", "BUY", 10, 20, "2026-06-02", database_path=self.database_path)
        log_trade("TEST", "SELL", 15, 15, "2026-06-03", database_path=self.database_path)

        stats = calculate_stats(self.database_path)
        self.assertEqual(stats.closed_trades, 8)
        self.assertEqual(stats.wins, 7)
        self.assertEqual(stats.losses, 1)

    def test_rejects_non_positive_and_invalid_values(self) -> None:
        for value in ("0", "-1", "nan", "inf", "nope"):
            with self.assertRaises(ValueError):
                validate_positive_number(value, "qty")
        with self.assertRaises(ValueError):
            log_trade("TEST", "BUY", -1, 2, database_path=self.database_path)

    def test_recent_trades_are_newest_first(self) -> None:
        initialize_trade_log(self.database_path)
        trade_id = log_trade("TEST", "BUY", 2, 3, "2026-06-10", database_path=self.database_path)

        trades = list_trades(1, self.database_path)
        self.assertEqual(trades[0]["id"], trade_id)
        self.assertEqual(trades[0]["value"], 6)

    def test_kelly_warning_clears_only_with_30_closed_trades_and_losses(self) -> None:
        stats = TradeStats(30, 20, 10, 66.67, 5.0, -2.0, 2.67)

        self.assertNotIn("INSUFFICIENT DATA FOR KELLY", format_stats(stats))


if __name__ == "__main__":
    unittest.main()
