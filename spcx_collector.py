#!/usr/bin/env python3
"""
Incremental SPCX perp candle collector for Hyperliquid.

Maintains a single ever-growing master CSV. Each run fetches only the candles
since the last stored one, dedups on candle open-time, and re-fetches the last
few candles (the most recent candle is still forming and its o/h/l/c/v/n keep
updating until it closes). Writes are atomic (temp file + os.replace).

Why incremental matters: the candleSnapshot endpoint only serves roughly the
most recent 5000 candles (~3.5 days at 1m). Anything older is unrecoverable.
So you must poll regularly and append into your own archive; if you ever go
longer than ~3.5 days without polling you get a permanent gap.

MODES
    python spcx_collector.py                 # one-shot: fetch new candles, append, exit
    python spcx_collector.py --loop          # run forever, poll every --poll-sec (default 60)
    python spcx_collector.py --coin xyz:SPCX # skip discovery (also auto-cached after first run)

Designed to be safe to call repeatedly from Windows Task Scheduler or GitHub
Actions (idempotent). Output: out/perp_<interval>_master.csv
"""

import argparse
import json
import logging
import os
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)


class CollectorError(Exception):
    """Raised when an unrecoverable error occurs during collection."""

API = "https://api.hyperliquid.xyz/info"
SYMBOL = "SPCX"
UNIT_MS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000,
           "w": 604_800_000, "M": 30 * 86_400_000}
MAX_CANDLES = 5000          # endpoint per-request / recency limit
OVERLAP = 5                 # re-fetch this many trailing candles each poll
COLUMNS = ["ts", "open", "high", "low", "close", "volume", "trades"]

OUTDIR = Path("out")
COIN_CACHE = OUTDIR / ".coin_cache.json"


def post(payload, retries=5):
    """POST with exponential backoff on 429 / transient network errors."""
    data = json.dumps(payload).encode()
    last_exc = None
    for attempt in range(retries):
        req = urllib.request.Request(
            API, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read()
            try:
                return json.loads(body)
            except json.JSONDecodeError as e:
                raise CollectorError(
                    f"Invalid JSON from API: {e}; "
                    f"body[:200]={body[:200]!r}"
                ) from e
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                wait = 2 ** attempt
                log.warning("429 rate limited, backing off %ds", wait)
                time.sleep(wait)
                continue
            raise CollectorError(
                f"HTTP {e.code} {e.reason}: "
                f"{e.read().decode(errors='replace')}"
            ) from e
        except urllib.error.URLError as e:
            last_exc = e
            if attempt < retries - 1:
                wait = 2 ** attempt
                log.warning("Network error (%s), retry in %ds",
                            e.reason, wait)
                time.sleep(wait)
                continue
            raise CollectorError(
                f"Request failed after {retries} attempts: {e.reason}"
            ) from last_exc


def discover_coin(symbol):
    """Find the full {dex}:{coin} id whose name contains `symbol`."""
    dexs = post({"type": "perpDexs"}) or []
    seen, hits = [], []
    for d in dexs:
        dex = "" if d is None else (d.get("name", "") if isinstance(d, dict) else "")
        meta = post({"type": "meta", "dex": dex} if dex else {"type": "meta"})
        for a in (meta or {}).get("universe", []):
            nm = a.get("name", "")
            full = nm if ":" in nm else (f"{dex}:{nm}" if dex else nm)
            seen.append(full)
            if symbol.lower() in full.lower():
                hits.append(full)
        time.sleep(0.05)
    hits = sorted(set(hits))
    if not hits:
        sample = sorted(set(seen))[:40]
        raise CollectorError(
            f"No coin matched '{symbol}'. "
            f"Sample of available coins: {sample}. "
            f"Pass --coin with the exact id."
        )
    if len(hits) > 1:
        log.warning("Multiple matches for '%s': %s; using %s",
                    symbol, hits, hits[0])
    return hits[0]


def resolve_coin(symbol, coin_arg):
    """Use --coin, else cached coin, else discover and cache it."""
    if coin_arg:
        return coin_arg
    if COIN_CACHE.exists():
        try:
            cached = json.loads(COIN_CACHE.read_text()).get(symbol)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Corrupt coin cache %s (%s), re-discovering",
                        COIN_CACHE, e)
            cached = None
        if cached:
            return cached
    coin = discover_coin(symbol)
    OUTDIR.mkdir(exist_ok=True)
    COIN_CACHE.write_text(json.dumps({symbol: coin}))
    log.info("Discovered coin = %s (cached)", coin)
    return coin


def interval_ms(interval):
    num_part = interval[:-1]
    unit_part = interval[-1]
    if unit_part not in UNIT_MS:
        raise CollectorError(
            f"Unknown interval unit '{unit_part}' in '{interval}'; "
            f"expected one of {list(UNIT_MS)}"
        )
    try:
        return int(num_part) * UNIT_MS[unit_part]
    except ValueError as e:
        raise CollectorError(
            f"Invalid interval '{interval}': {e}"
        ) from e


