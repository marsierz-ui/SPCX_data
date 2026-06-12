# SPCX_data — SpaceX (Nasdaq: SPCX) intraday trading dataset

Continuous collection of the most granular freely available market data for
SpaceX, which IPO'd on Nasdaq on **June 12, 2026** at $135/share under the
ticker **SPCX**.

## What gets collected

| Dataset | Path | Granularity | Source |
|---|---|---|---|
| OHLCV bars | `data/1min/SPCX_YYYY-MM-DD.csv` (one file per trading day) | 1 minute, incl. pre/post-market | Yahoo Finance via `yfinance` |
| Quote snapshots | `data/snapshots/SPCX_quotes.csv` | one sample per collector run (~15 min) | Yahoo Finance real-time quote |

Bar columns: `timestamp` (America/New_York), `open`, `high`, `low`, `close`, `volume`.

## How it works

Yahoo Finance only retains 1-minute bars for the **trailing ~7 days**, so the
data must be harvested continuously or it is lost forever. A GitHub Actions
workflow ([`.github/workflows/collect.yml`](.github/workflows/collect.yml))
runs `collect.py` on a schedule:

- every 15 minutes around US regular trading hours (Mon–Fri),
- hourly during extended pre/post-market hours,
- a nightly catch-up run after the after-hours session closes.

Each run fetches the trailing 7 days of 1-minute bars and merges them into the
per-day CSVs, deduplicating by timestamp (newer fetches overwrite partial
in-progress bars). Runs are idempotent, and because each fetch covers 7 days,
the dataset stays gap-free even if the schedule misses runs for several days.

> **Note:** GitHub only triggers scheduled workflows from the repository's
> **default branch**, so the cron starts firing once this lands on `main`.
> You can also trigger a run manually from the Actions tab (`workflow_dispatch`).

## Running locally

```bash
pip install -r requirements.txt
python collect.py
```

## Granularity: what "most granular" means here

1-minute OHLCV is the finest resolution available without a paid market-data
subscription. True **tick-level data** (every individual trade and quote)
requires a paid feed — e.g. Polygon.io, Databento, Alpaca, or IEX Cloud. If
you later get an API key for one of those, `collect.py` is the single place to
plug it in; the per-day-file layout works the same for tick data.
