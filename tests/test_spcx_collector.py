"""Unit tests for spcx_collector.py — Hyperliquid perp candle collector."""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# interval_ms
# ---------------------------------------------------------------------------

class TestIntervalMs:
    def test_one_minute(self):
        from spcx_collector import interval_ms
        assert interval_ms("1m") == 60_000

    def test_five_minutes(self):
        from spcx_collector import interval_ms
        assert interval_ms("5m") == 300_000

    def test_one_hour(self):
        from spcx_collector import interval_ms
        assert interval_ms("1h") == 3_600_000

    def test_one_day(self):
        from spcx_collector import interval_ms
        assert interval_ms("1d") == 86_400_000

    def test_one_week(self):
        from spcx_collector import interval_ms
        assert interval_ms("1w") == 604_800_000

    def test_one_month(self):
        from spcx_collector import interval_ms
        assert interval_ms("1M") == 30 * 86_400_000


# ---------------------------------------------------------------------------
# candles_to_frame
# ---------------------------------------------------------------------------

class TestCandlesToFrame:
    def test_empty_dict_returns_empty_frame(self):
        from spcx_collector import candles_to_frame, COLUMNS
        result = candles_to_frame({})
        assert list(result.columns) == COLUMNS
        assert result.empty

    def test_converts_candle_dict(self):
        from spcx_collector import candles_to_frame

        candles = {
            1000: {"t": 1000, "o": "100.5", "h": "101.0",
                   "l": "99.5", "c": "100.0", "v": "5000", "n": "42"},
            2000: {"t": 2000, "o": "100.0", "h": "102.0",
                   "l": "99.0", "c": "101.5", "v": "6000", "n": "55"},
        }
        result = candles_to_frame(candles)

        assert len(result) == 2
        assert result["open"].iloc[0] == 100.5
        assert result["close"].iloc[1] == 101.5
        assert result["trades"].iloc[0] == 42

    def test_sorts_by_timestamp(self):
        from spcx_collector import candles_to_frame

        candles = {
            3000: {"t": 3000, "o": "1", "h": "1", "l": "1", "c": "1",
                   "v": "1", "n": "1"},
            1000: {"t": 1000, "o": "2", "h": "2", "l": "2", "c": "2",
                   "v": "2", "n": "2"},
        }
        result = candles_to_frame(candles)

        assert result["ts"].iloc[0] < result["ts"].iloc[1]

    def test_coerces_non_numeric(self):
        from spcx_collector import candles_to_frame

        candles = {
            1000: {"t": 1000, "o": "bad", "h": "101", "l": "99",
                   "c": "100", "v": "5000", "n": "10"},
        }
        result = candles_to_frame(candles)
        assert pd.isna(result["open"].iloc[0])
        assert result["high"].iloc[0] == 101.0


# ---------------------------------------------------------------------------
# load_master
# ---------------------------------------------------------------------------

class TestLoadMaster:
    def test_returns_empty_for_missing_file(self, tmp_path):
        from spcx_collector import load_master, COLUMNS
        result = load_master(tmp_path / "nope.csv")
        assert result.empty
        assert list(result.columns) == COLUMNS

    def test_reads_existing_csv(self, tmp_path):
        from spcx_collector import load_master

        path = tmp_path / "master.csv"
        ts = pd.Timestamp("2026-06-12 09:30:00", tz="UTC")
        df = pd.DataFrame({"ts": [ts], "open": [135.0], "high": [136.0],
                           "low": [134.0], "close": [135.5],
                           "volume": [1000], "trades": [50]})
        df.to_csv(path, index=False)

        result = load_master(path)
        assert len(result) == 1
        assert result["ts"].iloc[0].tzinfo is not None


# ---------------------------------------------------------------------------
# atomic_write_csv
# ---------------------------------------------------------------------------

class TestAtomicWriteCsv:
    def test_writes_csv_atomically(self, tmp_path):
        from spcx_collector import atomic_write_csv

        path = tmp_path / "out.csv"
        df = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})

        atomic_write_csv(df, path)

        assert path.exists()
        assert not path.with_suffix(".csv.tmp").exists()
        result = pd.read_csv(path)
        assert len(result) == 3

    def test_overwrites_existing(self, tmp_path):
        from spcx_collector import atomic_write_csv

        path = tmp_path / "out.csv"
        atomic_write_csv(pd.DataFrame({"x": [1]}), path)
        atomic_write_csv(pd.DataFrame({"x": [10, 20]}), path)

        result = pd.read_csv(path)
        assert len(result) == 2
        assert result["x"].iloc[0] == 10


