#!/usr/bin/env python3
"""Collect SPCX (SpaceX) market data and store it as per-day CSV files.

Two data sources, picked automatically:

1. LSEG Data Platform (preferred) — true tick-level trades and quotes via the
   `lseg-data` library. Used when LSEG_APP_KEY, LSEG_MACHINE_ID and
   LSEG_PASSWORD are set in the environment. Writes data/ticks/SPCX_YYYY-MM-DD.csv.
2. Yahoo Finance (fallback) — 1-minute OHLCV bars (incl. pre/post-market).
   Yahoo only retains 1-minute bars for the trailing ~7 days, so this script
   runs on a schedule (see .github/workflows/collect.yml) and merges each
   fetch into data/1min/SPCX_YYYY-MM-DD.csv, deduplicated by timestamp.

Runs are idempotent: re-running never loses or duplicates rows.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

TICKER = "SPCX"          # Yahoo symbol
RIC = "SPCX.O"           # LSEG RIC for Nasdaq listing
MARKET_TZ = "America/New_York"
REPO_ROOT = Path(__file__).resolve().parent
BARS_DIR = REPO_ROOT / "data" / "1min"
TICKS_DIR = REPO_ROOT / "data" / "ticks"
SNAPSHOT_FILE = REPO_ROOT / "data" / "snapshots" / f"{TICKER}_quotes.csv"

# Number of past calendar days (besides today) to refresh on each LSEG run,
# so late corrections and any missed runs are picked up.
LSEG_BACKFILL_DAYS = int(os.environ.get("LSEG_BACKFILL_DAYS", "2"))
LSEG_PAGE_SIZE = 10_000  # max rows per tick-history request


def lseg_credentials_present() -> bool:
    # App key alone is enough for a desktop session (LSEG Workspace running
    # and logged in on this machine). Headless runs (GitHub Actions) also
    # need LSEG_MACHINE_ID and LSEG_PASSWORD for a platform session.
    return bool(os.environ.get("LSEG_APP_KEY"))


# ---------------------------------------------------------------------------
# LSEG tick collection
# ---------------------------------------------------------------------------

def open_lseg_session():
    import lseg.data as ld

    app_key = os.environ["LSEG_APP_KEY"]
    if os.environ.get("LSEG_MACHINE_ID") and os.environ.get("LSEG_PASSWORD"):
        print("Opening LSEG platform session (machine account).")
        session = ld.session.platform.Definition(
            app_key=app_key,
            grant=ld.session.platform.GrantPassword(
                username=os.environ["LSEG_MACHINE_ID"],
                password=os.environ["LSEG_PASSWORD"],
            ),
            signon_control=True,
        ).get_session()
    else:
        # Rides on the logged-in LSEG Workspace application on this machine.
        print("Opening LSEG desktop session (requires Workspace running).")
        session = ld.session.desktop.Definition(app_key=app_key).get_session()
    session.open()
    ld.session.set_default(session)
    return session


def fetch_ticks_for_day(day) -> pd.DataFrame:
    """Fetch all ticks for one calendar day, paginating backwards in time."""
    import lseg.data as ld

    day_start = pd.Timestamp(day, tz=MARKET_TZ)
    day_end = day_start + timedelta(days=1)
    pages = []
    cursor = day_end
    while True:
        page = ld.get_history(
            universe=RIC,
            interval="tick",
            fields=["TRDPRC_1", "TRDVOL_1", "BID", "ASK"],
            start=day_start.tz_convert("UTC").tz_localize(None),
            end=cursor.tz_convert("UTC").tz_localize(None),
            count=LSEG_PAGE_SIZE,
        )
        if page is None or page.empty:
            break
        pages.append(page)
        if len(page) < LSEG_PAGE_SIZE:
            break
        # Page backwards: next request ends where this one began.
        cursor = pd.Timestamp(page.index.min()).tz_localize("UTC").tz_convert(MARKET_TZ)
        if cursor <= day_start:
            break

    if not pages:
        return pd.DataFrame()
    ticks = pd.concat(pages)
    ticks.index = pd.DatetimeIndex(ticks.index, tz="UTC").tz_convert(MARKET_TZ)
    ticks.index.name = "timestamp"
    # Pagination windows overlap at the boundary timestamp; identical rows
    # there are duplicates, but distinct trades sharing a timestamp are kept.
    ticks = ticks.reset_index().drop_duplicates().set_index("timestamp")
    return ticks.sort_index()


def collect_lseg_ticks() -> None:
    TICKS_DIR.mkdir(parents=True, exist_ok=True)
    session = open_lseg_session()
    try:
        today = datetime.now(timezone.utc).astimezone().date()
        for offset in range(LSEG_BACKFILL_DAYS, -1, -1):
            day = today - timedelta(days=offset)
            path = TICKS_DIR / f"{TICKER}_{day}.csv"
            # Finalized past days never change; skip if already stored.
            if offset > 0 and path.exists():
                continue
            ticks = fetch_ticks_for_day(day)
            if ticks.empty:
                print(f"{day}: no ticks (non-trading day or not yet traded).")
                continue
            # Overwrite with the full, freshly fetched day: simplest way to
            # stay correct when ticks share timestamps.
            ticks.to_csv(path)
            print(f"{day}: stored {len(ticks)} ticks.")
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Yahoo Finance fallback (1-minute bars)
# ---------------------------------------------------------------------------

def fetch_minute_bars(ticker) -> pd.DataFrame:
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


def append_quote_snapshot(ticker) -> None:
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


def collect_yahoo_bars() -> None:
    import yfinance as yf

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


def main() -> int:
    if lseg_credentials_present():
        print("LSEG credentials found: collecting tick-level data.")
        collect_lseg_ticks()
    else:
        print("No LSEG credentials: falling back to Yahoo 1-minute bars.")
        collect_yahoo_bars()
    return 0


if __name__ == "__main__":
    sys.exit(main())
