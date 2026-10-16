"""Offline tests for financial field mapping and cache/pagination correctness."""

import csv
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import subprocess

from backend.service import DataError, InputError, MarketService, UpstreamError, normalize_rows, normalize_symbol, run_worker


def row(day: int, close: float = 10.0) -> dict:
    return {
        "date": f"2026-09-{day:02d}", "open": close - 0.2,
        "high": close + 0.3, "low": close - 0.4, "close": close,
        "volume": 10000.0, "turnover": 0.012, "amount": 102000.0,
    }


class MarketServiceTests(unittest.TestCase):
    def setUp(self):
        # Keep test artifacts inside the checkout (also works in restricted
        # Windows shells whose redirected system temp is not writable).
        test_temp = Path(__file__).resolve().parent / ".tmp"
        test_temp.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=test_temp)
        self.root = Path(self.temporary.name)
        def cleanup():
            if not self.root.resolve().is_relative_to(test_temp.resolve()):
                raise RuntimeError("Refusing cleanup outside the test temporary directory")
            self.temporary.cleanup()
        self.addCleanup(cleanup)
        self.calls = []
        self.rows = [row(day) for day in range(1, 6)]
        self.fail = False
        self.raw_valid = True

        def worker(payload, timeout):
            self.calls.append(payload)
            if self.fail:
                raise UpstreamError("上游连接失败")
            if payload["task"] == "validate_raw":
                return self.raw_valid
            if payload["task"] == "directory":
                return [{"code": "600570", "name": "恒生电子"}, {"code": "920001", "name": "北交所样例"}]
            offset = {"qfq": -12, "none": 0, "hfq": 100}[payload["adjust"]]
            return [row(day, 10 + offset) for day in range(1, 6)]

        self.worker = worker
        self.service = MarketService(self.root, worker)

    def write_csv(self, adjust: str, rows: list[dict]):
        path = self.root / f"sz002027_daily_{adjust}.csv"
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        return path

    def test_exclusive_pagination_has_no_missing_or_duplicate_days(self):
        # Include unsorted data and a revised duplicate; the latest record wins.
        self.write_csv("qfq", [row(5), row(2), row(1), row(3), row(4), row(3, 11)])
        latest = self.service.history("002027", limit=2)
        middle = self.service.history("sz002027", before=latest["bars"][0]["timestamp"], limit=2)
        earliest = self.service.history("002027", before=middle["bars"][0]["timestamp"], limit=2)
        all_bars = earliest["bars"] + middle["bars"] + latest["bars"]
        self.assertEqual(len(all_bars), 5)
        self.assertEqual(len({bar["timestamp"] for bar in all_bars}), 5)
        self.assertEqual(all_bars[2]["close"], 11)
        self.assertTrue(latest["hasMore"])
        self.assertFalse(earliest["hasMore"])
        self.assertEqual(latest["total"], 5)
        self.assertEqual(self.calls, [])

    def test_field_units_and_negative_adjusted_prices_are_preserved(self):
        bar = normalize_rows([row(1, -1.27)])[0]
        self.assertEqual(bar["close"], -1.27)
        self.assertEqual(bar["volume"], 10000)
        self.assertEqual(bar["amount"], 102000)
        self.assertEqual(bar["turnoverRate"], 0.012)
        # 2026-09-01 00:00 Asia/Shanghai, not local-machine or UTC midnight.
        self.assertEqual(bar["timestamp"], 1788192000000)

    def test_raw_and_adjusted_caches_never_mix(self):
        qfq = self.service.history("sz002027", adjust="qfq")
        raw = self.service.history("sz002027", adjust="none")
        hfq = self.service.history("sz002027", adjust="hfq")
        self.assertEqual(qfq["bars"][-1]["close"], -2)
        self.assertEqual(raw["bars"][-1]["close"], 10)
        self.assertEqual(hfq["bars"][-1]["close"], 110)
        # A new service must load each isolated disk cache without the provider.
        self.fail = True
        another = MarketService(self.root, self.worker)
        self.assertEqual(another.history("002027", "none")["bars"][-1]["close"], 10)
        self.assertEqual(another.history("002027", "qfq")["bars"][-1]["close"], -2)

    def test_legacy_bfq_requires_verified_raw_prices(self):
        self.write_csv("bfq", self.rows)
        response = self.service.history("002027", "none")
        self.assertEqual(self.calls[0]["task"], "validate_raw")
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(response["cached"])
        self.assertIn("抽样核验", response["source"])

    def test_mislabeled_bfq_is_not_served_as_raw(self):
        self.write_csv("bfq", [row(1, 999)])
        self.raw_valid = False
        response = self.service.history("002027", "none")
        self.assertEqual(response["bars"][-1]["close"], 10)
        self.assertFalse(response["cached"])
        self.assertEqual([call["task"] for call in self.calls], ["validate_raw", "history"])

    def test_failed_refresh_preserves_data_with_explicit_stale_warning(self):
        original = self.service.history("002027")
        self.fail = True
        fallback = self.service.history("002027", refresh=True)
        self.assertEqual(fallback["bars"], original["bars"])
        self.assertEqual(fallback["fetchedAt"], original["fetchedAt"])
        self.assertTrue(fallback["cached"])
        self.assertTrue(fallback["stale"])
        self.assertIn("刷新失败", fallback["warning"])

    def test_source_failure_without_cache_is_an_error(self):
        self.fail = True
        with self.assertRaises(UpstreamError):
            self.service.history("002027")

    def test_malformed_csv_does_not_become_fake_or_empty_success(self):
        self.write_csv("qfq", [{**row(1), "close": "bad"}])
        with self.assertRaises(DataError):
            self.service.history("002027")
        self.assertEqual(self.calls, [])

    def test_stock_search_supports_general_names_and_codes(self):
        exact = self.service.search("600570")
        self.assertEqual(exact["items"][0]["symbol"], "sh600570")
        self.assertEqual(self.calls, [])
        named = self.service.search("恒生")
        self.assertEqual(named["items"][0]["name"], "恒生电子")
        self.assertFalse(named["limited"])
        self.assertEqual(len(self.service._directory), 1)
        with self.assertRaises(InputError):
            normalize_symbol("bj920001")
        with self.assertRaises(InputError):
            normalize_symbol("sh002027")

    def test_total_worker_deadline_turns_into_a_useful_upstream_error(self):
        with patch("backend.service.subprocess.run", side_effect=subprocess.TimeoutExpired("worker", 7)):
            with self.assertRaisesRegex(UpstreamError, "7 秒"):
                run_worker({"task": "history", "symbol": "sz002027", "adjust": "none"}, timeout=7)

    def test_worker_maps_none_to_documented_empty_adjust(self):
        import pandas as pd
        from types import SimpleNamespace
        from backend.worker import execute

        captured = {}
        def fetch(**kwargs):
            captured.update(kwargs)
            return pd.DataFrame([row(1)])
        with patch.dict("sys.modules", {"akshare": SimpleNamespace(stock_zh_a_hist_tx=fetch)}):
            results = execute({"task": "history", "symbol": "sz000001", "adjust": "none"})
        self.assertEqual(captured["adjust"], "")
        self.assertEqual(captured["timeout"], 8)
        self.assertEqual(results[0]["volume"], 1000000)


if __name__ == "__main__":
    unittest.main()
