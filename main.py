"""Command-line orchestrator for the PSX research pipeline."""

import argparse
import logging
import sys
from typing import Any

from analysis.engine import analyze_all
from bot.telegram import format_report, send_report
from scrapers.news import scrape_news
from scrapers.psx import initialize_database, scrape_stocks

LOGGER = logging.getLogger(__name__)


def dummy_data() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Create deterministic offline data for Telegram and pipeline testing."""
    history = [
        {"date": f"day-{index}", "close": 5 + index * 0.02, "open": 4.98 + index * 0.02, "volume": 100_000 + index * 1_000}
        for index in range(240)
    ]
    stock = {
        "symbol": "TEST",
        "company_name": "PSX Test Company",
        "current_price": 9.78,
        "volume": 339_000,
        "change_percent": 1.2,
        "52_week_high": 10.0,
        "52_week_low": 4.5,
        "sector": "TEST",
        "shariah_status": "unknown",
        "data_quality": 1.0,
        "rs_rating": 85,
        "price_history": list(reversed(history)),
    }
    article = {
        "headline": "TEST wins major contract",
        "source": "Test Source",
        "url": "https://example.com/test",
        "published_date": "2026-06-09",
        "full_text": "PSX Test Company announced a major contract and expansion.",
        "mentioned_symbols": ["TEST"],
    }
    return [stock], [article]


def parse_args() -> argparse.Namespace:
    """Parse the supported discovery, monitoring, and test flags."""
    parser = argparse.ArgumentParser(description="Personal PSX stock research assistant")
    parser.add_argument("--mode", choices=("discovery", "monitoring"), default="discovery")
    parser.add_argument("--test", action="store_true", help="Use dummy data and send a Telegram test report")
    parser.add_argument("--no-ai", action="store_true", help="Use keyword catalyst scoring instead of GPT-5.4")
    return parser.parse_args()


def run(mode: str, test: bool = False, use_ai: bool = True) -> int:
    """Run the complete research pipeline and deliver its Telegram report."""
    initialize_database()
    if test:
        stocks, news = dummy_data()
        use_ai = False
    else:
        stocks = scrape_stocks()
        news = scrape_news([stock["symbol"] for stock in stocks])
    results = analyze_all(stocks, news, use_ai=use_ai)
    report = format_report(results, stocks, mode)
    print(report)
    if not send_report(report):
        LOGGER.error("Pipeline completed, but Telegram delivery failed")
        return 1
    return 0


def main() -> int:
    """Configure logging and execute the selected CLI workflow."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args()
    return run(args.mode, test=args.test, use_ai=not args.no_ai)


if __name__ == "__main__":
    sys.exit(main())
