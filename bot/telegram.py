"""Synchronous Telegram report formatting and delivery."""

import logging
import sqlite3
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

from config import DATABASE_PATH, REQUEST_TIMEOUT, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

LOGGER = logging.getLogger(__name__)


def _reason(result: dict[str, Any]) -> str:
    """Build a compact explanation from the strongest framework details."""
    details = result["framework_details"]
    return (
        f"{details['weinstein']['reason']}. "
        f"SEPA {details['minervini']['criteria_passed']}/5; "
        f"catalyst {details['catalyst']['score']}/2."
    )


def _active_positions() -> list[tuple[Any, ...]]:
    """Load open positions and their latest stored prices for monitoring."""
    if not DATABASE_PATH.exists():
        return []
    try:
        with sqlite3.connect(DATABASE_PATH) as connection:
            return connection.execute(
                """
                SELECT trades.symbol, trades.entry_price, trades.quantity,
                       stocks.current_price
                FROM trades
                LEFT JOIN stocks ON stocks.symbol = trades.symbol
                WHERE trades.closed_at IS NULL
                """
            ).fetchall()
    except sqlite3.Error as exc:
        LOGGER.warning("Could not load active positions: %s", exc)
        return []


def format_report(
    results: list[dict[str, Any]],
    stocks: list[dict[str, Any]],
    mode: str,
) -> str:
    """Render the end-of-day PSX analysis report."""
    date = datetime.now(ZoneInfo("Asia/Karachi")).strftime("%Y-%m-%d")
    lines = [
        f"PSX ANALYSIS REPORT - {date}",
        "Market closed at 3:30 PM PKT",
        f"MODE: {mode.upper()}",
        "",
        "TOP PICKS:",
    ]
    picks = [result for result in results if result["tier"] in (1, 2)]
    if not picks:
        lines.append("No stocks met the Tier 1 or Tier 2 thresholds.")
    for result in picks:
        marker = "🟢" if result["tier"] == 1 else "🟡"
        lines.extend(
            [
                f"{marker} TIER {result['tier']}: {result['symbol']} - {result['company_name']}",
                f"Price: Rs {result['current_price']:.2f} | Target: Rs {result['target_price']:.2f} | Stop: Rs {result['stop_loss']:.2f}",
                f"Frameworks: {result['frameworks_passed']}/5 passed",
                _reason(result),
                "",
            ]
        )
    lines.append("MONITORING:")
    positions = _active_positions()
    position_lines = []
    for symbol, entry, quantity, current in positions:
        status = (
            f"current Rs {current:.2f}, P/L {((current / entry) - 1) * 100:+.1f}%"
            if current is not None and entry
            else "current price unavailable"
        )
        position_lines.append(f"{symbol}: entry Rs {entry:.2f}, {status}, quantity {quantity or 0:g}")
    lines.extend(position_lines or ["No active positions recorded."])
    complete = sum(float(stock.get("data_quality", 0)) == 1.0 for stock in stocks)
    quality = (complete / len(stocks) * 100) if stocks else 0
    lines.extend(
        [
            "",
            f"Data quality: {quality:.0f}% of stocks had complete data",
            "Sources checked: PSX, Dawn, Profit PK, The News, ARY",
            "Shariah classification: Official PSX Notice N-1419, with SCS Trade fallback",
            "",
            "Research aid only. Human review required before any trade.",
        ]
    )
    return "\n".join(lines)


def send_report(report: str) -> bool:
    """Send a report through Telegram's HTTPS Bot API."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        LOGGER.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = [report[index : index + 4000] for index in range(0, len(report), 4000)]
    for chunk in chunks:
        try:
            response = requests.post(
                url,
                data={"chat_id": TELEGRAM_CHAT_ID, "text": chunk},
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            LOGGER.error("Telegram report delivery failed: %s", exc)
            return False
    return True
