"""Standalone trade logging and FIFO closed-trade statistics."""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date
from math import isfinite
from pathlib import Path
from typing import Iterable

from config import DATABASE_PATH

TRADE_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    qty REAL NOT NULL CHECK (qty > 0),
    price REAL NOT NULL CHECK (price > 0),
    value REAL NOT NULL CHECK (value > 0),
    trade_date TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('manual', 'system_signal')),
    exit_reason TEXT,
    linked_recommendation_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS trade_log_fifo_idx
    ON trade_log(symbol, trade_date, id);
"""

SEED_TRADES = (
    ("CLOV", "BUY", 960, 8.20, "2026-05-21"),
    ("CLOV", "SELL", 960, 8.49, "2026-05-21"),
    ("BECO", "BUY", 1200, 5.32, "2026-05-21"),
    ("BECO", "SELL", 1200, 5.41, "2026-05-21"),
    ("TOMCL", "BUY", 107, 33.87, "2026-05-25"),
    ("TOMCL", "SELL", 107, 34.90, "2026-05-25"),
    ("TOMCL", "BUY", 143, 33.88, "2026-05-25"),
    ("TOMCL", "SELL", 143, 34.90, "2026-05-25"),
    ("PRL", "BUY", 169, 34.97, "2026-05-25"),
    ("PRL", "SELL", 169, 35.40, "2026-05-25"),
    ("GCIL", "BUY", 275, 28.63, "2026-06-02"),
    ("GCIL", "SELL", 275, 30.31, "2026-06-02"),
)

SYMBOL_PATTERN = re.compile(r"^[A-Z0-9.-]{1,20}$")


@dataclass(frozen=True)
class ClosedTrade:
    """A FIFO-matched buy lot, or portion of one, closed by a sell."""

    symbol: str
    qty: float
    buy_price: float
    sell_price: float

    @property
    def return_percent(self) -> float:
        return ((self.sell_price / self.buy_price) - 1) * 100


@dataclass(frozen=True)
class TradeStats:
    """Summary metrics computed only from FIFO-matched closed trades."""

    closed_trades: int
    wins: int
    losses: int
    win_rate: float
    average_win_percent: float
    average_loss_percent: float
    expectancy_percent: float


def initialize_trade_log(database_path: Path = DATABASE_PATH) -> None:
    """Create the trade log and insert the requested seed trades once."""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path) as connection:
        connection.executescript(TRADE_LOG_SCHEMA)
        for symbol, side, qty, price, trade_date in SEED_TRADES:
            connection.execute(
                """
                INSERT INTO trade_log
                    (symbol, side, qty, price, value, trade_date, source)
                SELECT ?, ?, ?, ?, ?, ?, 'manual'
                WHERE NOT EXISTS (
                    SELECT 1 FROM trade_log
                    WHERE symbol = ? AND side = ? AND qty = ? AND price = ?
                      AND trade_date = ? AND source = 'manual'
                )
                """,
                (
                    symbol,
                    side,
                    qty,
                    price,
                    qty * price,
                    trade_date,
                    symbol,
                    side,
                    qty,
                    price,
                    trade_date,
                ),
            )


def validate_symbol(symbol: str) -> str:
    """Normalize and validate a PSX-style trading symbol."""
    normalized = symbol.strip().upper()
    if not SYMBOL_PATTERN.fullmatch(normalized):
        raise ValueError("symbol must be 1-20 letters, numbers, dots, or hyphens")
    return normalized


def validate_positive_number(raw_value: str | float, field_name: str) -> float:
    """Parse a finite, strictly positive number."""
    try:
        value = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a number greater than zero") from exc
    if value <= 0 or not isfinite(value):
        raise ValueError(f"{field_name} must be a number greater than zero")
    return value


def validate_trade_date(raw_date: str | None) -> str:
    """Return an ISO trade date, defaulting to today's local date."""
    if raw_date is None:
        return date.today().isoformat()
    try:
        return date.fromisoformat(raw_date).isoformat()
    except ValueError as exc:
        raise ValueError("date must use YYYY-MM-DD") from exc


