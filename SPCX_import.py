#!/usr/bin/env python3
"""Collect SPCX (SpaceX) market data and store it as per-day CSV files."""

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

# "1min" for minute bars, "tick" for trade-by-trade data.
LSEG_INTERVAL = "tick"

# Past calendar days (besides today) to refresh each run.
LSEG_BACKFILL_DAYS = int(os.environ.get("LSEG_BACKFILL_DAYS", "2"))
LSEG_PAGE_SIZE = 10_000  # max rows per tick-history request


def load_dotenv() -> None:
    """Read KEY=VALUE lines from ./.env into os.environ (real env wins)."""
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def lseg_credentials_present() -> bool:
    return bool(os.environ.get("LSEG_APP_KEY"))


def open_lseg_session():
    import lseg.data as ld

    # Rides on the logged-in LSEG Workspace application on this machine.
    print("Opening LSEG desktop session (requires Workspace running).")
    session = ld.session.desktop.Definition(
        app_key=os.environ["LSEG_APP_KEY"]).get_session()
    session.open()
    ld.session.set_default(session)
    return session


def fetch_lseg_day(day) -> pd.DataFrame:
    """Fetch one calendar day of LSEG data at LSEG_INTERVAL resolution."""
    import lseg.data as ld

    day_start = pd.Timestamp(day, tz=MARKET_TZ)
    day_end = day_start + timedelta(days=1)

    if LSEG_INTERVAL != "tick":
        # A day of minute bars fits in one request; default OHLCV fields.
        pages = [ld.get_history(
            universe=RIC,
            interval=LSEG_INTERVAL,
            start=day_start.tz_convert("UTC").tz_localize(None),
            end=day_end.tz_convert("UTC").tz_localize(None),
        )]
        if pages[0] is None or pages[0].empty:
            return pd.DataFrame()
    else:
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
            # Page backwards: next request ends where this one began. A short
            # page does NOT mean the data is exhausted (LSEG can return fewer
            # rows than requested), so only an empty page or an exhausted
            # cursor ends the loop.
            new_cursor = pd.Timestamp(page.index.min()).tz_localize("UTC").tz_convert(MARKET_TZ)
            if new_cursor >= cursor:
                # 10k+ events share this exact millisecond (e.g. a volatility
                # halt resume); step past it or the cursor never advances.
                new_cursor = cursor - pd.Timedelta(milliseconds=1)
            if new_cursor <= day_start:
                break
            cursor = new_cursor
            print(f"  ...paged back to {cursor.strftime('%H:%M:%S')} ET "
                  f"({sum(len(p) for p in pages)} rows so far)")

    if not pages:
        return pd.DataFrame()
    ticks = pd.concat(pages)
    ticks.index = pd.DatetimeIndex(ticks.index, tz="UTC").tz_convert(MARKET_TZ)
    ticks.index.name = "timestamp"
    # Pagination windows overlap at the boundary timestamp; identical rows
    # there are duplicates, but distinct trades sharing a timestamp are kept.
    ticks = ticks.reset_index().drop_duplicates().set_index("timestamp")
    return ticks.sort_index()


def collect_lseg_data() -> None:
    out_dir = TICKS_DIR if LSEG_INTERVAL == "tick" else REPO_ROOT / "data" / f"{LSEG_INTERVAL}_lseg"
    out_dir.mkdir(parents=True, exist_ok=True)
    session = open_lseg_session()
    try:
        today = datetime.now(timezone.utc).astimezone().date()
        for offset in range(LSEG_BACKFILL_DAYS, -1, -1):
            day = today - timedelta(days=offset)
            path = out_dir / f"{TICKER}_{day}.csv"
            # Finalized past days never change; skip if already stored.
            if offset > 0 and path.exists():
                continue
            rows = fetch_lseg_day(day)
            if rows.empty:
                print(f"{day}: no data (non-trading day or not yet traded).")
                continue
            # Overwrite with the full, freshly fetched day: simplest way to
            # stay correct when rows share timestamps.
            rows.to_csv(path)
            print(f"{day}: stored {len(rows)} {LSEG_INTERVAL} rows.")
    finally:
        session.close()


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
        print(f"WARNING: no 1-minute bars returned for {TICKER}.")
    else:
        added = merge_bars_into_daily_files(bars)
        first, last = bars.index[0], bars.index[-1]
        print(f"Fetched {len(bars)} bars ({first} .. {last}), {added} new rows stored.")

    try:
        append_quote_snapshot(ticker)
    except Exception as exc:  # snapshot is best-effort; never fail the run
        print(f"WARNING: quote snapshot failed: {exc}")


def main() -> int:
    load_dotenv()
    if lseg_credentials_present():
        print(f"LSEG credentials found: collecting {LSEG_INTERVAL} data.")
        collect_lseg_data()
    else:
        print("No LSEG credentials: falling back to Yahoo 1-minute bars.")
        collect_yahoo_bars()
    return 0


if __name__ == "__main__":
    sys.exit(main())