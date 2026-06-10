"""Synchronous PSX stock and price-history scraper."""

import json
import hashlib
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from bot.telegram import send_system_alert
from config import (
    DATABASE_PATH,
    KMI_ALL_SHARE_CONSTITUENTS_URL,
    MAX_STOCK_PRICE,
    MIN_DAILY_VOLUME,
    MIN_STOCK_PRICE,
    PROGRESS_INTERVAL,
    PSX_BASE_URL,
    REQUEST_DELAY,
    REQUEST_TIMEOUT,
    SCHEMA_PATH,
    SHARIAH_SOURCE_URL,
    USER_AGENT,
    WATCHLIST_MAX_SIZE,
    WATCHLIST_REFRESH_DAYS,
)

LOGGER = logging.getLogger(__name__)
ANNOUNCEMENTS_URL = "https://dps.psx.com.pk/announcements"


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


def _post_request(
    session: requests.Session,
    url: str,
    data: dict[str, Any],
) -> requests.Response | None:
    """Post form data with the same retries and throttling as PSX GET requests."""
    for attempt in range(1, 4):
        try:
            time.sleep(REQUEST_DELAY)
            response = session.post(url, data=data, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            LOGGER.warning("PSX request failed (%s/3) %s: %s", attempt, url, exc)
    return None


def _normalize_shariah_status(value: Any) -> str:
    """Normalize Shariah status spellings before filtering or persistence."""
    return str(value or "unknown").strip().lower().replace("_", "-").replace(" ", "-")


def _is_shariah_compliant(value: Any) -> bool:
    """Return true only for an explicit compliant classification."""
    return _normalize_shariah_status(value) == "compliant"


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


def fetch_kmi_all_share_symbols(session: requests.Session) -> set[str] | None:
    """Return compliant constituents from PSX's authoritative KMI All Share table."""
    response = _request(session, KMI_ALL_SHARE_CONSTITUENTS_URL)
    if not response:
        LOGGER.warning("KMI All Share source unavailable; Shariah gate will fail closed")
        return None
    soup = BeautifulSoup(response.text, "html.parser")
    rows = soup.select("table tbody tr")
    symbols = {
        row.select_one("a.tbl__symbol").get_text(strip=True).upper()
        for row in rows
        if row.select_one("a.tbl__symbol")
        and not any(tag.get_text(strip=True).upper() == "NC" for tag in row.select(".tag"))
    }
    if not symbols:
        LOGGER.warning("KMI All Share source returned no compliant constituents; Shariah gate will fail closed")
        return None
    LOGGER.info(
        "Loaded %s compliant constituents from official PSX KMI All Share index",
        len(symbols),
    )
    return symbols


def store_shariah_universe(symbols: set[str]) -> None:
    """Replace the persisted KMI Shariah universe atomically."""
    initialize_database()
    fetched_at = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute("DELETE FROM shariah_universe")
        connection.executemany(
            "INSERT INTO shariah_universe (symbol, fetched_at) VALUES (?, ?)",
            [(symbol, fetched_at) for symbol in sorted(symbols)],
        )


def _cross_check_scs(session: requests.Session, kmi_symbols: set[str]) -> None:
    """Log SCS Trade disagreements without allowing SCS to affect the gate."""
    scs_symbols = fetch_shariah_symbols(session)
    if scs_symbols is None:
        LOGGER.warning("SCS Trade cross-check unavailable; official KMI gate remains authoritative")
        return
    only_kmi = sorted(kmi_symbols - scs_symbols)
    only_scs = sorted(scs_symbols - kmi_symbols)
    if only_kmi or only_scs:
        message = (
            f"Shariah source disagreement: {len(only_kmi)} only in official KMI "
            f"and {len(only_scs)} only in SCS Trade."
        )
        LOGGER.warning("%s", message)
        send_system_alert(message)


def company_page_is_non_compliant(html: str) -> bool:
    """Return true when a PSX company page displays an NC/non-compliant marker."""
    soup = BeautifulSoup(html, "html.parser")
    return any(tag.get_text(strip=True).upper() == "NC" for tag in soup.select(".tag")) or bool(
        re.search(r"\bNON[- ]COMPLIANT\b", soup.get_text(" ", strip=True), re.IGNORECASE)
    )


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

    range_match = re.search(
        r"52-WEEK RANGE\s*\^?\s*([\d,.]+)\s*[—-]\s*([\d,.]+)",
        stats_text,
        re.IGNORECASE,
    )
    volume_match = re.search(r"\bVolume\s+([\d,]+)", stats_text, re.IGNORECASE)
    change_node = soup.select_one(".change__percent")
    non_compliant = company_page_is_non_compliant(html)
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
        "shariah_status": "non-compliant" if non_compliant else _normalize_shariah_status(shariah_status),
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
            _normalize_shariah_status(stock.get("shariah_status")),
            stock.get("data_quality", 0),
            json.dumps(stock.get("price_history", [])),
            now,
        )
        for stock in stocks
    ]
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.executemany(query, rows)


