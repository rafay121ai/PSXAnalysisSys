"""Standalone Sarmaaya fundamentals scraper.

Verified URL patterns (2026-06-11):
- Company page: https://sarmaaya.pk/stocks/{SYMBOL}
- Legacy https://sarmaaya.pk/psx/company/{SYMBOL} redirects to that page.
- The page's public JSON endpoints are rooted at
  https://beta-restapi.sarmaaya.pk/api/stocks/.

This module is intentionally not integrated into the analysis pipeline.
Missing or unparseable values remain None and are persisted as SQLite NULL.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

import requests

from config import DATABASE_PATH, REQUEST_TIMEOUT, SCHEMA_PATH, USER_AGENT

LOGGER = logging.getLogger(__name__)

SARMAAYA_PAGE_PATTERN = "https://sarmaaya.pk/stocks/{symbol}"
SARMAAYA_API_ROOT = "https://beta-restapi.sarmaaya.pk/api/stocks"
SOURCE = "sarmaaya.pk"
DEFAULT_DELAY_SECONDS = 2.5
DRY_RUN_SYMBOLS = ("KEL", "BECO", "CLOV", "UNITY", "TELE")
FUNDAMENTAL_FIELDS = (
    "pe_ratio",
    "eps_trailing",
    "market_cap",
    "shares_outstanding",
    "free_float_percent",
    "debt_to_equity",
    "book_value",
    "dividend_yield",
    "sector",
)

DETAIL_METRICS = {
    "FF_PE": "pe_ratio",
    "FF_EPS": "eps_trailing",
    "FF_MKT_CAP": "market_cap",
    "FF_COM_SHS_OUT": "shares_outstanding",
    "FF_SHS_FLOAT_PERCENT": "free_float_percent",
    "FF_DIV_YLD": "dividend_yield",
}
RATIO_METRICS = {
    "Basic EPS": "eps_trailing",
    "Debt to Equity": "debt_to_equity",
    "Book Value Per Share": "book_value",
}


class PoliteSession:
    """Requests session enforcing a minimum delay between all requests."""

    def __init__(self, delay_seconds: float = DEFAULT_DELAY_SECONDS) -> None:
        if delay_seconds < 2 or delay_seconds > 3:
            raise ValueError("delay_seconds must be between 2 and 3")
        self.delay_seconds = delay_seconds
        self.last_request_at: float | None = None
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Referer": "https://sarmaaya.pk/",
            }
        )

    def request_json(self, method: str, url: str, **kwargs: Any) -> Any:
        """Request one JSON endpoint after honoring the inter-request delay."""
        if self.last_request_at is not None:
            elapsed = time.monotonic() - self.last_request_at
            time.sleep(max(0, self.delay_seconds - elapsed))
        try:
            # Sarmaaya's route cookie can pin public API calls to a backend that
            # responds 401; each company-page request is otherwise stateless.
            self.session.cookies.clear()
            response = self.session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
            response.raise_for_status()
            payload = response.json()
            if not payload.get("success"):
                raise ValueError(payload.get("message") or "Sarmaaya returned an unsuccessful response")
            return payload.get("response")
        finally:
            self.last_request_at = time.monotonic()


def _number(value: Any) -> float | None:
    """Parse a source value without converting missing data to zero."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    normalized = str(value).strip().replace(",", "").replace("%", "")
    if not normalized or normalized in {"-", "N/A", "null", "None"}:
        return None
    if normalized.startswith("(") and normalized.endswith(")"):
        normalized = f"-{normalized[1:-1]}"
    try:
        return float(normalized)
    except ValueError:
        return None


def _empty_record(symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol.strip().upper(),
        **dict.fromkeys(FUNDAMENTAL_FIELDS),
        "source": SOURCE,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }


def _extract_details(
    details: list[dict[str, Any]],
    record: dict[str, Any],
    field_dates: dict[str, str | None],
) -> str | None:
    """Extract snapshot metrics and return the ISIN needed for ratio lookup."""
    isin = None
    for metric in details or []:
        isin = isin or metric.get("isin")
        field = DETAIL_METRICS.get(metric.get("metricMetric"))
        if field and record[field] is None:
            record[field] = _number(metric.get("afValue"))
            field_dates[field] = _clean_date(metric.get("afFiscalEndDate"))
    return isin


def _extract_ratios(
    ratios: dict[str, Any],
    record: dict[str, Any],
    field_dates: dict[str, str | None],
) -> None:
    """Extract the latest explicit ratio values, preserving legitimate zeros."""
    for metric_name, field in RATIO_METRICS.items():
        entries = (ratios or {}).get(metric_name, {}).get("data", [])
        for entry in entries:
            value = _number(entry.get("value"))
            # Sarmaaya ratio histories use zero as a missing-period placeholder.
            # Never interpret an uncorroborated zero debt ratio as debt-free.
            if field == "debt_to_equity" and value == 0:
                break
            if value is not None:
                record[field] = value
                field_dates[field] = _clean_date(entry.get("date"))
                break


