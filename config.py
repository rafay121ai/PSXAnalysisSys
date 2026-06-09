"""Application configuration loaded from environment variables."""

import os
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

MAX_STOCK_PRICE = 20
MIN_STOCK_PRICE = 0
MIN_DAILY_VOLUME = int(os.getenv("MIN_DAILY_VOLUME", "500000"))
WATCHLIST_MAX_SIZE = max(50, min(100, int(os.getenv("WATCHLIST_MAX_SIZE", "100"))))
WATCHLIST_REFRESH_DAYS = int(os.getenv("WATCHLIST_REFRESH_DAYS", "15"))
TIER1_FRAMEWORK_THRESHOLD = 4
TIER2_FRAMEWORK_THRESHOLD = 3
TIER1_REWARD_RISK = 2.0
TIER2_REWARD_RISK = 1.5
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "10"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "2"))
PROGRESS_INTERVAL = max(1, int(os.getenv("PROGRESS_INTERVAL", "25")))
PSX_BASE_URL = "https://dps.psx.com.pk"
SHARIAH_SOURCE_URL = (
    "https://www.scstrade.com/MarketStatistics/MS_MarketValuations.aspx/resdata"
)
SHARIAH_DISCLOSURE_PDF = (
    BASE_DIR
    / "N-1419-List-of-Listed-Companies-Shariah-Disclosures-by-Companies-(Dec-24-2025).pdf"
)

DATABASE_PATH = BASE_DIR / "data" / "psx.db"
SCHEMA_PATH = BASE_DIR / "data" / "schema.sql"
NEWS_MAX_ARTICLES_PER_SOURCE = int(os.getenv("NEWS_MAX_ARTICLES_PER_SOURCE", "15"))
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (compatible; PersonalPSXResearchTool/1.0)",
)
