# PSX Analysis System

A synchronous personal research assistant for Pakistan Stock Exchange equities.
It discovers low-priced stocks, checks Shariah status, gathers company news,
runs five analysis frameworks, and sends a Telegram report. It recommends;
the human decides.

## Data Sources

- Pakistan Stock Exchange Data Portal for symbols, quotes, and EOD history
- Official PSX Notice N-1419 for explicit Shariah classifications
- SCS Trade as a positive-confirmation fallback for Shariah compliance
- Dawn, Profit Pakistan Today, The News, and ARY News for catalysts

## Setup

```bash
python3 -m venv ~/psx-tool
source ~/psx-tool/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Add the OpenAI and Telegram credentials to `.env`.

## Run

```bash
# Test the complete analysis/report flow with dummy data
python main.py --test

# Full daily discovery
python main.py --mode discovery

# Discovery without OpenAI catalyst classification
python main.py --mode discovery --no-ai

# Include open-position status in the report
python main.py --mode monitoring

# Force the 15-day full-universe watchlist refresh
python main.py --mode discovery --refresh
```

Every run checks the persisted watchlist refresh timestamp. When the watchlist
is empty or at least 15 days old, the system screens the full PSX equity
universe for confirmed Shariah compliance, price from Rs 0-20, and daily
volume of at least 500,000 shares. It stores up to the 100 highest-volume
qualifying stocks. Discovery analyzes that watchlist; monitoring analyzes only
symbols with open trades. Results are stored in `data/psx.db`, which is
excluded from version control.

## Railway

The included `Procfile` starts a one-time discovery worker. Normal discovery
runs read the persisted watchlist and are fast. The scheduled 15-day refresh is
slower because it screens the confirmed compliant universe with the required
two-second request delay.

Configure Railway variables from `.env.example`. To run discovery on a daily
schedule, use a Railway cron job rather than an always-restarting service.

## Important

This project is a research aid, not an autonomous trading system. It does not
connect to brokers or place orders. Always verify data and make trading
decisions independently.
