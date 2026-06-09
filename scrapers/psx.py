"""Synchronous PSX stock and price-history scraper."""

import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

import requests
from bs4 import BeautifulSoup

from config import (
    DATABASE_PATH,
    MAX_STOCK_PRICE,
    MIN_STOCK_PRICE,
    PSX_BASE_URL,
    REQUEST_DELAY,
    REQUEST_TIMEOUT,
    SCHEMA_PATH,
    SHARIAH_DISCLOSURE_PDF,
    SHARIAH_SOURCE_URL,
    USER_AGENT,
)

LOGGER = logging.getLogger(__name__)


def initialize_database() -> None:
    """Create all application tables from the shared SQLite schema."""
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def _number(value: str | None) -> float | None:
    """Convert formatted PSX text to a float, returning None when absent."""
    if not value:
        return None
    match = re.search(r"-?[\d,]+(?:\.\d+)?", value)
    return float(match.group(0).replace(",", "")) if match else None


def _request(session: requests.Session, url: str) -> requests.Response | None:
    """Fetch a URL with retries, throttling, and non-fatal error handling."""
    for attempt in range(1, 4):
        try:
            time.sleep(REQUEST_DELAY)
            response = session.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            LOGGER.warning("PSX request failed (%s/3) %s: %s", attempt, url, exc)
    return None


def fetch_symbols(session: requests.Session) -> list[dict[str, Any]]:
    """Return listed ordinary equities from the PSX symbols endpoint."""
    response = _request(session, f"{PSX_BASE_URL}/symbols")
    if not response:
        return []
    try:
        symbols = response.json()
        return [
            item
            for item in symbols
            if not item.get("isDebt") and not item.get("isETF") and item.get("symbol")
        ]
    except (requests.JSONDecodeError, TypeError) as exc:
        LOGGER.error("Could not parse PSX symbols response: %s", exc)
        return []