def _clean_date(value: Any) -> str | None:
    """Normalize source date strings enough for freshness inspection."""
    if value is None or str(value).strip() in {"", "null", "None"}:
        return None
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass
    for pattern in (
        "%a %b %d %Y %H:%M:%S GMT%z (Coordinated Universal Time)",
        "%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)",
    ):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    return text


def scrape_symbol(client: PoliteSession, symbol: str) -> tuple[dict[str, Any], dict[str, str | None]]:
    """Scrape one symbol; endpoint failures leave affected fields as None."""
    normalized_symbol = symbol.strip().upper()
    record = _empty_record(normalized_symbol)
    field_dates = dict.fromkeys(FUNDAMENTAL_FIELDS)
    isin = None

    try:
        details = client.request_json(
            "POST",
            f"{SARMAAYA_API_ROOT}/details/{normalized_symbol}",
        )
        isin = _extract_details(details, record, field_dates)
    except (requests.RequestException, ValueError) as exc:
        LOGGER.warning("%s details failed: %s", normalized_symbol, exc)

    try:
        about = client.request_json(
            "GET",
            f"{SARMAAYA_API_ROOT}/about",
            params={"symbol": normalized_symbol},
        )
        if about:
            record["sector"] = about.get("sector") or None
    except (requests.RequestException, ValueError) as exc:
        LOGGER.warning("%s company metadata failed: %s", normalized_symbol, exc)

    if isin:
        try:
            ratios = client.request_json(
                "GET",
                f"{SARMAAYA_API_ROOT}/fundamentals/ratios",
                params={"isin": isin, "periodicity": "LTM"},
            )
            _extract_ratios(ratios, record, field_dates)
        except (requests.RequestException, ValueError) as exc:
            LOGGER.warning("%s ratios failed: %s", normalized_symbol, exc)
    else:
        LOGGER.warning("%s has no ISIN; ratio lookup skipped", normalized_symbol)

    return record, field_dates


def initialize_database() -> None:
    """Create the shared schema without invoking any pipeline module."""
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def upsert_fundamental(record: dict[str, Any]) -> None:
    """Upsert one complete scrape record by symbol."""
    columns = ("symbol", *FUNDAMENTAL_FIELDS, "source", "scraped_at")
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(f"{column} = excluded.{column}" for column in columns if column != "symbol")
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(
            f"""
            INSERT INTO fundamentals ({", ".join(columns)})
            VALUES ({placeholders})
            ON CONFLICT(symbol) DO UPDATE SET {updates}
            """,
            tuple(record[column] for column in columns),
        )


def scrape_fundamentals(
    symbols: list[str] | tuple[str, ...],
    delay_seconds: float = DEFAULT_DELAY_SECONDS,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str | None]]]:
    """Scrape and persist a batch without allowing one symbol to stop it."""
    initialize_database()
    client = PoliteSession(delay_seconds)
    records: list[dict[str, Any]] = []
    freshness: dict[str, dict[str, str | None]] = {}
    for symbol in symbols:
        normalized_symbol = symbol.strip().upper()
        try:
            record, field_dates = scrape_symbol(client, normalized_symbol)
            upsert_fundamental(record)
            records.append(record)
            freshness[normalized_symbol] = field_dates
        except Exception:
            LOGGER.exception("%s failed unexpectedly; continuing batch", normalized_symbol)
    log_coverage_summary(records)
    return records, freshness


def coverage(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return per-symbol found/NULL counts."""
    return [
        {
            "symbol": record["symbol"],
            "fields_found": sum(record[field] is not None for field in FUNDAMENTAL_FIELDS),
            "fields_null": sum(record[field] is None for field in FUNDAMENTAL_FIELDS),
        }
        for record in records
    ]


def log_coverage_summary(records: list[dict[str, Any]]) -> None:
    """Log the required per-symbol coverage summary."""
    for row in coverage(records):
        LOGGER.info(
            "%s coverage: %s fields found / %s fields NULL",
            row["symbol"],
            row["fields_found"],
            row["fields_null"],
        )


def print_dry_run(
    records: list[dict[str, Any]],
    freshness: dict[str, dict[str, str | None]],
) -> None:
    """Print full records, field dates, and a compact coverage table."""
    for record in records:
        print(json.dumps(record, indent=2, sort_keys=True))
        print(
            json.dumps(
                {"symbol": record["symbol"], "field_dates": freshness[record["symbol"]]},
                indent=2,
                sort_keys=True,
            )
        )
    print("\nCOVERAGE")
    print(f"{'SYMBOL':<8} {'FOUND':>7} {'NULL':>7}")
    for row in coverage(records):
        print(f"{row['symbol']:<8} {row['fields_found']:>7} {row['fields_null']:>7}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Standalone Sarmaaya fundamentals scraper")
    parser.add_argument("symbols", nargs="*", default=list(DRY_RUN_SYMBOLS))
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_SECONDS)
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args()
    records, freshness = scrape_fundamentals(args.symbols, args.delay)
    print_dry_run(records, freshness)
    return 0 if len(records) == len(args.symbols) else 1


if __name__ == "__main__":
    raise SystemExit(main())
