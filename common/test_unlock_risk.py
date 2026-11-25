# -*- coding: utf-8 -*-
"""unlock_risk 单元测试：解禁聚合 / 大解禁门槛 / 读取窗口失效（全程离线）。"""
import datetime
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import unlock_risk as ur  # noqa: E402

DAY = datetime.date(2026, 11, 25)


def _row(code, date, ratio, cap=1000.0, name="测试"):
    return {
        "SECURITY_CODE": code,
        "SECURITY_NAME_ABBR": name,
        "FREE_DATE": date + " 00:00:00",
        "CURRENT_FREE_SHARES": 100,
        "LIFT_MARKET_CAP": cap,
        "TOTALSHARES_RATIO": ratio,
        "FREE_SHARES_TYPE": "定向增发机构配售股份",
    }


class UnlockRiskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "unlock_exclude.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_aggregate_filters_and_max(self):
        rows = [
            _row("600001", "2026-11-20", 0.20),
            _row("600001", "2026-12-10", 0.05),
            _row("000002", "2026-12-01", 0.08),
            _row("123456", "2026-12-01", 0.90),       # 非 A 股前缀，剔除
            {"SECURITY_CODE": "600009", "FREE_DATE": "坏日期",
             "TOTALSHARES_RATIO": 0.99},              # 无效日期，跳过
        ]
        agg = ur.aggregate(rows)
        self.assertEqual(set(agg), {"600001", "000002"})
        self.assertAlmostEqual(agg["600001"]["max_ratio"], 0.20)
        self.assertEqual(len(agg["600001"]["batches"]), 2)

    def test_build_excludes_big_only(self):
        rows = [
            _row("600001", "2026-11-20", 0.20),   # 大解禁 → 剔除
            _row("000002", "2026-12-01", 0.08),   # 不足 10% → 保留
        ]
        with mock.patch.object(ur, "_today", return_value=DAY), \
                mock.patch.object(ur, "fetch_unlocks", return_value=rows):
            codes, stats = ur.build(path=self.path, verbose=False)
        self.assertEqual(codes, {"600001"})
        self.assertEqual(stats["big_unlock"], 1)
        self.assertFalse(stats["scan_empty"])
        # 落盘后可读回，且窗口校验通过
        got = ur.load_unlock_exclude(today=DAY, path=self.path)
        self.assertEqual(got, {"600001"})

    def test_build_scan_empty_flag(self):
        with mock.patch.object(ur, "_today", return_value=DAY), \
                mock.patch.object(ur, "fetch_unlocks", return_value=[]):
            codes, stats = ur.build(path=self.path, verbose=False)
        self.assertEqual(codes, set())
        self.assertTrue(stats["scan_empty"])

    def test_ratio_threshold(self):
        rows = [_row("600001", "2026-11-20", 0.20)]
        with mock.patch.object(ur, "_today", return_value=DAY), \
                mock.patch.object(ur, "fetch_unlocks", return_value=rows):
            codes, _ = ur.build(path=self.path, ratio=0.30, verbose=False)
        self.assertEqual(codes, set())

    def test_load_window_prunes_expired(self):
        payload = {
            "window_days": 30,
            "big_ratio": 0.10,
            "codes": ["600001", "600002"],
            "records": {
                "600001": {"name": "A", "batches": [
                    {"date": "2026-01-01", "ratio": 0.50}]},   # 早已过期
                "600002": {"name": "B", "batches": [
                    {"date": "2026-12-01", "ratio": 0.30}]},   # 窗口内
            },
        }
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        got = ur.load_unlock_exclude(today=DAY, path=self.path)
        self.assertEqual(got, {"600002"})

    def test_load_missing_file(self):
        self.assertEqual(ur.load_unlock_exclude(today=DAY, path=self.path), set())


if __name__ == "__main__":
    unittest.main()
