#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import csv
import datetime
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import market_store as ms
import common_lib as cm

_spec = importlib.util.spec_from_file_location(
    "stock_v3_datafetch", os.path.join(os.path.dirname(HERE), "stock-v3", "datafetch.py"))
stock_v3_datafetch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stock_v3_datafetch)


def make_bar(code, date, close=10.0):
    return {"code": code, "date": date, "open": close - 0.2,
            "high": close + 0.5, "low": close - 0.5, "close": close,
            "volume": 1000.0, "turnover": 1.2}


class MarketStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ms.MarketStore(os.path.join(self.tmp.name, "market"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_append_preserves_history_and_reads_ranges(self):
        self.store.save_bars([
            make_bar("000001", "2024-01-02", 10),
            make_bar("600000", "2024-01-02", 20),
            make_bar("000001", "2024-01-03", 11),
        ], complete_codes={"000001", "600000"})
        path = self.store.shard_path("2024-01-02")
        with open(path, "rb") as fh:
            before = fh.read()

        result = self.store.save_bars([
            make_bar("000001", "2024-01-02", 999),  # 历史记录不得被覆盖
            make_bar("000001", "2024-01-04", 12),
        ])
        self.assertEqual(result["inserted"], 1)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), before)

        bars = self.store.load_bars("000001", required=True)
        self.assertEqual([b["date"] for b in bars],
                         ["2024-01-02", "2024-01-03", "2024-01-04"])
        self.assertEqual(bars[0]["close"], 10)
        self.assertEqual(len(self.store.load_bars(
            "000001", start="2024-01-03", end="2024-01-04")), 2)
        self.assertEqual(self.store.load_bars("000001", count=1)[0]["date"],
                         "2024-01-04")
        self.assertEqual(self.store.load_bars("000001", count=0), [])
        self.assertTrue(self.store.is_backfilled("000001"))
        self.assertEqual(self.store.coverage()["stocks"], 2)

    def test_materialize_and_required_missing(self):
        self.store.save_bars([make_bar("000001", "2024-01-02")])
        dest = os.path.join(self.tmp.name, "klines")
        result = self.store.materialize(["000001", "000002"], dest)
        self.assertEqual(result["written"], 1)
        self.assertEqual(result["missing"], ["000002"])
        with open(os.path.join(dest, "000001.csv"), encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(rows[0]["date"], "2024-01-02")
        self.assertEqual(self.store.materialize(["000001"], dest)["unchanged"], 1)
        with self.assertRaises(ms.MarketDataError):
            self.store.load_bars("000002", required=True)

    def test_offline_load_never_calls_requests(self):
        self.store.save_bars([make_bar("000001", "2024-01-02")])
        with mock.patch("requests.sessions.Session.request",
                        side_effect=AssertionError("离线读取访问了网络")):
            bars = self.store.load_bars("000001", required=True)
        self.assertEqual(len(bars), 1)


class CommonLibIncrementalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_store = cm.MARKET_STORE
        self.old_dir = cm.KLINE_DIR
        cm.MARKET_STORE = ms.MarketStore(os.path.join(self.tmp.name, "market"))
        cm.KLINE_DIR = os.path.join(self.tmp.name, "klines")
        os.makedirs(cm.KLINE_DIR, exist_ok=True)
        self.available = [make_bar("000001", "2024-01-02", 10),
                          make_bar("000001", "2024-01-03", 11)]
        self.calls = []

        def fake_fetch(code, count=cm.KLINE_CACHE_COUNT, fqt=1, start=None):
            self.calls.append({"code": code, "count": count, "start": start})
            bars = [b for b in self.available
                    if b["code"] == code and (not start or b["date"] >= start)]
            return [{k: v for k, v in b.items() if k != "code"} for b in bars[-count:]]

        self.fetcher = mock.patch.object(cm, "_fetch_one_kline", side_effect=fake_fetch)
        self.fetcher.start()

    def tearDown(self):
        self.fetcher.stop()
        cm.MARKET_STORE = self.old_store
        cm.KLINE_DIR = self.old_dir
        self.tmp.cleanup()

    def test_backfill_then_incremental(self):
        universe = [{"code": "000001", "name": "测试"}]
        cm.fetch_klines_batch(universe, workers=1, budget=30,
                              quote_date="2024-01-03", materialize_codes=["000001"])
        self.assertEqual(self.calls[0]["start"], "2024-01-01")
        self.assertEqual(self.calls[0]["count"], cm.KLINE_CACHE_COUNT)
        self.assertEqual(cm.MARKET_STORE.coverage()["rows"], 2)

        cm.fetch_klines_batch(universe, workers=1, budget=30,
                              quote_date="2024-01-03", materialize_codes=["000001"])
        self.assertEqual(len(self.calls), 1, "已是最新时不应再次抓取")

        self.available.append(make_bar("000001", "2024-01-04", 12))
        cm.fetch_klines_batch(universe, workers=1, budget=30,
                              quote_date="2024-01-04", materialize_codes=["000001"])
        self.assertEqual(self.calls[-1]["start"], "2024-01-03")
        bars = cm.load_kline("000001", offline=True, required=True)
        self.assertEqual([b["close"] for b in bars], [10, 11, 12])
        self.assertEqual(len(cm.load_kline("000001")), 3)

    def test_full_backfill_flushes_every_250_stocks(self):
        self.available = [make_bar(str(i).zfill(6), "2024-01-02")
                          for i in range(1, 252)]
        universe = [{"code": b["code"], "name": "测试"} for b in self.available]
        with mock.patch.object(cm.MARKET_STORE, "save_bars",
                               wraps=cm.MARKET_STORE.save_bars) as save:
            ok, fail = cm.fetch_klines_batch(
                universe, workers=1, budget=30, quote_date="2024-01-02")
        self.assertEqual((ok, fail), (251, 0))
        self.assertEqual(save.call_count, 2)
        self.assertEqual(cm.MARKET_STORE.coverage()["backfilled_stocks"], 251)

    def test_required_offline_missing_does_not_fetch(self):
        self.fetcher.stop()
        with mock.patch.object(cm, "_fetch_one_kline",
                               side_effect=AssertionError("离线读取触发了抓取")) as fetch:
            with self.assertRaises(ms.MarketDataError):
                cm.load_kline("000002", offline=True, required=True)
            fetch.assert_not_called()
        self.fetcher.start()


class FallbackKlineTest(unittest.TestCase):
    def test_tencent_qfq_pages_cover_requested_range(self):
        def rows(start, n):
            return [[(start + datetime.timedelta(days=i)).isoformat(),
                     "10", "11", "12", "9", "1000"] for i in range(n)]

        pages = [rows(datetime.date(2024, 3, 28), 640),
                 rows(datetime.date(2024, 1, 28), 60)]
        calls = []

        class Response:
            status_code = 200

            def __init__(self, data):
                self.data = data

            def json(self):
                return {"code": 0, "data": {"sz000001": {"qfqday": self.data}}}

        class Session:
            def get(self, _url, params=None, timeout=None):
                calls.append(params["param"])
                return Response(pages[len(calls) - 1])

        with mock.patch.object(cm, "_TX_WAF_BLOCKED", False), \
                mock.patch.object(cm, "_session", return_value=Session()):
            bars = cm._fetch_one_kline_tx(
                "000001", count=700, fqt=1, start="2024-01-01")
        self.assertEqual(len(bars), 700)
        self.assertEqual(bars[0]["date"], "2024-01-28")
        self.assertEqual(calls, [
            "sz000001,day,2024-01-01,,640,qfq",
            "sz000001,day,2024-01-01,2024-03-27,60,qfq",
        ])

    def test_sina_caps_datalen_at_supported_limit(self):
        class Response:
            status_code = 200
            text = ('/*<script>location.href="//sina.com";</script>*/\n'
                    'var _=([{"day":"2024-01-02","open":"10","high":"12",'
                    '"low":"9","close":"11","volume":"10000"}]);')

        class Session:
            url = None

            def get(self, url, headers=None, timeout=None):
                self.url = url
                return Response()

        session = Session()
        with mock.patch.object(cm, "_session", return_value=session):
            bars = cm._fetch_one_kline_sina(
                "000001", count=2000, start="2024-01-01")
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["volume"], 100.0)
        self.assertIn("datalen=1023", session.url)


