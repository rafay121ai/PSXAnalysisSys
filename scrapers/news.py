"""News scraper for public Pakistani business-news sites."""

import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from config import (
    DATABASE_PATH,
    NEWS_MAX_ARTICLES_PER_SOURCE,
    REQUEST_DELAY,
    REQUEST_TIMEOUT,
    USER_AGENT,
)
from scrapers.psx import initialize_database

LOGGER = logging.getLogger(__name__)

NEWS_SOURCES = {
    "Dawn": "https://www.dawn.com/business",
    "Profit Pakistan Today": "https://profit.pakistantoday.com.pk",
    "The News": "https://www.thenews.com.pk/latest/category/business",
    "ARY News": "https://arynews.tv/category/business",
}

_TRAILING_LEGAL_SUFFIX = re.compile(
    r"(?:\s+|\s*\(\s*)"
    r"(?:limited|ltd\.?|pvt|private|company|corporation|co\.?|mills|industries)"
    r"(?:\s*\))?\.?\s*$",
    re.IGNORECASE,
)
_GENERIC_SINGLE_TOKEN_NAMES = {
    "first",
    "global",
    "image",
    "loads",
    "secure",
    "systems",
    "unity",
    "waves",
}


def _request(session: requests.Session, url: str) -> requests.Response | None:
    """Fetch a news page with retries and a delay between attempts."""
    for attempt in range(1, 4):
        try:
            time.sleep(REQUEST_DELAY)
            response = session.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            LOGGER.warning("News request failed (%s/3) %s: %s", attempt, url, exc)
    return None


def _article_links(html: str, base_url: str) -> list[str]:
    """Find likely same-site article URLs from a source listing page."""
    soup = BeautifulSoup(html, "html.parser")
    host = urlparse(base_url).netloc.removeprefix("www.")
    links: list[str] = []
    for anchor in soup.select("a[href]"):
        url = urljoin(base_url, anchor.get("href", ""))
        parsed = urlparse(url)
        anchor_text = anchor.get_text(" ", strip=True)
        excluded_path = any(part in parsed.path.lower() for part in ("/category/", "/tag/", "/author/"))
        if (
            parsed.netloc.removeprefix("www.") == host
            and len(anchor_text) >= 20
            and not excluded_path
            and url.rstrip("/") != base_url.rstrip("/")
            and url not in links
        ):
            links.append(url)
    return links[:NEWS_MAX_ARTICLES_PER_SOURCE]


def _matchable_company_name(company_name: str) -> str:
    """Return a registered name suitable for strict headline matching."""
    stripped_name = re.sub(r"\s+", " ", company_name).strip()
    while stripped_name:
        without_suffix = _TRAILING_LEGAL_SUFFIX.sub("", stripped_name).strip(" ,.-")
        if without_suffix == stripped_name:
            break
        stripped_name = without_suffix

    tokens = re.findall(r"[A-Za-z0-9]+", stripped_name)
    if len(tokens) == 1:
        token = tokens[0].casefold()
        # Short or known-generic single names are too ambiguous; require their ticker instead.
        if len(token) <= 3 or token in _GENERIC_SINGLE_TOKEN_NAMES:
            return ""
    return stripped_name


def _mentioned_symbols(text: str, stocks: list[Any]) -> list[str]:
    """Match an uppercase ticker token or a strict stripped company-name phrase."""
    matches: list[str] = []
    normalized_text = re.sub(r"\s+", " ", text)
    for stock in stocks:
        raw_symbol = stock.get("symbol") if isinstance(stock, dict) else stock
        symbol = str(raw_symbol or "").strip().upper()
        company_name = str(stock.get("company_name") or "").strip() if isinstance(stock, dict) else ""
        ticker_match = symbol and re.search(
            rf"(?<![A-Za-z0-9]){re.escape(symbol)}(?![A-Za-z0-9])",
            text,
        )
        matchable_name = _matchable_company_name(company_name)
        company_match = matchable_name and re.search(
            rf"(?<![A-Za-z0-9]){re.escape(matchable_name)}(?![A-Za-z0-9])",
            normalized_text,
            re.IGNORECASE,
        )
        if ticker_match or company_match:
            matches.append(symbol)
    return sorted(set(matches))


def parse_article(html: str, source: str, url: str, stocks: list[Any]) -> dict[str, object]:
    """Extract normalized article fields from heterogeneous news HTML."""
    soup = BeautifulSoup(html, "html.parser")
    headline_node = soup.select_one("h1")
    headline_meta = soup.select_one('meta[property="og:title"]')
    headline = (
        headline_node.get_text(" ", strip=True)
        if headline_node
        else headline_meta.get("content", "").strip()
        if headline_meta
        else url
    )
    paragraphs = soup.select("article p, .story__content p, .entry-content p, main p")
    text = " ".join(node.get_text(" ", strip=True) for node in paragraphs)
    if not text:
        text = " ".join(node.get_text(" ", strip=True) for node in soup.select("p"))
    date_node = soup.select_one("time")
    date_meta = soup.select_one('meta[property="article:published_time"]')
    published_date = (
        date_node.get("datetime") or date_node.get_text(" ", strip=True)
        if date_node
        else date_meta.get("content")
        if date_meta
        else None
    )
    searchable = f"{headline} {text}"
    return {
        "headline": headline,
        "source": source,
        "url": url,
        "published_date": published_date,
        "full_text": text[:500],
        "mentioned_symbols": _mentioned_symbols(searchable, stocks),
    }


def store_news(articles: list[dict[str, object]]) -> None:
    """Insert articles into SQLite and deduplicate them by URL."""
    initialize_database()
    query = """
        INSERT OR IGNORE INTO news (
            headline, source, url, published_date, full_text,
            mentioned_symbols, scraped_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        (
            article["headline"],
            article["source"],
            article["url"],
            article.get("published_date"),
            article.get("full_text"),
            json.dumps(article.get("mentioned_symbols", [])),
            now,
        )
        for article in articles
    ]
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.executemany(query, rows)


def scrape_news(stocks: list[Any]) -> list[dict[str, object]]:
    """Scrape configured sources, persist articles, and continue past failures."""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    articles: list[dict[str, object]] = []
    for source, listing_url in NEWS_SOURCES.items():
        LOGGER.info("Checking news source: %s", source)
        listing = _request(session, listing_url)
        if not listing:
            continue
        links = _article_links(listing.text, listing_url)
        LOGGER.info("Found %s candidate articles at %s", len(links), source)
        for url in links:
            response = _request(session, url)
            if response:
                articles.append(parse_article(response.text, source, url, stocks))
    store_news(articles)
    LOGGER.info("Stored or deduplicated %s news articles", len(articles))
    return articles