def _deserialize_stock(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a joined SQLite stock row into the analysis-engine object shape."""
    try:
        history = json.loads(row["price_history"] or "[]")
    except json.JSONDecodeError:
        LOGGER.warning("Stored price history is invalid JSON for %s", row["symbol"])
        history = []
    return {
        "symbol": row["symbol"],
        "company_name": row["company_name"],
        "current_price": row["current_price"],
        "volume": row["volume"],
        "change_percent": row["change_percent"],
        "52_week_high": row["week_52_high"],
        "52_week_low": row["week_52_low"],
        "sector": row["sector"],
        "shariah_status": _normalize_shariah_status(row["shariah_status"]),
        "data_quality": row["data_quality"],
        "price_history": history,
    }


def _load_stocks_for_query(query: str) -> list[dict[str, Any]]:
    """Run a fixed stock-loading query and return normalized stock objects."""
    initialize_database()
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory = sqlite3.Row
        return [_deserialize_stock(row) for row in connection.execute(query).fetchall()]


def get_watchlist() -> list[dict[str, Any]]:
    """Return active watchlist stocks with their latest persisted market data."""
    return _load_stocks_for_query(
        """
        SELECT stocks.*
        FROM watchlist
        JOIN stocks ON stocks.symbol = watchlist.symbol
        JOIN shariah_universe ON shariah_universe.symbol = stocks.symbol
        WHERE watchlist.active = 1
          AND LOWER(REPLACE(stocks.shariah_status, '_', '-')) = 'compliant'
        ORDER BY stocks.volume DESC, stocks.symbol ASC
        """
    )


def get_active_position_stocks() -> list[dict[str, Any]]:
    """Return persisted stock objects for symbols with currently open trades."""
    return _load_stocks_for_query(
        """
        SELECT stocks.*
        FROM trades
        JOIN stocks ON stocks.symbol = trades.symbol
        WHERE trades.closed_at IS NULL
          AND LOWER(REPLACE(COALESCE(stocks.shariah_status, 'unknown'), '_', '-')) <> 'non-compliant'
        GROUP BY stocks.symbol
        ORDER BY stocks.symbol ASC
        """
    )


def get_watchlist_last_refreshed() -> datetime | None:
    """Return the successful watchlist refresh timestamp, if one exists."""
    initialize_database()
    with sqlite3.connect(DATABASE_PATH) as connection:
        row = connection.execute(
            "SELECT value FROM app_metadata WHERE key = ?",
            ("watchlist_last_refreshed",),
        ).fetchone()
    if not row:
        return None
    try:
        refreshed_at = datetime.fromisoformat(row[0])
        return (
            refreshed_at
            if refreshed_at.tzinfo is not None
            else refreshed_at.replace(tzinfo=timezone.utc)
        )
    except ValueError:
        LOGGER.warning("Stored watchlist refresh timestamp is invalid: %s", row[0])
        return None


def watchlist_needs_refresh(now: datetime | None = None) -> bool:
    """Return true when the watchlist is empty, missing metadata, or older than configured."""
    initialize_database()
    with sqlite3.connect(DATABASE_PATH) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM watchlist WHERE active = 1"
        ).fetchone()[0]
    refreshed_at = get_watchlist_last_refreshed()
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    return (
        count == 0
        or refreshed_at is None
        or current_time - refreshed_at.astimezone(timezone.utc) >= timedelta(days=WATCHLIST_REFRESH_DAYS)
    )


def _replace_watchlist(stocks: list[dict[str, Any]]) -> None:
    """Replace the active watchlist and record the successful refresh atomically."""
    compliant_stocks = [
        stock for stock in stocks if _is_shariah_compliant(stock.get("shariah_status"))
    ]
    if len(compliant_stocks) != len(stocks):
        LOGGER.error(
            "Blocked %s non-compliant or unconfirmed stocks from watchlist persistence",
            len(stocks) - len(compliant_stocks),
        )
    if not compliant_stocks:
        raise ValueError("Refusing to replace watchlist without confirmed compliant stocks")
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute("DELETE FROM watchlist")
        connection.executemany(
            """
            INSERT INTO watchlist (symbol, company_name, notes, added_at, active)
            VALUES (?, ?, ?, ?, 1)
            """,
            [
                (
                    stock["symbol"],
                    stock["company_name"],
                    f"15-day screen: compliant, Rs {stock['current_price']:.2f}, volume {stock['volume']:,}",
                    now,
                )
                for stock in compliant_stocks
            ],
        )
        connection.execute(
            """
            INSERT INTO app_metadata (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            ("watchlist_last_refreshed", now, now),
        )


def _clear_watchlist(reason: str) -> None:
    """Clear the active watchlist when a mandatory gate source is unavailable."""
    initialize_database()
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute("DELETE FROM watchlist")
    LOGGER.warning("Cleared watchlist because Shariah gate failed closed: %s", reason)
    send_system_alert(f"Watchlist cleared because Shariah gate failed closed: {reason}.")


def refresh_watchlist() -> list[dict[str, Any]]:
    """Scan the full universe and persist the highest-volume hard-filtered stocks."""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    shariah_symbols = fetch_kmi_all_share_symbols(session)
    if shariah_symbols is None:
        _clear_watchlist("official PSX KMI All Share source unavailable")
        return []
    store_shariah_universe(shariah_symbols)
    _cross_check_scs(session, shariah_symbols)
    symbols = fetch_symbols(session)
    eligible = [
        item
        for item in symbols
        if item["symbol"].upper() in shariah_symbols
    ]
    LOGGER.info(
        "Starting full-universe watchlist scan: %s equities, %s confirmed Shariah compliant",
        len(symbols),
        len(eligible),
    )
    qualifying: list[dict[str, Any]] = []
    page_gate_passed = 0
    price_gate_passed = 0
    volume_gate_passed = 0
    for index, item in enumerate(eligible, start=1):
        symbol = item["symbol"]
        response = _request(session, f"{PSX_BASE_URL}/company/{symbol}")
        if not response:
            LOGGER.warning("Excluded %s because its PSX company page was unavailable", symbol)
            continue
        if company_page_is_non_compliant(response.text):
            LOGGER.warning("Excluded %s because its PSX company page is marked non-compliant", symbol)
            continue
        page_gate_passed += 1
        stock = parse_company_page(
            response.text,
            item,
            shariah_status="compliant",
        )
        price = stock.get("current_price")
        volume = stock.get("volume")
        price_passed = price is not None and MIN_STOCK_PRICE <= price <= MAX_STOCK_PRICE
        volume_passed = volume is not None and volume >= MIN_DAILY_VOLUME
        price_gate_passed += int(price_passed)
        volume_gate_passed += int(volume_passed)
        if (
            price_passed
            and volume_passed
        ):
            stock["price_history"] = fetch_history(session, symbol)
            qualifying.append(stock)
        if index % PROGRESS_INTERVAL == 0 or index == len(eligible):
            LOGGER.info(
                "Watchlist scan progress: %s/%s checked, %s stocks qualify",
                index,
                len(eligible),
                len(qualifying),
            )
    LOGGER.info(
        "Watchlist refresh funnel: listed=%s, KMI=%s, page-clear=%s, price=%s, volume=%s, all-filters=%s",
        len(symbols),
        len(eligible),
        page_gate_passed,
        price_gate_passed,
        volume_gate_passed,
        len(qualifying),
    )
    selected = sorted(
        qualifying,
        key=lambda stock: (stock.get("volume") or 0, stock.get("data_quality") or 0),
        reverse=True,
    )[:WATCHLIST_MAX_SIZE]
    if not selected:
        _clear_watchlist("no stocks passed the mandatory KMI/company-page and market filters")
        return []
    if len(selected) < 50:
        LOGGER.warning(
            "Only %s stocks met all hard filters; watchlist target is 50-100",
            len(selected),
        )
    store_stocks(selected)
    _replace_watchlist(selected)
    LOGGER.info(
        "Watchlist refresh stored %s of %s qualifying stocks",
        len(selected),
        len(qualifying),
    )
    return selected


def scrape_stocks() -> list[dict[str, Any]]:
    """Backward-compatible alias for the full watchlist refresh."""
    return refresh_watchlist()


def _announcement_type(title: str) -> str:
    """Classify a PSX announcement title into a stable catalyst category."""
    lowered = title.lower()
    categories = (
        ("financial_results", ("financial result", "earnings", "profit", "loss")),
        ("dividend", ("dividend",)),
        ("material_information", ("material information",)),
        ("board_meeting", ("board meeting",)),
        ("corporate_action", ("right share", "bonus share", "merger", "acquisition")),
        ("management_change", ("appointment", "resignation", "chief executive", "director")),
        ("disclosure", ("disclosure",)),
    )
    return next(
        (category for category, terms in categories if any(term in lowered for term in terms)),
        "other",
    )


def _announcement_id(row: Any, symbol: str, date: str, title: str) -> str:
    """Return the PSX document identifier, with a stable fallback when absent."""
    for node in row.select("[href], [data-images]"):
        value = node.get("href") or node.get("data-images") or ""
        match = re.search(r"(\d+)(?:-\d+)?\.(?:pdf|gif|jpg|png)", value, re.IGNORECASE)
        if match:
            return match.group(1)
    return hashlib.sha256(f"{symbol}|{date}|{title}".encode("utf-8")).hexdigest()


def store_announcements(announcements: list[dict[str, str]]) -> None:
    """Persist PSX announcements and deduplicate them by source announcement ID."""
    initialize_database()
    fetched_at = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.executemany(
            """
            INSERT INTO announcements (
                announcement_id, symbol, title, date, type, pdf_url, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(announcement_id) DO UPDATE SET
                symbol=excluded.symbol, title=excluded.title, date=excluded.date,
                type=excluded.type, pdf_url=excluded.pdf_url, fetched_at=excluded.fetched_at
            """,
            [
                (
                    item["announcement_id"],
                    item["symbol"],
                    item["title"],
                    item["date"],
                    item["announcement_type"],
                    item.get("pdf_url"),
                    fetched_at,
                )
                for item in announcements
            ],
        )


def get_announcements(stocks: list[Any]) -> list[dict[str, str]]:
    """Load persisted announcements for the requested stock symbols."""
    symbols = sorted(
        {
            str(stock.get("symbol") if isinstance(stock, dict) else stock).strip().upper()
            for stock in stocks
            if (stock.get("symbol") if isinstance(stock, dict) else stock)
        }
    )
    if not symbols:
        return []
    initialize_database()
    placeholders = ",".join("?" for _ in symbols)
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            f"""
            SELECT announcement_id, symbol, title, date, type, pdf_url
            FROM announcements
            WHERE symbol IN ({placeholders})
            ORDER BY date DESC, announcement_id DESC
            """,
            symbols,
        ).fetchall()
    return [
        {
            "announcement_id": row["announcement_id"],
            "symbol": row["symbol"],
            "title": row["title"],
            "date": row["date"],
            "announcement_type": row["type"],
            "pdf_url": row["pdf_url"],
        }
        for row in rows
    ]


