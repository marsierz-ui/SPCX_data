"""Unit tests for collect.py — Yahoo Finance / LSEG data collection module."""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, call, patch

import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Remove LSEG env vars so tests start with a clean slate."""
    for key in ("LSEG_APP_KEY", "LSEG_MACHINE_ID", "LSEG_PASSWORD",
                "LSEG_BACKFILL_DAYS", "LSEG_MAX_PAGES_PER_RUN"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def bars_df():
    """Minimal 1-minute bars DataFrame resembling Yahoo output."""
    idx = pd.DatetimeIndex(
        ["2026-06-12 09:30:00", "2026-06-12 09:31:00", "2026-06-12 09:32:00"],
        tz="America/New_York",
    )
    idx.name = "timestamp"
    return pd.DataFrame(
        {"open": [135.0, 136.0, 137.0],
         "high": [136.0, 137.0, 138.0],
         "low": [134.5, 135.5, 136.5],
         "close": [135.5, 136.5, 137.5],
         "volume": [1000, 2000, 3000]},
        index=idx,
    )


# ---------------------------------------------------------------------------
# load_dotenv
# ---------------------------------------------------------------------------

class TestLoadDotenv:
    def test_loads_vars_into_env(self, tmp_path, monkeypatch):
        from collect import load_dotenv

        env_file = tmp_path / ".env"
        env_file.write_text("FOO=bar\nBAZ=qux\n")
        monkeypatch.delenv("FOO", raising=False)
        monkeypatch.delenv("BAZ", raising=False)

        load_dotenv(env_file)

        assert os.environ["FOO"] == "bar"
        assert os.environ["BAZ"] == "qux"

    def test_does_not_override_existing(self, tmp_path, monkeypatch):
        from collect import load_dotenv

        env_file = tmp_path / ".env"
        env_file.write_text("EXISTING=new_value\n")
        monkeypatch.setenv("EXISTING", "old_value")

        load_dotenv(env_file)

        assert os.environ["EXISTING"] == "old_value"

    def test_skips_comments_and_blanks(self, tmp_path, monkeypatch):
        from collect import load_dotenv

        env_file = tmp_path / ".env"
        env_file.write_text("# comment\n\n  \nVALID=yes\n")
        monkeypatch.delenv("VALID", raising=False)

        load_dotenv(env_file)

        assert os.environ["VALID"] == "yes"

    def test_strips_quotes(self, tmp_path, monkeypatch):
        from collect import load_dotenv

        env_file = tmp_path / ".env"
        env_file.write_text("SINGLE='hello'\nDOUBLE=\"world\"\n")
        monkeypatch.delenv("SINGLE", raising=False)
        monkeypatch.delenv("DOUBLE", raising=False)

        load_dotenv(env_file)

        assert os.environ["SINGLE"] == "hello"
        assert os.environ["DOUBLE"] == "world"

    def test_missing_file_is_noop(self, tmp_path):
        from collect import load_dotenv

        load_dotenv(tmp_path / "nonexistent")  # should not raise


# ---------------------------------------------------------------------------
# lseg_credentials_present
# ---------------------------------------------------------------------------

class TestLsegCredentialsPresent:
    def test_false_when_no_key(self):
        from collect import lseg_credentials_present
        assert lseg_credentials_present() is False

    def test_true_when_key_set(self, monkeypatch):
        from collect import lseg_credentials_present
        monkeypatch.setenv("LSEG_APP_KEY", "test-key")
        assert lseg_credentials_present() is True


# ---------------------------------------------------------------------------
# merge_bars_into_file
# ---------------------------------------------------------------------------

class TestMergeBarsIntoFile:
    def test_creates_new_file(self, tmp_path, bars_df, monkeypatch):
        import collect
        bars_file = tmp_path / "1min" / "SPCX_1min.csv"
        monkeypatch.setattr(collect, "BARS_FILE", bars_file)

        added = collect.merge_bars_into_file(bars_df)

        assert added == 3
        assert bars_file.exists()
        result = pd.read_csv(bars_file, index_col="timestamp", parse_dates=True)
        assert len(result) == 3

    def test_merges_with_existing(self, tmp_path, bars_df, monkeypatch):
        import collect
        bars_file = tmp_path / "1min" / "SPCX_1min.csv"
        monkeypatch.setattr(collect, "BARS_FILE", bars_file)

        # Write initial bars
        collect.merge_bars_into_file(bars_df)

        # New bars with overlap + new data
        new_idx = pd.DatetimeIndex(
            ["2026-06-12 09:32:00", "2026-06-12 09:33:00"],
            tz="America/New_York",
        )
        new_idx.name = "timestamp"
        new_bars = pd.DataFrame(
            {"open": [137.1, 138.0],
             "high": [138.1, 139.0],
             "low": [136.6, 137.5],
             "close": [137.6, 138.5],
             "volume": [3100, 4000]},
            index=new_idx,
        )
        added = collect.merge_bars_into_file(new_bars)

        assert added == 1  # only 09:33 is truly new
        result = pd.read_csv(bars_file, index_col="timestamp", parse_dates=True)
        assert len(result) == 4

    def test_dedup_keeps_last(self, tmp_path, bars_df, monkeypatch):
        import collect
        bars_file = tmp_path / "1min" / "SPCX_1min.csv"
        monkeypatch.setattr(collect, "BARS_FILE", bars_file)
        collect.merge_bars_into_file(bars_df)

        # Re-fetch same timestamps with updated close
        updated = bars_df.copy()
        updated["close"] = [999.0, 998.0, 997.0]
        collect.merge_bars_into_file(updated)

        result = pd.read_csv(bars_file, index_col="timestamp", parse_dates=True)
        assert list(result["close"]) == [999.0, 998.0, 997.0]


# ---------------------------------------------------------------------------
# fetch_minute_bars
# ---------------------------------------------------------------------------

class TestFetchMinuteBars:
    def test_returns_cleaned_frame(self, bars_df):
        from collect import fetch_minute_bars

        raw = bars_df.copy()
        raw.columns = [c.capitalize() for c in raw.columns]
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = raw

        result = fetch_minute_bars(mock_ticker)

        assert list(result.columns) == ["open", "high", "low", "close", "volume"]
        assert result.index.name == "timestamp"
        assert len(result) == 3

    def test_returns_empty_when_no_data(self):
        from collect import fetch_minute_bars

        mock_ticker = MagicMock()
        mock_ticker.history.return_value = pd.DataFrame()

        result = fetch_minute_bars(mock_ticker)

        assert result.empty


# ---------------------------------------------------------------------------
# append_quote_snapshot
# ---------------------------------------------------------------------------

class TestAppendQuoteSnapshot:
    def test_creates_snapshot_file(self, tmp_path, monkeypatch):
        import collect
        snap_file = tmp_path / "snapshots" / "SPCX_quotes.csv"
        monkeypatch.setattr(collect, "SNAPSHOT_FILE", snap_file)

        mock_ticker = MagicMock()
        mock_ticker.fast_info.last_price = 135.50
        mock_ticker.fast_info.last_volume = 500000
        mock_ticker.fast_info.day_high = 140.0
        mock_ticker.fast_info.day_low = 130.0
        mock_ticker.fast_info.previous_close = 135.0

        collect.append_quote_snapshot(mock_ticker)

        assert snap_file.exists()
        df = pd.read_csv(snap_file)
        assert len(df) == 1
        assert df["last_price"].iloc[0] == 135.50

    def test_appends_to_existing(self, tmp_path, monkeypatch):
        import collect
        snap_file = tmp_path / "snapshots" / "SPCX_quotes.csv"
        monkeypatch.setattr(collect, "SNAPSHOT_FILE", snap_file)

        mock_ticker = MagicMock()
        mock_ticker.fast_info.last_price = 135.50
        mock_ticker.fast_info.last_volume = 500000
        mock_ticker.fast_info.day_high = 140.0
        mock_ticker.fast_info.day_low = 130.0
        mock_ticker.fast_info.previous_close = 135.0

        collect.append_quote_snapshot(mock_ticker)
        collect.append_quote_snapshot(mock_ticker)

        df = pd.read_csv(snap_file)
        assert len(df) == 2

    def test_skips_when_no_price(self, tmp_path, monkeypatch, capsys):
        import collect
        snap_file = tmp_path / "snapshots" / "SPCX_quotes.csv"
        monkeypatch.setattr(collect, "SNAPSHOT_FILE", snap_file)

        mock_ticker = MagicMock()
        mock_ticker.fast_info.last_price = None

        collect.append_quote_snapshot(mock_ticker)

        assert not snap_file.exists()
        assert "skipping" in capsys.readouterr().out.lower()


# ---------------------------------------------------------------------------
# read_existing_ticks
# ---------------------------------------------------------------------------

class TestReadExistingTicks:
    def test_returns_none_for_missing_file(self, tmp_path):
        from collect import read_existing_ticks
        assert read_existing_ticks(tmp_path / "nope.csv") is None

    def test_reads_csv_with_tz(self, tmp_path):
        from collect import read_existing_ticks

        idx = pd.DatetimeIndex(
            ["2026-06-12 13:30:00+00:00", "2026-06-12 13:31:00+00:00"],
        )
        idx.name = "timestamp"
        df = pd.DataFrame({"TRDPRC_1": [135.0, 136.0]}, index=idx)
        path = tmp_path / "ticks.csv"
        df.to_csv(path)

        result = read_existing_ticks(path)
        assert result is not None
        assert len(result) == 2
        assert str(result.index.tz) == "America/New_York"


# ---------------------------------------------------------------------------
# main orchestration
# ---------------------------------------------------------------------------

class TestMain:
    def test_yahoo_fallback_when_no_lseg(self, monkeypatch):
        import collect

        monkeypatch.delenv("LSEG_APP_KEY", raising=False)
        mock_yahoo = MagicMock()
        monkeypatch.setattr(collect, "collect_yahoo_bars", mock_yahoo)

        ret = collect.main()

        assert ret == 0
        mock_yahoo.assert_called_once()

    def test_lseg_path_when_key_present(self, monkeypatch):
        import collect

        monkeypatch.setenv("LSEG_APP_KEY", "test-key")
        mock_lseg = MagicMock()
        monkeypatch.setattr(collect, "collect_lseg_ticks", mock_lseg)

        ret = collect.main()

        assert ret == 0
        mock_lseg.assert_called_once()

    def test_lseg_failure_falls_back_to_yahoo(self, monkeypatch):
        import collect

        monkeypatch.setenv("LSEG_APP_KEY", "test-key")
        monkeypatch.setattr(collect, "collect_lseg_ticks",
                            MagicMock(side_effect=RuntimeError("auth failed")))
        mock_yahoo = MagicMock()
        monkeypatch.setattr(collect, "collect_yahoo_bars", mock_yahoo)

        ret = collect.main()

        assert ret == 0
        mock_yahoo.assert_called_once()


# ---------------------------------------------------------------------------
# collect_yahoo_bars (integration of fetch + merge + snapshot)
# ---------------------------------------------------------------------------

class TestCollectYahooBars:
    def _make_yf_mock(self, ticker_mock):
        yf_mock = MagicMock()
        yf_mock.Ticker.return_value = ticker_mock
        return yf_mock

    def test_fetches_merges_and_snapshots(self, tmp_path, bars_df, monkeypatch, capsys):
        import collect

        bars_file = tmp_path / "1min" / "SPCX_1min.csv"
        snap_file = tmp_path / "snapshots" / "SPCX_quotes.csv"
        monkeypatch.setattr(collect, "BARS_FILE", bars_file)
        monkeypatch.setattr(collect, "SNAPSHOT_FILE", snap_file)

        mock_ticker = MagicMock()
        raw = bars_df.copy()
        raw.columns = [c.capitalize() for c in raw.columns]
        mock_ticker.history.return_value = raw
        mock_ticker.fast_info.last_price = 137.5
        mock_ticker.fast_info.last_volume = 6000
        mock_ticker.fast_info.day_high = 138.0
        mock_ticker.fast_info.day_low = 134.5
        mock_ticker.fast_info.previous_close = 135.0

        yf_mock = self._make_yf_mock(mock_ticker)
        with patch.dict(sys.modules, {"yfinance": yf_mock}):
            collect.collect_yahoo_bars()

        assert bars_file.exists()
        assert snap_file.exists()
        out = capsys.readouterr().out
        assert "3 bars" in out or "Fetched" in out

    def test_empty_bars_prints_warning(self, monkeypatch, capsys):
        import collect

        mock_ticker = MagicMock()
        mock_ticker.history.return_value = pd.DataFrame()
        mock_ticker.fast_info.last_price = None

        yf_mock = self._make_yf_mock(mock_ticker)
        with patch.dict(sys.modules, {"yfinance": yf_mock}):
            collect.collect_yahoo_bars()

        out = capsys.readouterr().out
        assert "WARNING" in out

    def test_snapshot_failure_is_nonfatal(self, tmp_path, bars_df, monkeypatch, capsys):
        import collect

        bars_file = tmp_path / "1min" / "SPCX_1min.csv"
        monkeypatch.setattr(collect, "BARS_FILE", bars_file)

        mock_ticker = MagicMock()
        raw = bars_df.copy()
        raw.columns = [c.capitalize() for c in raw.columns]
        mock_ticker.history.return_value = raw

        yf_mock = self._make_yf_mock(mock_ticker)
        with patch.dict(sys.modules, {"yfinance": yf_mock}), \
             patch.object(collect, "append_quote_snapshot",
                          side_effect=RuntimeError("snapshot boom")):
            collect.collect_yahoo_bars()

        assert bars_file.exists()
        assert "snapshot failed" in capsys.readouterr().out.lower()


# ---------------------------------------------------------------------------
# LSEG: open_lseg_session
# ---------------------------------------------------------------------------

class TestOpenLsegSession:
    def _make_lseg_mocks(self):
        """Build fake lseg + lseg.data modules wired together."""
        ld = MagicMock()
        ld.OpenState.Opened = "OPENED"
        lseg_mock = MagicMock()
        lseg_mock.data = ld
        return ld, lseg_mock

    def test_desktop_session_when_only_app_key(self, monkeypatch):
        import collect

        monkeypatch.setenv("LSEG_APP_KEY", "my-key")
        monkeypatch.delenv("LSEG_MACHINE_ID", raising=False)
        monkeypatch.delenv("LSEG_PASSWORD", raising=False)

        ld, lseg_mock = self._make_lseg_mocks()
        session = MagicMock()
        session.open.return_value = "OPENED"
        ld.session.desktop.Definition.return_value.get_session.return_value = session

        with patch.dict(sys.modules, {"lseg.data": ld, "lseg": lseg_mock}):
            result = collect.open_lseg_session()

        assert result is session
        ld.session.desktop.Definition.assert_called_once_with(app_key="my-key")

    def test_platform_session_when_all_creds(self, monkeypatch):
        import collect

        monkeypatch.setenv("LSEG_APP_KEY", "my-key")
        monkeypatch.setenv("LSEG_MACHINE_ID", "GE-A-123")
        monkeypatch.setenv("LSEG_PASSWORD", "secret")

        ld, lseg_mock = self._make_lseg_mocks()
        session = MagicMock()
        session.open.return_value = "OPENED"
        ld.session.platform.Definition.return_value.get_session.return_value = session

        with patch.dict(sys.modules, {"lseg.data": ld, "lseg": lseg_mock}):
            result = collect.open_lseg_session()

        assert result is session
        ld.session.platform.Definition.assert_called_once()

    def test_raises_when_session_fails(self, monkeypatch):
        import collect

        monkeypatch.setenv("LSEG_APP_KEY", "my-key")
        monkeypatch.delenv("LSEG_MACHINE_ID", raising=False)

        ld, lseg_mock = self._make_lseg_mocks()
        session = MagicMock()
        session.open.return_value = "FAILED"
        ld.session.desktop.Definition.return_value.get_session.return_value = session

        with patch.dict(sys.modules, {"lseg.data": ld, "lseg": lseg_mock}), \
             pytest.raises(RuntimeError, match="LSEG session failed"):
            collect.open_lseg_session()


# ---------------------------------------------------------------------------
# LSEG: fetch_ticks_window
# ---------------------------------------------------------------------------

class TestFetchTicksWindow:
    def _make_lseg_mocks(self):
        ld = MagicMock()
        lseg_mock = MagicMock()
        lseg_mock.data = ld
        return ld, lseg_mock

    def test_fetches_pages_backwards(self, monkeypatch):
        import collect

        ld, lseg_mock = self._make_lseg_mocks()
        page1_idx = pd.DatetimeIndex(
            ["2026-06-12 10:00:00", "2026-06-12 09:30:00"], tz="UTC",
        )
        page1 = pd.DataFrame(
            {"TRDPRC_1": [136.0, 135.0], "TRDVOL_1": [100, 200],
             "BID": [135.9, 134.9], "ASK": [136.1, 135.1]},
            index=page1_idx,
        )
        # Second call returns empty → done
        ld.get_history.side_effect = [page1, pd.DataFrame()]

        start = pd.Timestamp("2026-06-12 09:30:00", tz="America/New_York")
        end = pd.Timestamp("2026-06-12 10:01:00", tz="America/New_York")

        with patch.dict(sys.modules, {"lseg.data": ld, "lseg": lseg_mock}):
            pages, used = collect.fetch_ticks_window(start, end, budget=5)

        assert len(pages) == 1
        assert used == 1  # page covers the entire window so loop ends

    def test_stops_on_budget(self, monkeypatch):
        import collect

        ld, lseg_mock = self._make_lseg_mocks()
        page_idx = pd.DatetimeIndex(["2026-06-12 09:30:00"], tz="UTC")
        page = pd.DataFrame(
            {"TRDPRC_1": [135.0], "TRDVOL_1": [100],
             "BID": [134.9], "ASK": [135.1]},
            index=page_idx,
        )
        ld.get_history.return_value = page

        start = pd.Timestamp("2026-06-12 09:00:00", tz="America/New_York")
        end = pd.Timestamp("2026-06-12 10:00:00", tz="America/New_York")

        with patch.dict(sys.modules, {"lseg.data": ld, "lseg": lseg_mock}):
            pages, used = collect.fetch_ticks_window(start, end, budget=1)

        assert used == 1

    def test_stops_on_no_progress(self, monkeypatch):
        import collect

        ld, lseg_mock = self._make_lseg_mocks()
        # Page always returns a timestamp AT the cursor → no progress
        ts = pd.Timestamp("2026-06-12 10:00:00", tz="UTC")
        page = pd.DataFrame(
            {"TRDPRC_1": [135.0], "TRDVOL_1": [100],
             "BID": [134.9], "ASK": [135.1]},
            index=pd.DatetimeIndex([ts]),
        )
        ld.get_history.return_value = page

        start = pd.Timestamp("2026-06-12 09:00:00", tz="America/New_York")
        end = pd.Timestamp("2026-06-12 10:01:00", tz="America/New_York")

        with patch.dict(sys.modules, {"lseg.data": ld, "lseg": lseg_mock}):
            pages, used = collect.fetch_ticks_window(start, end, budget=10)

        assert used == 1
        assert len(pages) == 1


# ---------------------------------------------------------------------------
# LSEG: fetch_ticks_for_day
# ---------------------------------------------------------------------------

class TestFetchTicksForDay:
    def _make_tick_page(self, timestamps, tz="America/New_York"):
        idx = pd.DatetimeIndex(timestamps, tz=tz)
        idx.name = "timestamp"
        return pd.DataFrame(
            {"TRDPRC_1": [135.0] * len(timestamps),
             "TRDVOL_1": [100] * len(timestamps),
             "BID": [134.9] * len(timestamps),
             "ASK": [135.1] * len(timestamps)},
            index=idx,
        )

    def test_cold_start_no_existing(self, monkeypatch):
        import collect

        page = self._make_tick_page(["2026-06-12 09:30:00", "2026-06-12 09:31:00"])

        with patch.object(collect, "fetch_ticks_window",
                          return_value=([page], 1)):
            ticks, used = collect.fetch_ticks_for_day(
                "2026-06-12", None, budget=5)

        assert len(ticks) == 2
        assert used == 1

    def test_incremental_with_existing(self, monkeypatch):
        import collect

        existing = self._make_tick_page(["2026-06-12 09:30:00"])
        newer = self._make_tick_page(["2026-06-12 09:31:00"])

        with patch.object(collect, "fetch_ticks_window") as mock_ftw:
            mock_ftw.side_effect = [([newer], 1), ([], 0)]
            ticks, used = collect.fetch_ticks_for_day(
                "2026-06-12", existing, budget=5)

        assert len(ticks) == 2
        assert used == 1

    def test_returns_empty_when_no_pages(self, monkeypatch):
        import collect

        with patch.object(collect, "fetch_ticks_window",
                          return_value=([], 1)):
            ticks, used = collect.fetch_ticks_for_day(
                "2026-06-12", None, budget=5)

        assert ticks.empty


# ---------------------------------------------------------------------------
# LSEG: collect_lseg_ticks
# ---------------------------------------------------------------------------

class TestCollectLsegTicks:
    def test_writes_tick_files(self, tmp_path, monkeypatch):
        import collect

        monkeypatch.setattr(collect, "TICKS_DIR", tmp_path / "ticks")
        monkeypatch.setattr(collect, "LSEG_BACKFILL_DAYS", 0)
        monkeypatch.setattr(collect, "LSEG_MAX_PAGES_PER_RUN", 5)

        mock_session = MagicMock()
        idx = pd.DatetimeIndex(["2026-06-12 09:30:00"], tz="America/New_York")
        idx.name = "timestamp"
        tick_df = pd.DataFrame(
            {"TRDPRC_1": [135.0], "TRDVOL_1": [100],
             "BID": [134.9], "ASK": [135.1]},
            index=idx,
        )

        with patch.object(collect, "open_lseg_session", return_value=mock_session), \
             patch.object(collect, "fetch_ticks_for_day",
                          return_value=(tick_df, 1)):
            collect.collect_lseg_ticks()

        mock_session.close.assert_called_once()

    def test_budget_exhaustion_stops_early(self, tmp_path, monkeypatch, capsys):
        import collect

        monkeypatch.setattr(collect, "TICKS_DIR", tmp_path / "ticks")
        monkeypatch.setattr(collect, "LSEG_BACKFILL_DAYS", 2)
        monkeypatch.setattr(collect, "LSEG_MAX_PAGES_PER_RUN", 1)

        mock_session = MagicMock()
        idx = pd.DatetimeIndex(["2026-06-12 09:30:00"], tz="America/New_York")
        idx.name = "timestamp"
        tick_df = pd.DataFrame(
            {"TRDPRC_1": [135.0], "TRDVOL_1": [100],
             "BID": [134.9], "ASK": [135.1]},
            index=idx,
        )

        with patch.object(collect, "open_lseg_session", return_value=mock_session), \
             patch.object(collect, "fetch_ticks_for_day",
                          return_value=(tick_df, 1)):
            collect.collect_lseg_ticks()

        assert "budget exhausted" in capsys.readouterr().out.lower()

    def test_session_closed_on_error(self, tmp_path, monkeypatch):
        import collect

        monkeypatch.setattr(collect, "TICKS_DIR", tmp_path / "ticks")
        monkeypatch.setattr(collect, "LSEG_BACKFILL_DAYS", 0)
        monkeypatch.setattr(collect, "LSEG_MAX_PAGES_PER_RUN", 5)

        mock_session = MagicMock()

        with patch.object(collect, "open_lseg_session", return_value=mock_session), \
             patch.object(collect, "fetch_ticks_for_day",
                          side_effect=RuntimeError("boom")), \
             pytest.raises(RuntimeError):
            collect.collect_lseg_ticks()

        mock_session.close.assert_called_once()