def fetch_candles(coin, interval, start_ms, end_ms):
    """Page through [start, end] in <=5000-candle chunks; dedup on open time."""
    step = interval_ms(interval) * MAX_CANDLES
    out, lo = {}, start_ms
    while lo < end_ms:
        hi = min(lo + step, end_ms)
        batch = post({"type": "candleSnapshot",
                      "req": {"coin": coin, "interval": interval,
                              "startTime": lo, "endTime": hi}}) or []
        for c in batch:
            out[c["t"]] = c
        lo = hi
        time.sleep(0.1)
    return out


def candles_to_frame(candles_by_t):
    """{t: hyperliquid candle} -> tidy DataFrame keyed by ts."""
    if not candles_by_t:
        return pd.DataFrame(columns=COLUMNS)
    df = pd.DataFrame([candles_by_t[t] for t in sorted(candles_by_t)])
    df["ts"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    ren = {"o": "open", "h": "high", "l": "low", "c": "close",
           "v": "volume", "n": "trades"}
    for src, std in ren.items():
        df[std] = pd.to_numeric(df[src], errors="coerce")
    return df[COLUMNS]


def load_master(path):
    if not path.exists():
        return pd.DataFrame(columns=COLUMNS)
    try:
        df = pd.read_csv(path, parse_dates=["ts"])
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        return df
    except Exception:
        log.warning("Corrupt master CSV %s, starting fresh\n%s",
                    path, traceback.format_exc())
        return pd.DataFrame(columns=COLUMNS)


def atomic_write_csv(df, path):
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        df.to_csv(tmp, index=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def poll_once(coin, interval, master_path, backfill_days):
    """Fetch new candles, merge into master, write back. Returns row count."""
    master = load_master(master_path)
    now_ms = int(pd.Timestamp.now(tz="UTC").value // 1e6)
    ims = interval_ms(interval)

    if master.empty:
        # Fresh start: grab the whole window the endpoint will serve.
        span = min(backfill_days, MAX_CANDLES * ims / 86_400_000)
        start_ms = now_ms - int(span * 86_400_000)
        log.info("Cold start, backfilling ~%.1fd", span)
    else:
        last_ms = int(master["ts"].max().value // 1e6)
        start_ms = last_ms - OVERLAP * ims          # re-fetch trailing candles
        gap_min = (now_ms - last_ms) / 60_000
        if gap_min > MAX_CANDLES * ims / 60_000:
            log.warning("Gap of %.1fd exceeds the ~3.5d recovery window; "
                        "older candles are lost.", gap_min / 1440)

    fetched = fetch_candles(coin, interval, start_ms, now_ms)
    new = candles_to_frame(fetched)
    if new.empty:
        log.info("No candles returned (master has %d rows)", len(master))
        return len(master)

    stack = new if master.empty else pd.concat([master, new])
    combined = (stack
                .drop_duplicates(subset="ts", keep="last")   # keep updated last candle
                .sort_values("ts")
                .reset_index(drop=True))
    added = len(combined) - len(master)
    atomic_write_csv(combined, master_path)
    log.info("+%d new, %d total, latest %s",
             added, len(combined), combined['ts'].iloc[-1])
    return len(combined)


def main():
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.INFO,
    )
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interval", default="1m")
    ap.add_argument("--symbol", default=SYMBOL)
    ap.add_argument("--coin", default=None, help="exact {dex}:{coin}; skips discovery")
    ap.add_argument("--loop", action="store_true", help="run forever")
    ap.add_argument("--poll-sec", type=float, default=60.0)
    ap.add_argument("--backfill-days", type=float, default=3.4,
                    help="cold-start lookback (capped at endpoint window)")
    ap.add_argument("--outdir", default="out", help="output directory")
    args = ap.parse_args()

    global OUTDIR, COIN_CACHE
    OUTDIR = Path(args.outdir)
    COIN_CACHE = OUTDIR / ".coin_cache.json"
    OUTDIR.mkdir(parents=True, exist_ok=True)
    coin = resolve_coin(args.symbol, args.coin)
    master_path = OUTDIR / f"perp_{args.interval}_master.csv"
    log.info("=== collector coin=%s interval=%s -> %s ===",
             coin, args.interval, master_path)

    if not args.loop:
        poll_once(coin, args.interval, master_path, args.backfill_days)
        return

    log.info("Polling every %.0fs; Ctrl-C to stop", args.poll_sec)
    consecutive_errors = 0
    while True:
        try:
            poll_once(coin, args.interval, master_path, args.backfill_days)
            consecutive_errors = 0
        except KeyboardInterrupt:
            log.info("Interrupted, exiting.")
            break
        except CollectorError as e:
            consecutive_errors += 1
            log.error("Poll failed (%d consecutive): %s", consecutive_errors, e)
            if consecutive_errors >= 10:
                log.error("Too many consecutive failures, exiting.")
                sys.exit(1)
        except Exception:                            # noqa: BLE001
            consecutive_errors += 1
            log.error("Unexpected poll error (%d consecutive):\n%s",
                      consecutive_errors, traceback.format_exc())
            if consecutive_errors >= 10:
                log.error("Too many consecutive failures, exiting.")
                sys.exit(1)
        time.sleep(args.poll_sec)


if __name__ == "__main__":
    main()