def scrape_announcements(stocks: list[Any]) -> list[dict[str, str]]:
    """Fetch, persist, and reload recent announcements matched by exact symbol."""
    wanted = {
        str(stock.get("symbol") if isinstance(stock, dict) else stock).strip().upper()
        for stock in stocks
        if (stock.get("symbol") if isinstance(stock, dict) else stock)
    }
    if not wanted:
        return []
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    response = _post_request(
        session,
        ANNOUNCEMENTS_URL,
        {
            "type": "C",
            "symbol": "",
            "query": "",
            "count": 500,
            "offset": 0,
            "date_from": "",
            "date_to": "",
            "page": "annc",
        },
    )
    if not response:
        LOGGER.warning("Announcements feed unavailable; analysis will use persisted announcements")
        return get_announcements(stocks)
    announcements: list[dict[str, str]] = []
    soup = BeautifulSoup(response.text, "html.parser")
    for row in soup.select("#announcementsTable tbody tr"):
        cells = row.find_all("td")
        if len(cells) < 5:
            continue
        row_symbol = cells[2].get_text(" ", strip=True).upper()
        if row_symbol not in wanted:
            continue
        date = cells[0].get_text(" ", strip=True)
        title = cells[4].get_text(" ", strip=True)
        pdf_node = row.select_one('a[href*="/download/document/"]')
        announcements.append(
            {
                "announcement_id": _announcement_id(row, row_symbol, date, title),
                "symbol": row_symbol,
                "title": title,
                "date": date,
                "announcement_type": _announcement_type(title),
                "pdf_url": urljoin(PSX_BASE_URL, pdf_node.get("href")) if pdf_node else "",
            }
        )
    store_announcements(announcements)
    LOGGER.info(
        "Loaded %s recent PSX announcements matching %s requested symbols",
        len(announcements),
        len(wanted),
    )
    return get_announcements(stocks)
