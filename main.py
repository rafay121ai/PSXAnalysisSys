"""Command-line orchestrator for the PSX research pipeline."""

import argparse
import logging
import sys
import time
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
    started_at = time.monotonic()
    LOGGER.info("Starting PSX pipeline in %s mode%s", mode, " with dummy data" if test else "")
    initialize_database()
    if test:
        stocks, news = dummy_data()
        use_ai = False
    else:
        LOGGER.info("Stage 1/4: scraping PSX stocks")
        stocks = scrape_stocks()
        LOGGER.info("Stage 1/4 complete: %s qualifying stocks", len(stocks))
        LOGGER.info("Stage 2/4: scraping company news")
        news = scrape_news([stock["symbol"] for stock in stocks])
        LOGGER.info("Stage 2/4 complete: %s articles", len(news))
    LOGGER.info("Stage 3/4: analyzing %s stocks", len(stocks))
    results = analyze_all(stocks, news, use_ai=use_ai)
    LOGGER.info("Stage 3/4 complete: %s actionable picks", sum(result["tier"] > 0 for result in results))
    report = format_report(results, stocks, mode)
    print(report)
    LOGGER.info("Stage 4/4: sending Telegram report")
    if not send_report(report):
        LOGGER.error("Pipeline completed, but Telegram delivery failed")
        return 1
    LOGGER.info("Pipeline completed successfully in %.1f minutes", (time.monotonic() - started_at) / 60)
    return 0


def main() -> int:
    """Configure logging and execute the selected CLI workflow."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    args = parse_args()
    return run(args.mode, test=args.test, use_ai=not args.no_ai)


if __name__ == "__main__":
    sys.exit(main())
