"""CSV read/write/merge utilities shared across SPCX collectors."""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd


def read_timestamped_csv(
    path: Path,
    *,
    ts_column: str = "timestamp",
    tz: str | None = None,
    index: bool = True,
) -> pd.DataFrame | None:
    """Read a CSV with a parsed datetime column, optionally timezone-converted.

    Parameters
    ----------
    path : Path
        File to read. Returns None if the file does not exist.
    ts_column : str
        Name of the timestamp column (default "timestamp").
    tz : str or None
        If set, convert the timestamp to this timezone (e.g. "America/New_York", "UTC").
    index : bool
        If True, the timestamp column becomes the DataFrame index.

    Returns
    -------
    pd.DataFrame or None
    """
    if not path.exists():
        return None
    if index:
        df = pd.read_csv(path, index_col=ts_column, parse_dates=True)
        if tz:
            df.index = pd.DatetimeIndex(df.index).tz_convert(tz)
        return df
    df = pd.read_csv(path, parse_dates=[ts_column])
    if tz:
        df[ts_column] = pd.to_datetime(df[ts_column], utc=True)
    return df


def merge_deduplicate(
    existing: pd.DataFrame,
    new: pd.DataFrame,
    *,
    ts_column: str | None = None,
    keep: str = "last",
) -> pd.DataFrame:
    """Concatenate two DataFrames and deduplicate on timestamps.

    Parameters
    ----------
    existing : pd.DataFrame
        Previously stored data.
    new : pd.DataFrame
        Freshly fetched data.
    ts_column : str or None
        If None, deduplicate on the index (assumed to be the timestamp).
        If a column name, deduplicate on that column and sort by it.
    keep : str
        Which duplicate to keep ("first" or "last"). Default "last" so that
        freshly fetched data overwrites partial/stale earlier entries.

    Returns
    -------
    pd.DataFrame
        Merged, deduplicated, and sorted result.
    """
    combined = pd.concat([existing, new])
    if ts_column is None:
        # Deduplicate on index
        combined = combined[~combined.index.duplicated(keep=keep)]
        return combined.sort_index()
    # Deduplicate on a named column
    combined = (
        combined
        .drop_duplicates(subset=ts_column, keep=keep)
        .sort_values(ts_column)
        .reset_index(drop=True)
    )
    return combined


def atomic_write_csv(df: pd.DataFrame, path: Path, **kwargs) -> None:
    """Write a DataFrame to CSV atomically (temp file + rename).

    This prevents partial writes from corrupting the file if the process
    is interrupted mid-write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_csv(tmp, **kwargs)
    os.replace(tmp, path)
