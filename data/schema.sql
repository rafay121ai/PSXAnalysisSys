PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS stocks (
    symbol TEXT PRIMARY KEY,
    company_name TEXT NOT NULL,
    current_price REAL,
    volume INTEGER,
    change_percent REAL,
    week_52_high REAL,
    week_52_low REAL,
    sector TEXT,
    shariah_status TEXT NOT NULL DEFAULT 'unknown',
    data_quality REAL NOT NULL DEFAULT 0,
    price_history TEXT NOT NULL DEFAULT '[]',
    scraped_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    headline TEXT NOT NULL,
    source TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    published_date TEXT,
    full_text TEXT,
    mentioned_symbols TEXT NOT NULL DEFAULT '[]',
    scraped_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watchlist (
    symbol TEXT PRIMARY KEY,
    company_name TEXT,
    notes TEXT,
    added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS app_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL,
    quantity REAL,
    opened_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    closed_at TEXT,
    outcome TEXT,
    pnl REAL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS framework_weights (
    framework TEXT PRIMARY KEY,
    weight REAL NOT NULL DEFAULT 1.0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT OR IGNORE INTO framework_weights (framework, weight) VALUES
    ('weinstein', 1.0),
    ('minervini', 1.0),
    ('catalyst', 1.0),
    ('kelly', 1.0),
    ('munger', 1.0);
