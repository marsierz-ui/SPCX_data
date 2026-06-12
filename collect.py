#!/usr/bin/env python3
"""Collect SPCX (SpaceX) intraday market data and store it as per-day CSV files.

Fetches 1-minute OHLCV bars (including pre/post-market) from Yahoo Finance.
Yahoo only retains 1-minute bars for the trailing ~7 days, so this script is
meant to run on a schedule (see .github/workflows/collect.yml) and merge each
fetch into data/1min/SPCX_YYYY-MM-DD.csv, deduplicated by timestamp. Runs are
idempotent: re-running never loses or duplicates rows.

Also appends a single real-time quote snapshot per run to
data/snapshots/SPCX_quotes.csv (last price, volume, bid/ask when available),
which gives sub-minute samples between bars while the collector runs often.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

TICKER = "SPCX"
MARKET_TZ = "America/New_York"
REPO_ROOT = Path(__file__).resolve().parent
BARS_DIR = REPO_ROOT / "data" / "1min"
SNAPSHOT_FILE = REPO_ROOT / "data" / "snapshots" / f"{TICKER}_quotes.csv"


def fetch_minute_bars(ticker: yf.Ticker) -> pd.DataFrame:
    """Fetch the trailing week of 1-minute bars, pre/post-market included."""
    bars = ticker.history(period="7d", interval="1m", prepost=True,
                          auto_adjust=False, actions=False)
    if bars.empty:
        return bars
    bars = bars.tz_convert(MARKET_TZ)
    bars.index.name = "timestamp"
    bars = bars[["Open", "High", "Low", "Close", "Volume"]]
    bars.columns = [c.lower() for c in bars.columns]
    return bars


def merge_bars_into_daily_files(bars: pd.DataFrame) -> int:
    """Merge fetched bars into one CSV per trading day. Returns rows added."""
    BARS_DIR.mkdir(parents=True, exist_ok=True)
    added = 0
    for day, day_bars in bars.groupby(bars.index.date):
        path = BARS_DIR / f"{TICKER}_{day}.csv"
        if path.exists():
            existing = pd.read_csv(path, index_col="timestamp", parse_dates=True)
            existing.index = pd.DatetimeIndex(existing.index).tz_convert(MARKET_TZ)
            before = len(existing)
            # keep="last" so freshly fetched bars overwrite earlier partial bars
            merged = pd.concat([existing, day_bars])
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()
            added += len(merged) - before
        else:
            merged = day_bars.sort_index()
            added += len(merged)
        merged.to_csv(path)
    return added


def append_quote_snapshot(ticker: yf.Ticker) -> None:
    """Append one point-in-time quote sample to the snapshot log."""
    info = ticker.fast_info
    row = {
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "last_price": getattr(info, "last_price", None),
        "day_volume": getattr(info, "last_volume", None),
        "day_high": getattr(info, "day_high", None),
        "day_low": getattr(info, "day_low", None),
        "previous_close": getattr(info, "previous_close", None),
    }
    if row["last_price"] is None:
        print("No quote snapshot available, skipping snapshot append.")
        return
    SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame([row])
    df.to_csv(SNAPSHOT_FILE, mode="a", header=not SNAPSHOT_FILE.exists(), index=False)


def main() -> int:
    ticker = yf.Ticker(TICKER)

    bars = fetch_minute_bars(ticker)
    if bars.empty:
        # Not an error: market may not have traded yet, or Yahoo hiccupped.
        # The next scheduled run will catch up (1m history covers 7 days).
        print(f"WARNING: no 1-minute bars returned for {TICKER}.")
    else:
        added = merge_bars_into_daily_files(bars)
        first, last = bars.index[0], bars.index[-1]
        print(f"Fetched {len(bars)} bars ({first} .. {last}), {added} new rows stored.")

    try:
        append_quote_snapshot(ticker)
    except Exception as exc:  # snapshot is best-effort; never fail the run for it
        print(f"WARNING: quote snapshot failed: {exc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
