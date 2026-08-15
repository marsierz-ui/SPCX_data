# SPCX_data — SpaceX (Nasdaq: SPCX) intraday trading dataset

Continuous collection of the most granular freely available market data for
SpaceX, which IPO'd on Nasdaq on **June 12, 2026** at $135/share under the
ticker **SPCX**.

## What gets collected

The collector picks its source automatically:

- **LSEG Data Platform** (preferred, tick-level) — used when LSEG credentials
  are configured. Every trade and quote, microsecond timestamps.
- **Yahoo Finance** (fallback, free) — 1-minute OHLCV bars when no LSEG
  credentials are present.

| Dataset | Path | Granularity | Source |
|---|---|---|---|
| Tick data (trades + quotes) | `data/ticks/SPCX_YYYY-MM-DD.csv` (one file per day) | every tick (`TRDPRC_1`, `TRDVOL_1`, `BID`, `ASK`) | LSEG (`lseg-data`, RIC `SPCX.O`) |
| OHLCV bars | `data/1min/SPCX_YYYY-MM-DD.csv` (one file per trading day) | 1 minute, incl. pre/post-market | Yahoo Finance via `yfinance` |
| Quote snapshots | `data/snapshots/SPCX_quotes.csv` | one sample per collector run (~15 min) | Yahoo Finance real-time quote |

Bar columns: `timestamp` (America/New_York), `open`, `high`, `low`, `close`, `volume`.

## Enabling LSEG tick collection

Two authentication modes, picked automatically:

**Locally, with LSEG Workspace running and logged in** — only the app key is
needed; the collector opens a *desktop session* through Workspace. Put the key
in a local `.env` file (gitignored, loaded automatically — see
[`.env.example`](.env.example)):

```bash
cp .env.example .env    # then edit .env and set LSEG_APP_KEY
python collect.py
```

Setting `LSEG_APP_KEY` as an environment variable works too and takes
precedence over `.env`.

**Headless (GitHub Actions)** — a desktop session is impossible on a cloud
runner, so a machine (service) account is required. In the repo:
**Settings → Secrets and variables → Actions → New repository secret**, add:

- `LSEG_APP_KEY` — your LSEG Data Platform app key
- `LSEG_MACHINE_ID` — your machine account ID (service account user)
- `LSEG_PASSWORD` — its password

In either mode the collector pulls full tick history for SPCX (RIC `SPCX.O`),
refreshing today plus the previous `LSEG_BACKFILL_DAYS` (default 2) calendar
days. Past days already on disk are never refetched, and missed days can be
backfilled by running once with a larger `LSEG_BACKFILL_DAYS`.

**Licensing warning:** exchange data agreements generally prohibit
redistributing raw tick data. Keep this repository **private** if you store
LSEG tick data in it, and check your subscription's storage terms.

## ⚠️ One-time activation step

GitHub Actions workflows can't be pushed with this repo's automation
credentials, so the workflow file lives at [`workflows/collect.yml`](workflows/collect.yml)
and must be moved into place once, manually:

1. In the GitHub web UI, open `workflows/collect.yml` and copy its contents.
2. Create a new file at **`.github/workflows/collect.yml`** (on the default
   branch) with that content and commit it.

Yahoo only keeps 1-minute bars for ~7 days, so do this **within a few days of
the June 12 IPO** to keep first-day trading data from expiring. After saving
the file you can also trigger an immediate run from the **Actions** tab
("Collect SPCX data" → "Run workflow").

## How it works

Yahoo Finance only retains 1-minute bars for the **trailing ~7 days**, so the
data must be harvested continuously or it is lost forever. A GitHub Actions
workflow (`.github/workflows/collect.yml`, staged at
[`workflows/collect.yml`](workflows/collect.yml)) runs `collect.py` on a
schedule:

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

`spcx_hl_api.py` is a one-shot Hyperliquid pull for ad-hoc windows, with
optional alignment of the perp against a stock CSV (`--stock`).
`SPCX_import.py` is an earlier per-day stock collector, superseded by
`collect.py`; both read `LSEG_APP_KEY` from the environment or `.env`.

## Repository size

The tick files are gzipped, so git cannot delta-compress them: every run that
rewrites a day's file stores a fresh full blob. That grew this repo to ~2.2 GB
by 2026-08-15 at roughly **107 MB/day**. GitHub's soft limit is 1 GB (already
passed) and the hard limit is 5 GB, which at that rate arrives around
2026-09-10.

`archive_ticks.py` offloads the tick archive to a local folder when the limit
nears:

```bash
python archive_ticks.py --status                # headroom against the limits
python archive_ticks.py --archive               # copy + verify to the local folder
python archive_ticks.py --prune-older-than 14   # untrack what is safely archived
```

Verification is sha256 plus a full decompress-and-parse of the archived copy,
and `--prune` refuses to untrack any file it has not verified, so nothing is
dropped from git until a readable local copy exists.

Note that `--prune` stops the repo growing but does **not** reclaim space:
deleting a file in a new commit leaves its blob in history, and history is what
GitHub measures. Reclaiming the already-committed gigabytes needs a rewrite:

```bash
git filter-repo --path data/ticks --invert-paths
git push --force origin main
```

That rewrites every commit SHA and breaks existing clones, so only run it once
`--archive` reports every file verified.

## Granularity: what "most granular" means here

1-minute OHLCV is the finest resolution available without a paid market-data
subscription. True **tick-level data** (every individual trade and quote)
requires a paid feed — e.g. Polygon.io, Databento, Alpaca, or IEX Cloud. If
you later get an API key for one of those, `collect.py` is the single place to
plug it in; the per-day-file layout works the same for tick data.
