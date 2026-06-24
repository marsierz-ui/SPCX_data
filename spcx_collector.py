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
import sys
import time
from pathlib import Path

import pandas as pd

from spcx_utils import atomic_write_csv, merge_deduplicate, post_json, read_timestamped_csv

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
    return post_json(API, payload, retries=retries)


def interval_ms(interval):
    return int(interval[:-1]) * UNIT_MS[interval[-1]]


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
        print(f"[discover] No coin matched '{symbol}'. Sample of coins:")
        for s in sorted(set(seen))[:40]:
            print("   ", s)
        sys.exit("Pass --coin with the exact id.")
    if len(hits) > 1:
        print(f"[discover] Multiple matches for '{symbol}': {hits}; using {hits[0]}")
    return hits[0]


def resolve_coin(symbol, coin_arg):
    """Use --coin, else cached coin, else discover and cache it."""
    if coin_arg:
        return coin_arg
    if COIN_CACHE.exists():
        cached = json.loads(COIN_CACHE.read_text()).get(symbol)
        if cached:
            return cached
    coin = discover_coin(symbol)
    OUTDIR.mkdir(exist_ok=True)
    COIN_CACHE.write_text(json.dumps({symbol: coin}))
    print(f"[discover] coin = {coin} (cached)")
    return coin


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
    df = read_timestamped_csv(path, ts_column="ts", tz="UTC", index=False)
    if df is None:
        return pd.DataFrame(columns=COLUMNS)
    return df


def write_master(df, path):
    atomic_write_csv(df, path, index=False)


def poll_once(coin, interval, master_path, backfill_days):
    """Fetch new candles, merge into master, write back. Returns row count."""
    master = load_master(master_path)
    now_ms = int(pd.Timestamp.now(tz="UTC").value // 1e6)
    ims = interval_ms(interval)

    if master.empty:
        # Fresh start: grab the whole window the endpoint will serve.
        span = min(backfill_days, MAX_CANDLES * ims / 86_400_000)
        start_ms = now_ms - int(span * 86_400_000)
        print(f"[poll] cold start, backfilling ~{span:.1f}d")
    else:
        last_ms = int(master["ts"].max().value // 1e6)
        start_ms = last_ms - OVERLAP * ims          # re-fetch trailing candles
        gap_min = (now_ms - last_ms) / 60_000
        if gap_min > MAX_CANDLES * ims / 60_000:
            print(f"[poll] WARNING gap of {gap_min/1440:.1f}d exceeds the "
                  f"~3.5d recovery window; older candles are lost.")

    fetched = fetch_candles(coin, interval, start_ms, now_ms)
    new = candles_to_frame(fetched)
    if new.empty:
        print(f"[poll] no candles returned (master has {len(master)} rows)")
        return len(master)

    if master.empty:
        combined = new.sort_values("ts").reset_index(drop=True)
    else:
        combined = merge_deduplicate(master, new, ts_column="ts", keep="last")
    added = len(combined) - len(master)
    write_master(combined, master_path)
    print(f"[poll] +{added} new, {len(combined)} total, "
          f"latest {combined['ts'].iloc[-1]}")
    return len(combined)


def main():
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
    print(f"=== collector coin={coin} interval={args.interval} -> {master_path} ===")

    if not args.loop:
        poll_once(coin, args.interval, master_path, args.backfill_days)
        return

    print(f"[loop] polling every {args.poll_sec:.0f}s; Ctrl-C to stop")
    while True:
        try:
            poll_once(coin, args.interval, master_path, args.backfill_days)
        except SystemExit:
            raise
        except Exception as e:                       # noqa: BLE001
            print(f"[loop] poll error: {e}")
        time.sleep(args.poll_sec)


if __name__ == "__main__":
    main()