def fetch_shariah_symbols(session: requests.Session) -> set[str] | None:
    """Return the SCS Trade Shariah-compliant symbol set, or None on failure."""
    for attempt in range(1, 4):
        try:
            time.sleep(REQUEST_DELAY)
            response = session.post(
                SHARIAH_SOURCE_URL,
                json={"id": "-112"},
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            outer = response.json()
            rows = json.loads(outer["d"]).get("dt", [])
            symbols = {
                str(row["Symbol"]).strip().upper()
                for row in rows
                if row.get("Symbol")
            }
            if not symbols:
                raise ValueError("SCS Trade returned an empty Shariah symbol list")
            LOGGER.info("Loaded %s Shariah-compliant symbols from SCS Trade", len(symbols))
            return symbols
        except (requests.RequestException, requests.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            LOGGER.warning(
                "SCS Trade Shariah request failed (%s/3): %s",
                attempt,
                exc,
            )
    return None


def load_official_shariah_statuses() -> dict[str, str] | None:
    """Load explicit compliant and non-compliant symbols from the PSX notice PDF."""
    if not SHARIAH_DISCLOSURE_PDF.exists():
        LOGGER.warning("Official PSX Shariah disclosure PDF not found: %s", SHARIAH_DISCLOSURE_PDF)
        return None
    try:
        from pypdf import PdfReader

        text = "\n".join(page.extract_text() or "" for page in PdfReader(SHARIAH_DISCLOSURE_PDF).pages)
        matches = re.findall(
            r"^\s*\d+\s+([A-Z0-9-]+)\s+.+?\s+(Non-Compliant|Compliant)\s*$",
            text,
            re.MULTILINE,
        )
        statuses = {
            symbol.upper(): status.lower()
            for symbol, status in matches
        }
        if not statuses:
            raise ValueError("No Shariah classifications found in the official PSX notice")
        LOGGER.info(
            "Loaded %s compliant and %s non-compliant symbols from official PSX notice",
            sum(status == "compliant" for status in statuses.values()),
            sum(status == "non-compliant" for status in statuses.values()),
        )
        return statuses
    except (ImportError, OSError, ValueError) as exc:
        LOGGER.warning("Could not parse official PSX Shariah disclosure PDF: %s", exc)
        return None


def get_shariah_statuses(session: requests.Session) -> dict[str, str]:
    """Return official classifications, falling back to positive SCS confirmations."""
    official = load_official_shariah_statuses()
    if official is not None:
        return official
    scs_symbols = fetch_shariah_symbols(session)
    return {symbol: "compliant" for symbol in scs_symbols or set()}


def fetch_history(session: requests.Session, symbol: str) -> list[dict[str, Any]]:
    """Return PSX EOD history normalized into dated OHLC-lite records."""
    response = _request(session, f"{PSX_BASE_URL}/timeseries/eod/{symbol}")
    if not response:
        return []
    try:
        rows = response.json().get("data", [])
        return [
            {
                "date": datetime.fromtimestamp(row[0], tz=timezone.utc).date().isoformat(),
                "close": float(row[1]),
                "volume": int(row[2]),
                "open": float(row[3]),
            }
            for row in rows
            if isinstance(row, list) and len(row) >= 4
        ]
    except (ValueError, TypeError, requests.JSONDecodeError) as exc:
        LOGGER.warning("Could not parse history for %s: %s", symbol, exc)
        return []


def parse_company_page(
    html: str,
    symbol_info: dict[str, Any],
    shariah_status: str = "unknown",
) -> dict[str, Any]:
    """Extract the quote fields exposed by a PSX company HTML page."""
    soup = BeautifulSoup(html, "html.parser")
    stats = soup.select_one(".quote__stats")
    stats_text = stats.get_text(" ", strip=True) if stats else ""
    page_text = soup.get_text(" ", strip=True)

    range_match = re.search(
        r"52-WEEK RANGE\s*\^?\s*([\d,.]+)\s*[—-]\s*([\d,.]+)",
        stats_text,
        re.IGNORECASE,
    )
    volume_match = re.search(r"\bVolume\s+([\d,]+)", stats_text, re.IGNORECASE)
    change_node = soup.select_one(".change__percent")
    non_compliant = bool(
        re.search(r"\b(non[- ]shariah|shariah non[- ]compliant)\b", page_text, re.I)
    )
    stock = {
        "symbol": symbol_info["symbol"],
        "company_name": (
            soup.select_one(".quote__name").get_text(strip=True)
            if soup.select_one(".quote__name")
            else symbol_info.get("name", symbol_info["symbol"])
        ),
        "current_price": _number(
            soup.select_one(".quote__close").get_text(strip=True)
            if soup.select_one(".quote__close")
            else None
        ),
        "volume": int(volume_match.group(1).replace(",", "")) if volume_match else None,
        "change_percent": _number(change_node.get_text(strip=True) if change_node else None),
        "52_week_low": _number(range_match.group(1)) if range_match else None,
        "52_week_high": _number(range_match.group(2)) if range_match else None,
        "sector": (
            soup.select_one(".quote__sector").get_text(" ", strip=True)
            if soup.select_one(".quote__sector")
            else symbol_info.get("sectorName")
        ),
        "shariah_status": "non-compliant" if non_compliant else shariah_status,
    }
    quality_fields = (
        "company_name",
        "current_price",
        "volume",
        "change_percent",
        "52_week_high",
        "52_week_low",
        "sector",
    )
    stock["data_quality"] = round(
        sum(stock.get(field) is not None for field in quality_fields) / len(quality_fields), 2
    )
    return stock


def store_stocks(stocks: list[dict[str, Any]]) -> None:
    """Upsert normalized stock records using parameterized SQL."""
    initialize_database()
    query = """
        INSERT INTO stocks (
            symbol, company_name, current_price, volume, change_percent,
            week_52_high, week_52_low, sector, shariah_status, data_quality,
            price_history, scraped_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol) DO UPDATE SET
            company_name=excluded.company_name, current_price=excluded.current_price,
            volume=excluded.volume, change_percent=excluded.change_percent,
            week_52_high=excluded.week_52_high, week_52_low=excluded.week_52_low,
            sector=excluded.sector, shariah_status=excluded.shariah_status,
            data_quality=excluded.data_quality, price_history=excluded.price_history,
            scraped_at=excluded.scraped_at
    """
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        (
            stock["symbol"],
            stock["company_name"],
            stock.get("current_price"),
            stock.get("volume"),
            stock.get("change_percent"),
            stock.get("52_week_high"),
            stock.get("52_week_low"),
            stock.get("sector"),
            stock.get("shariah_status", "unknown"),
            stock.get("data_quality", 0),
            json.dumps(stock.get("price_history", [])),
            now,
        )
        for stock in stocks
    ]
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.executemany(query, rows)


def scrape_stocks() -> list[dict[str, Any]]:
    """Scrape, filter, persist, and return low-priced Shariah-eligible stocks."""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    shariah_statuses = get_shariah_statuses(session)
    results: list[dict[str, Any]] = []
    for item in fetch_symbols(session):
        symbol = item["symbol"]
        shariah_status = shariah_statuses.get(symbol.upper(), "unknown")
        if shariah_status == "non-compliant":
            continue
        response = _request(session, f"{PSX_BASE_URL}/company/{symbol}")
        if not response:
            continue
        stock = parse_company_page(
            response.text,
            item,
            shariah_status=shariah_status,
        )
        price = stock.get("current_price")
        if (
            price is not None
            and MIN_STOCK_PRICE <= price <= MAX_STOCK_PRICE
            and stock["shariah_status"] != "non-compliant"
        ):
            stock["price_history"] = fetch_history(session, symbol)
            results.append(stock)
    store_stocks(results)
    LOGGER.info("Stored %s qualifying PSX stocks", len(results))
    return results
