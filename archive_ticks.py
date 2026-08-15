#!/usr/bin/env python3
"""
Offload the LSEG tick archive from this repository to a local folder.

Why: the tick files are gzipped, so git cannot delta-compress them. Every run
that rewrites a day's file stores a fresh full blob, which grew this repo to
~2.2 GB by 2026-08-15 at roughly 107 MB/day. GitHub's soft limit is 1 GB and
the hard limit is 5 GB.

The intended workflow, run when the limit nears:

    python archive_ticks.py --status                  # how much headroom is left
    python archive_ticks.py --archive                 # copy + verify to local folder
    python archive_ticks.py --prune-older-than 14     # untrack what is safely archived

IMPORTANT: --prune stops the repo from growing further, but it does NOT
reclaim space. Deleting a file in a new commit leaves its blob in history, and
history is what GitHub measures. Reclaiming the already-committed gigabytes
requires rewriting history and force-pushing:

    git filter-repo --path data/ticks --invert-paths
    git push --force origin main

That rewrites every commit SHA and breaks existing clones, so only do it once
--archive reports every file verified. Nothing here force-pushes for you.

Verification is sha256 plus a full decompress-and-parse of the archived copy,
so a file is only ever untracked after its local copy is proven readable.
"""

import argparse
import hashlib
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent
TICKS_DIR = REPO_ROOT / "data" / "ticks"
ARCHIVE_DIR = Path("C:/Users/test/Desktop/IT/SPCX_ticks_archive")
SOFT_LIMIT_MB = 1024
HARD_LIMIT_MB = 5120


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def verify_copy(src, dst):
    """True only if dst matches src byte-for-byte AND parses as a tick file."""
    if not dst.exists() or sha256(src) != sha256(dst):
        return False
    try:
        pd.read_csv(dst)          # also proves the gzip stream is intact
        return True
    except Exception as exc:      # noqa: BLE001
        print(f"  {dst.name}: archived copy is unreadable ({exc})")
        return False


def archive(archive_dir):
    """Copy every tick file into archive_dir and verify it. Returns verified names."""
    archive_dir.mkdir(parents=True, exist_ok=True)
    verified, failed = [], []
    for src in sorted(TICKS_DIR.iterdir()):
        dst = archive_dir / src.name
        if not verify_copy(src, dst):
            shutil.copy2(src, dst)
            if not verify_copy(src, dst):
                failed.append(src.name)
                continue
        verified.append(src.name)
    print(f"[archive] {len(verified)} files verified in {archive_dir}")
    if failed:
        print(f"[archive] FAILED to archive: {failed}")
    return verified


def file_date(name):
    """SPCX_2026-08-14.csv.gz -> date(2026, 8, 14); None if unparseable."""
    try:
        return datetime.strptime(name.split("_")[1][:10], "%Y-%m-%d").date()
    except (IndexError, ValueError):
        return None


def prune(days, archive_dir):
    """git rm --cached tick files older than `days`, but only verified ones."""
    verified = set(archive(archive_dir))
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=days)
    stale = [p for p in sorted(TICKS_DIR.iterdir())
             if (d := file_date(p.name)) and d < cutoff]
    unsafe = [p.name for p in stale if p.name not in verified]
    if unsafe:
        sys.exit(f"[prune] refusing to untrack unverified files: {unsafe}")
    if not stale:
        print(f"[prune] nothing older than {cutoff}")
        return
    subprocess.run(["git", "rm", "--cached", "-q", *[str(p) for p in stale]],
                   cwd=REPO_ROOT, check=True)
    print(f"[prune] untracked {len(stale)} files older than {cutoff}; "
          f"local copies stay in {archive_dir}")
    print("[prune] commit this, then see the module docstring on reclaiming "
          "the space already in history (requires a force-push).")


def status():
    """Report GitHub's measured repo size against the soft/hard limits."""
    out = subprocess.run(
        ["gh", "api", "repos/marsierz-ui/SPCX_data", "--jq", ".size"],
        capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"[status] gh failed: {out.stderr.strip()}")
    mb = int(out.stdout.strip()) / 1024
    local = sum(p.stat().st_size for p in TICKS_DIR.iterdir()) / 1048576
    print(f"[status] GitHub repo size : {mb:,.0f} MB "
          f"({mb / HARD_LIMIT_MB * 100:.0f}% of the {HARD_LIMIT_MB} MB hard limit)")
    print(f"[status] soft limit       : {SOFT_LIMIT_MB} MB "
          f"({'exceeded' if mb > SOFT_LIMIT_MB else 'ok'})")
    print(f"[status] ticks in worktree: {local:,.0f} MB across "
          f"{len(list(TICKS_DIR.iterdir()))} files")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archive-dir", default=str(ARCHIVE_DIR))
    ap.add_argument("--status", action="store_true", help="show size vs limits")
    ap.add_argument("--archive", action="store_true",
                    help="copy + verify ticks into the archive folder")
    ap.add_argument("--prune-older-than", type=int, metavar="DAYS",
                    help="untrack archived tick files older than DAYS")
    args = ap.parse_args()

    if args.status:
        status()
    if args.archive:
        archive(Path(args.archive_dir))
    if args.prune_older_than is not None:
        prune(args.prune_older_than, Path(args.archive_dir))
    if not (args.status or args.archive or args.prune_older_than is not None):
        ap.print_help()


if __name__ == "__main__":
    main()