# ---------------------------------------------------------------------------
# post (HTTP helper)
# ---------------------------------------------------------------------------

class TestPost:
    def test_success(self):
        from spcx_collector import post

        payload = {"type": "meta"}
        resp_data = {"universe": []}

        with patch("spcx_collector.urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(resp_data).encode()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            result = post(payload, retries=1)

        assert result == resp_data

    def test_429_retries(self):
        import urllib.error
        from spcx_collector import post

        http_err = urllib.error.HTTPError(
            url="http://test", code=429, msg="Rate Limited",
            hdrs=None, fp=MagicMock(read=lambda: b"rate limited"),
        )
        success_resp = MagicMock()
        success_resp.read.return_value = b'{"ok": true}'
        success_resp.__enter__ = lambda s: s
        success_resp.__exit__ = MagicMock(return_value=False)

        with patch("spcx_collector.urllib.request.urlopen") as mock_urlopen, \
             patch("spcx_collector.time.sleep"):
            mock_urlopen.side_effect = [http_err, success_resp]
            result = post({"type": "meta"}, retries=2)

        assert result == {"ok": True}

    def test_fatal_http_error_exits(self):
        import urllib.error
        from spcx_collector import post

        http_err = urllib.error.HTTPError(
            url="http://test", code=500, msg="Server Error",
            hdrs=None, fp=MagicMock(read=lambda: b"internal error"),
        )

        with patch("spcx_collector.urllib.request.urlopen") as mock_urlopen, \
             pytest.raises(SystemExit):
            mock_urlopen.side_effect = http_err
            post({"type": "meta"}, retries=1)

    def test_url_error_retries_then_exits(self):
        import urllib.error
        from spcx_collector import post

        url_err = urllib.error.URLError("Connection refused")

        with patch("spcx_collector.urllib.request.urlopen") as mock_urlopen, \
             patch("spcx_collector.time.sleep"), \
             pytest.raises(SystemExit):
            mock_urlopen.side_effect = [url_err, url_err]
            post({"type": "meta"}, retries=2)


# ---------------------------------------------------------------------------
# resolve_coin
# ---------------------------------------------------------------------------

class TestResolveCoin:
    def test_returns_coin_arg_directly(self):
        from spcx_collector import resolve_coin
        assert resolve_coin("SPCX", "myDex:SPCX") == "myDex:SPCX"

    def test_returns_cached_value(self, tmp_path, monkeypatch):
        import spcx_collector
        monkeypatch.setattr(spcx_collector, "COIN_CACHE",
                            tmp_path / ".coin_cache.json")
        (tmp_path / ".coin_cache.json").write_text(json.dumps({"SPCX": "cachedDex:SPCX"}))

        result = spcx_collector.resolve_coin("SPCX", None)
        assert result == "cachedDex:SPCX"

    def test_discovers_and_caches(self, tmp_path, monkeypatch):
        import spcx_collector
        monkeypatch.setattr(spcx_collector, "COIN_CACHE",
                            tmp_path / ".coin_cache.json")
        monkeypatch.setattr(spcx_collector, "OUTDIR", tmp_path)
        monkeypatch.setattr(spcx_collector, "discover_coin",
                            lambda sym: "newDex:SPCX")

        result = spcx_collector.resolve_coin("SPCX", None)
        assert result == "newDex:SPCX"
        assert (tmp_path / ".coin_cache.json").exists()
        cached = json.loads((tmp_path / ".coin_cache.json").read_text())
        assert cached["SPCX"] == "newDex:SPCX"


# ---------------------------------------------------------------------------
# discover_coin
# ---------------------------------------------------------------------------

class TestDiscoverCoin:
    def test_finds_matching_coin(self):
        from spcx_collector import discover_coin

        meta_resp = {"universe": [{"name": "SPCX"}, {"name": "BTC"}]}
        with patch("spcx_collector.post") as mock_post, \
             patch("spcx_collector.time.sleep"):
            # First call: perpDexs → one unnamed dex
            # Second call: meta → universe with coins
            mock_post.side_effect = [[None], meta_resp]
            result = discover_coin("SPCX")

        assert "SPCX" in result

    def test_no_match_exits(self):
        from spcx_collector import discover_coin

        meta_resp = {"universe": [{"name": "BTC"}, {"name": "ETH"}]}
        with patch("spcx_collector.post") as mock_post, \
             patch("spcx_collector.time.sleep"), \
             pytest.raises(SystemExit):
            mock_post.side_effect = [[None], meta_resp]
            discover_coin("SPCX")

    def test_multiple_matches_uses_first(self, capsys):
        from spcx_collector import discover_coin

        meta_resp = {"universe": [
            {"name": "dexA:SPCX"}, {"name": "dexB:SPCX"},
        ]}
        with patch("spcx_collector.post") as mock_post, \
             patch("spcx_collector.time.sleep"):
            mock_post.side_effect = [[None], meta_resp]
            result = discover_coin("SPCX")

        assert result == "dexA:SPCX"
        assert "Multiple" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# fetch_candles
# ---------------------------------------------------------------------------

class TestFetchCandles:
    def test_fetches_and_dedup(self):
        from spcx_collector import fetch_candles

        candle_a = {"t": 1000, "o": "1", "h": "1", "l": "1",
                    "c": "1", "v": "1", "n": "1"}
        candle_b = {"t": 2000, "o": "2", "h": "2", "l": "2",
                    "c": "2", "v": "2", "n": "2"}

        with patch("spcx_collector.post") as mock_post, \
             patch("spcx_collector.time.sleep"):
            mock_post.return_value = [candle_a, candle_b]
            result = fetch_candles("testDex:SPCX", "1m", 0, 300_001)

        assert 1000 in result
        assert 2000 in result

    def test_empty_response(self):
        from spcx_collector import fetch_candles

        with patch("spcx_collector.post") as mock_post, \
             patch("spcx_collector.time.sleep"):
            mock_post.return_value = []
            result = fetch_candles("testDex:SPCX", "1m", 0, 60_001)

        assert result == {}


# ---------------------------------------------------------------------------
# poll_once
# ---------------------------------------------------------------------------

class TestPollOnce:
    def test_cold_start(self, tmp_path):
        from spcx_collector import poll_once

        master_path = tmp_path / "master.csv"
        candle = {"t": 1000, "o": "100", "h": "101", "l": "99",
                  "c": "100.5", "v": "500", "n": "10"}

        with patch("spcx_collector.fetch_candles") as mock_fetch, \
             patch("spcx_collector.time.sleep"):
            mock_fetch.return_value = {1000: candle}
            count = poll_once("testDex:SPCX", "1m", master_path, 3.4)

        assert count == 1
        assert master_path.exists()

    def test_incremental_append(self, tmp_path):
        from spcx_collector import poll_once, atomic_write_csv

        master_path = tmp_path / "master.csv"
        ts = pd.Timestamp("2026-06-12 09:30:00", tz="UTC")
        existing = pd.DataFrame(
            {"ts": [ts], "open": [135.0], "high": [136.0],
             "low": [134.0], "close": [135.5], "volume": [1000],
             "trades": [50]})
        atomic_write_csv(existing, master_path)

        new_candle = {"t": int(ts.value // 1e6) + 60_000,
                      "o": "136", "h": "137", "l": "135",
                      "c": "136.5", "v": "2000", "n": "60"}

        with patch("spcx_collector.fetch_candles") as mock_fetch, \
             patch("spcx_collector.time.sleep"):
            mock_fetch.return_value = {new_candle["t"]: new_candle}
            count = poll_once("testDex:SPCX", "1m", master_path, 3.4)

        assert count == 2

    def test_no_candles_returned(self, tmp_path):
        from spcx_collector import poll_once

        master_path = tmp_path / "master.csv"

        with patch("spcx_collector.fetch_candles") as mock_fetch, \
             patch("spcx_collector.time.sleep"):
            mock_fetch.return_value = {}
            count = poll_once("testDex:SPCX", "1m", master_path, 3.4)

        assert count == 0


# ---------------------------------------------------------------------------
# main (argument parsing + orchestration)
# ---------------------------------------------------------------------------

class TestMainCLI:
    def test_one_shot_mode(self, tmp_path, monkeypatch):
        import spcx_collector

        monkeypatch.setattr(spcx_collector, "OUTDIR", tmp_path)
        monkeypatch.setattr(spcx_collector, "COIN_CACHE",
                            tmp_path / ".coin_cache.json")
        monkeypatch.setattr("sys.argv",
                            ["spcx_collector.py", "--coin", "test:SPCX",
                             "--outdir", str(tmp_path)])

        with patch.object(spcx_collector, "poll_once", return_value=5) as mock_poll:
            spcx_collector.main()

        mock_poll.assert_called_once()
