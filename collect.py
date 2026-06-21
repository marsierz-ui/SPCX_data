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

from spcx_utils import atomic_write_csv, load_dotenv, merge_deduplicate, read_timestamped_csv

TICKER = "SPCX"          # Yahoo symbol
RIC = "SPCX.O"           # LSEG RIC for Nasdaq listing
MARKET_TZ = "America/New_York"
REPO_ROOT = Path(__file__).resolve().parent
BARS_FILE = REPO_ROOT / "data" / "1min" / f"{TICKER}_1min.csv"
TICKS_DIR = REPO_ROOT / "data" / "ticks"
SNAPSHOT_FILE = REPO_ROOT / "data" / "snapshots" / f"{TICKER}_quotes.csv"


load_dotenv(REPO_ROOT / ".env")

# Number of past calendar days (besides today) to refresh on each LSEG run,
# so late corrections and any missed runs are picked up.
LSEG_BACKFILL_DAYS = int(os.environ.get("LSEG_BACKFILL_DAYS", "4"))
LSEG_PAGE_SIZE = 10_000  # max rows per tick-history request
# Cap on tick-history requests per run so a heavy day (e.g. the IPO) can't
# push the job past its CI timeout; later runs resume where this one stopped.
LSEG_MAX_PAGES_PER_RUN = int(os.environ.get("LSEG_MAX_PAGES_PER_RUN", "40"))


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
    state = session.open()
    if state != ld.OpenState.Opened:
        # lseg-data logs auth errors (e.g. 403 on token refresh) without
        # raising, so surface the failure explicitly.
        raise RuntimeError(
            "LSEG session failed to open — check that the app key is valid "
            "and, for platform sessions, that LSEG_MACHINE_ID/LSEG_PASSWORD "
            "are real machine-account credentials (not a personal "
            "Workspace login)."
        )
    ld.session.set_default(session)
    return session


def fetch_ticks_window(window_start, window_end, budget: int):
    """Fetch ticks in [window_start, window_end), paginating backwards from
    the end. The API can return short pages mid-stream, so only an empty page
    (or lack of progress) ends the window. Returns (pages, pages_used)."""
    import lseg.data as ld

    pages = []
    cursor = window_end
    used = 0
    while cursor > window_start and used < budget:
        page = ld.get_history(
            universe=RIC,
            interval="tick",
            fields=["TRDPRC_1", "TRDVOL_1", "BID", "ASK"],
            start=window_start.tz_convert("UTC").tz_localize(None),
            end=cursor.tz_convert("UTC").tz_localize(None),
            count=LSEG_PAGE_SIZE,
        )
        used += 1
        if page is None or page.empty:
            break
        page.index = pd.DatetimeIndex(page.index, tz="UTC").tz_convert(MARKET_TZ)
        page.index.name = "timestamp"
        pages.append(page)
        earliest = page.index.min()
        if earliest >= cursor:  # no progress (page of identical timestamps)
            break
        # Page backwards: next request ends where this one began.
        cursor = earliest
    return pages, used


def fetch_ticks_for_day(day, existing: pd.DataFrame | None, budget: int):
    """Fetch the ticks still missing for one calendar day and merge them with
    what is already stored. Fetches the range newer than the stored data
    first, then continues backfilling the range older than it, so coverage
    grows across runs even when the page budget cuts a run short.
    Returns (merged_ticks, pages_used)."""
    day_start = pd.Timestamp(day, tz=MARKET_TZ)
    day_end = day_start + timedelta(days=1)

    if existing is None or existing.empty:
        pages, used = fetch_ticks_window(day_start, day_end, budget)
        frames = pages
    else:
        newer, used_n = fetch_ticks_window(existing.index.max(), day_end, budget)
        older, used_o = fetch_ticks_window(day_start, existing.index.min(),
                                           budget - used_n)
        frames = newer + older + [existing]
        used = used_n + used_o

    if not frames:
        return pd.DataFrame(), used
    ticks = pd.concat(frames)
    # Fetch windows overlap at their boundary timestamps; identical rows
    # there are duplicates, but distinct trades sharing a timestamp are kept.
    ticks = ticks.reset_index().drop_duplicates().set_index("timestamp")
    return ticks.sort_index(), used


def read_existing_ticks(path: Path) -> pd.DataFrame | None:
    return read_timestamped_csv(path, ts_column="timestamp", tz=MARKET_TZ)


def collect_lseg_ticks() -> None:
    TICKS_DIR.mkdir(parents=True, exist_ok=True)
    session = open_lseg_session()
    budget = LSEG_MAX_PAGES_PER_RUN
    try:
        today = datetime.now(timezone.utc).astimezone().date()
        for offset in range(LSEG_BACKFILL_DAYS, -1, -1):
            if budget <= 0:
                print("Page budget exhausted; the next run will continue.")
                break
            day = today - timedelta(days=offset)
            # .gz: pandas infers gzip from the extension on read and write.
            # Per-day (not one accumulating file): an ever-growing tick file
            # would (a) still hit GitHub's 100 MB/file cap in ~weeks and (b)
            # break git delta compression, since recompressing the whole gz
            # each commit stores a fresh full copy in history.
            path = TICKS_DIR / f"{TICKER}_{day}.csv.gz"
            existing = read_existing_ticks(path)
            before = 0 if existing is None else len(existing)
            ticks, used = fetch_ticks_for_day(day, existing, budget)
            budget -= used
            if ticks.empty:
                print(f"{day}: no ticks (non-trading day or not yet traded).")
                continue
            if len(ticks) > before:
                ticks.to_csv(path)
            print(f"{day}: {len(ticks)} ticks stored ({len(ticks) - before} new).")
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


def merge_bars_into_file(bars: pd.DataFrame) -> int:
    """Merge fetched bars into one accumulating CSV. Returns rows added.

    1-minute bars are small (~25-60 KB/day), so a single growing file stays
    well under GitHub's 100 MB/file cap for many years, unlike the tick data."""
    existing = read_timestamped_csv(BARS_FILE, ts_column="timestamp", tz=MARKET_TZ)
    if existing is not None:
        before = len(existing)
        merged = merge_deduplicate(existing, bars, keep="last")
        added = len(merged) - before
    else:
        merged = bars.sort_index()
        added = len(merged)
    atomic_write_csv(merged, BARS_FILE)
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
        added = merge_bars_into_file(bars)
        first, last = bars.index[0], bars.index[-1]
        print(f"Fetched {len(bars)} bars ({first} .. {last}), {added} new rows stored.")

    try:
        append_quote_snapshot(ticker)
    except Exception as exc:  # snapshot is best-effort; never fail the run for it
        print(f"WARNING: quote snapshot failed: {exc}")


def main() -> int:
    if lseg_credentials_present():
        print("LSEG credentials found: collecting tick-level data.")
        try:
            collect_lseg_ticks()
            return 0
        except Exception as exc:
            print(f"WARNING: LSEG collection failed: {exc}")
            print("Falling back to Yahoo 1-minute bars so data keeps flowing.")
    else:
        print("No LSEG credentials: collecting Yahoo 1-minute bars.")
    collect_yahoo_bars()
    return 0


if __name__ == "__main__":
    sys.exit(main())
