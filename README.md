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
```

The full scraper is intentionally slow because requests are rate-limited.
Results are stored in `data/psx.db`, which is excluded from version control.

## Important

This project is a research aid, not an autonomous trading system. It does not
connect to brokers or place orders. Always verify data and make trading
decisions independently.