class StockV3OfflineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_store = cm.MARKET_STORE
        cm.MARKET_STORE = ms.MarketStore(os.path.join(self.tmp.name, "market"))
        bars = []
        for i in range(65):
            day = (datetime.date(2024, 1, 1) + datetime.timedelta(days=i)).isoformat()
            bars.append(make_bar("000001", day, 10 + i / 10.0))
        cm.MARKET_STORE.save_bars(bars, complete_codes={"000001"})

    def tearDown(self):
        cm.MARKET_STORE = self.old_store
        self.tmp.cleanup()

    def test_shared_kline_and_bulk_load_never_use_network(self):
        df = stock_v3_datafetch
        with mock.patch.object(df._adapter, "ready", return_value=True), \
                mock.patch.object(df, "_get", side_effect=AssertionError("触网")) as get, \
                mock.patch.object(df, "_kline_em", side_effect=AssertionError("触网")) as em, \
                mock.patch.object(df, "_kline_sina", side_effect=AssertionError("触网")) as sina:
            bars = df.kline("sz000001")
            cached = df.load_market_cache(["sz000001"], count=2)
        self.assertEqual(len(bars), 65)
        self.assertEqual(bars[-1]["last"], 16.4)
        self.assertEqual(bars[-1]["exchange"], 1.2)
        self.assertEqual(len(cached["sz000001"]), 2)
        get.assert_not_called()
        em.assert_not_called()
        sina.assert_not_called()

    def test_shared_universe_applies_own_thresholds_without_fallback(self):
        df = stock_v3_datafetch
        rows = [
            {"code": "000001", "name": "平安银行", "mktcap": 3e9, "turnover": 1.0},
            {"code": "000002", "name": "ST测试", "mktcap": 3e9, "turnover": 1.0},
            {"code": "600000", "name": "低市值", "mktcap": 1e9, "turnover": 1.0},
            {"code": "300001", "name": "低换手", "mktcap": 3e9, "turnover": 0.1},
        ]
        with mock.patch.object(df._adapter, "ready", return_value=True), \
                mock.patch.object(df.cm, "load_universe", return_value=rows), \
                mock.patch.object(df, "_universe_em", side_effect=AssertionError("触网")) as em:
            pool = df.universe()
        self.assertEqual(pool, [{"code": "sz000001", "name": "平安银行"}])
        em.assert_not_called()

    def test_offline_flag_blocks_network_when_adapter_not_ready(self):
        df = stock_v3_datafetch
        rows = [{"code": "000001", "name": "平安银行",
                 "mktcap": 3e9, "turnover": 1.0}]
        with mock.patch.dict(os.environ, {"COMMON_OFFLINE": "1"}), \
                mock.patch.object(df._adapter, "ready", return_value=False), \
                mock.patch.object(df.cm, "load_universe", return_value=rows), \
                mock.patch.object(df, "_get", side_effect=AssertionError("触网")) as get, \
                mock.patch.object(df, "_universe_em", side_effect=AssertionError("触网")) as em, \
                mock.patch.object(df, "_kline_em", side_effect=AssertionError("触网")) as kline_em, \
                mock.patch.object(df, "_kline_sina", side_effect=AssertionError("触网")) as sina:
            bars = df.kline("sz000001")
            pool = df.universe()
        self.assertEqual(len(bars), 65)
        self.assertEqual(pool, [{"code": "sz000001", "name": "平安银行"}])
        get.assert_not_called()
        em.assert_not_called()
        kline_em.assert_not_called()
        sina.assert_not_called()

    def test_kit_fallback_disabled_by_offline_flag(self):
        import kit
        ctx = kit.ModelContext("offline_test")
        with mock.patch.dict(os.environ, {"COMMON_OFFLINE": "1"}), \
                mock.patch.object(kit.cm, "load_kline", return_value=None), \
                mock.patch.object(kit.cm, "_fetch_one_kline",
                                  side_effect=AssertionError("触网")) as fetch:
            bars = ctx.kline("000001", fallback=True)
        self.assertIsNone(bars)
        fetch.assert_not_called()