def log_trade(
    symbol: str,
    side: str,
    qty: float,
    price: float,
    trade_date: str | None = None,
    *,
    source: str = "manual",
    exit_reason: str | None = None,
    linked_recommendation_id: int | None = None,
    database_path: Path = DATABASE_PATH,
) -> int:
    """Validate and persist one buy or sell."""
    normalized_symbol = validate_symbol(symbol)
    normalized_side = side.strip().upper()
    if normalized_side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    normalized_qty = validate_positive_number(qty, "qty")
    normalized_price = validate_positive_number(price, "price")
    if source not in {"manual", "system_signal"}:
        raise ValueError("source must be manual or system_signal")
    normalized_date = validate_trade_date(trade_date)
    initialize_trade_log(database_path)
    with sqlite3.connect(database_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO trade_log
                (symbol, side, qty, price, value, trade_date, source,
                 exit_reason, linked_recommendation_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized_symbol,
                normalized_side,
                normalized_qty,
                normalized_price,
                normalized_qty * normalized_price,
                normalized_date,
                source,
                exit_reason,
                linked_recommendation_id,
            ),
        )
        return int(cursor.lastrowid)


def list_trades(limit: int = 10, database_path: Path = DATABASE_PATH) -> list[sqlite3.Row]:
    """Return the most recently logged trades."""
    if limit <= 0:
        raise ValueError("limit must be greater than zero")
    initialize_trade_log(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            """
            SELECT id, symbol, side, qty, price, value, trade_date, source,
                   exit_reason, linked_recommendation_id, created_at
            FROM trade_log
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def fifo_closed_trades(database_path: Path = DATABASE_PATH) -> list[ClosedTrade]:
    """Match buys to sells by symbol using FIFO and ignore unmatched quantities."""
    initialize_trade_log(database_path)
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT symbol, side, qty, price
            FROM trade_log
            ORDER BY trade_date, id
            """
        ).fetchall()
    open_buys: dict[str, deque[list[float]]] = defaultdict(deque)
    closed: list[ClosedTrade] = []
    for symbol, side, qty, price in rows:
        remaining = float(qty)
        if side == "BUY":
            open_buys[symbol].append([remaining, float(price)])
            continue
        while remaining > 0 and open_buys[symbol]:
            buy_lot = open_buys[symbol][0]
            matched_qty = min(remaining, buy_lot[0])
            closed.append(ClosedTrade(symbol, matched_qty, buy_lot[1], float(price)))
            remaining -= matched_qty
            buy_lot[0] -= matched_qty
            if buy_lot[0] <= 1e-12:
                open_buys[symbol].popleft()
    return closed


def calculate_stats(database_path: Path = DATABASE_PATH) -> TradeStats:
    """Calculate win/loss metrics from FIFO-matched closed trades."""
    returns = [trade.return_percent for trade in fifo_closed_trades(database_path)]
    wins = [value for value in returns if value > 0]
    losses = [value for value in returns if value < 0]
    closed_count = len(returns)
    win_rate = (len(wins) / closed_count * 100) if closed_count else 0.0
    average_win = sum(wins) / len(wins) if wins else 0.0
    average_loss = sum(losses) / len(losses) if losses else 0.0
    loss_rate = len(losses) / closed_count if closed_count else 0.0
    expectancy = (win_rate / 100 * average_win) + (loss_rate * average_loss)
    return TradeStats(
        closed_trades=closed_count,
        wins=len(wins),
        losses=len(losses),
        win_rate=win_rate,
        average_win_percent=average_win,
        average_loss_percent=average_loss,
        expectancy_percent=expectancy,
    )


def format_trade_list(trades: Iterable[sqlite3.Row]) -> str:
    """Format recent trades for Telegram."""
    rows = list(trades)
    if not rows:
        return "No trades logged."
    lines = ["LAST 10 TRADES"]
    for trade in rows:
        lines.append(
            f"#{trade['id']} {trade['trade_date']} {trade['side']} "
            f"{trade['symbol']} {trade['qty']:g} @ Rs {trade['price']:.2f} "
            f"= Rs {trade['value']:.2f}"
        )
    return "\n".join(lines)


def format_stats(stats: TradeStats) -> str:
    """Format closed-trade statistics and the Kelly data guardrail."""
    lines = [
        "CLOSED TRADE STATS (FIFO)",
        f"Closed trades: {stats.closed_trades} ({stats.wins} wins, {stats.losses} losses)",
        f"Win rate: {stats.win_rate:.2f}%",
        f"Average win: {stats.average_win_percent:+.2f}%",
        f"Average loss: {stats.average_loss_percent:+.2f}%",
        f"Expectancy: {stats.expectancy_percent:+.2f}%",
    ]
    if stats.closed_trades < 30 or stats.losses == 0:
        lines.extend(
            [
                "",
                "INSUFFICIENT DATA FOR KELLY (need 30+ closed trades incl. losses)",
            ]
        )
    return "\n".join(lines)