class RegulatoryExcludeTest(unittest.TestCase):
    """条件⑤：近一年监管立案/涉嫌违法排除集。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "regulatory_exclude.json")
        self.today = datetime.datetime.now(cm.BJT).date()

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, records):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"records": records, "codes": sorted(records)}, fh)

    def _days_ago(self, n):
        return (self.today - datetime.timedelta(days=n)).isoformat()

    def test_loader_drops_records_outside_one_year_window(self):
        self._write({"600001": {"date": self._days_ago(400)},
                     "600002": {"date": self._days_ago(30)}})
        with mock.patch.object(cm, "REGULATORY_JSON", self.path):
            self.assertEqual(cm.load_regulatory_exclude(), {"600002"})

    def test_title_filter_ignores_clarification_notices(self):
        import regulatory as rg
        self.assertTrue(rg.is_regulatory_title("关于收到中国证监会立案告知书的公告"))
        self.assertTrue(rg.is_regulatory_title("关于公司涉嫌信息披露违法违规的公告"))
        self.assertFalse(rg.is_regulatory_title("关于媒体报道的澄清公告"))
        self.assertFalse(rg.is_regulatory_title(
            "关于公司实际控制人因非本公司事项收到立案告知书的公告"))
        self.assertFalse(rg.is_regulatory_title("关于聘任高级管理人员的公告"))

    def test_build_merges_new_hits_and_prunes_expired(self):
        import regulatory as rg
        self._write({"600001": {"date": self._days_ago(400), "title": "立案调查"},
                     "600002": {"date": self._days_ago(20), "title": "立案调查"}})
        hits = {"600003": {"date": self.today.isoformat(), "title": "立案调查"}}
        with mock.patch.object(rg, "REG_JSON", self.path), \
                mock.patch.object(rg, "scan_incremental",
                                  return_value=(hits, {"notices": 5})):
            codes, stats = rg.build(["600001", "600002", "600003"])
        self.assertEqual(codes, {"600002", "600003"})
        self.assertEqual(stats["total_excluded"], 2)
        with open(self.path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertNotIn("600001", data["records"])
        self.assertEqual(data["records"]["600003"]["title"], "立案调查")

    def test_universe_physically_drops_regulatory_codes(self):
        rows = [
            {"code": "600001", "name": "甲公司", "f2": "10", "f3": "1",
             "f5": "1", "f6": "1", "f8": "1", "f10": "1", "f18": "9",
             "f20": "1e9", "f21": "1e9"},
            {"code": "600002", "name": "乙公司", "f2": "10", "f3": "1",
             "f5": "1", "f6": "1", "f8": "1", "f10": "1", "f18": "9",
             "f20": "1e9", "f21": "1e9"},
        ]
        self._write({"600001": {"date": self._days_ago(10)}})
        with mock.patch.object(cm, "_fetch_raw_universe", return_value=rows), \
                mock.patch.object(cm, "load_base_universe", return_value=[]), \
                mock.patch.object(cm, "LIXIN_JSON", "/nonexistent/lixin.json"), \
                mock.patch.object(cm, "REGULATORY_JSON", self.path):
            universe = cm.get_universe(verbose=False)
        self.assertEqual([x["code"] for x in universe], ["600002"])
        self.assertEqual(cm.universe_status().get("regulatory_drop"), 1)


if __name__ == "__main__":
    unittest.main()
