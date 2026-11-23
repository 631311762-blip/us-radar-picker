# -*- coding: utf-8 -*-
"""report_risk 单元测试：预约披露日历构建 + 推送标注（全程离线）。"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import report_risk as rr  # noqa: E402


def _payload(entries, as_of="2026-11-23"):
    return {"generated_at": as_of, "as_of": as_of, "entries": entries}


class ReportRiskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "report_calendar.json")
        self._old = rr.CAL_JSON
        rr.CAL_JSON = self.path

    def tearDown(self):
        rr.CAL_JSON = self._old
        self.tmp.cleanup()

    def _write(self, entries):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(_payload(entries), fh, ensure_ascii=False)

    def test_markdown_decorate_by_code(self):
        self._write({"000520": {"name": "凤凰航运", "report": "2026年 三季报",
                                "date": "2026-12-01"}})
        title, body = rr.decorate(
            "善水模型 入选1只", "| 000520 | 凤凰航运 |", template="markdown",
            today=__import__("datetime").date(2026, 11, 23))
        self.assertTrue(title.startswith(rr.TITLE_PREFIX))
        self.assertIn("临近定期报告/业绩预告", body)
        self.assertIn("2026年 三季报", body)
        self.assertIn("8 天后", body)

    def test_html_decorate(self):
        self._write({"600900": {"name": "长江电力", "report": "2025年 年报",
                                "date": "2026-12-05"}})
        _, body = rr.decorate("<b>长江电力</b>", "<p>600900</p>", template="html",
                              today=__import__("datetime").date(2026, 11, 23))
        self.assertIn("<ul>", body)
        self.assertIn("<li>", body)

    def test_no_hit_unchanged(self):
        self._write({"600900": {"name": "长江电力", "report": "2025年 年报",
                                "date": "2026-12-05"}})
        t, b = rr.decorate("模型 入选1只", "000001 平安银行", "markdown",
                           today=__import__("datetime").date(2026, 11, 23))
        self.assertEqual(t, "模型 入选1只")
        self.assertEqual(b, "000001 平安银行")

    def test_name_fallback_when_no_code(self):
        self._write({"600900": {"name": "长江电力", "report": "2025年 年报",
                                "date": "2026-12-05"}})
        t, b = rr.decorate("模型 入选", "长江电力 今日放量", "markdown",
                           today=__import__("datetime").date(2026, 11, 23))
        self.assertTrue(t.startswith(rr.TITLE_PREFIX))
        self.assertIn("600900", b)

    def test_expired_entry_ignored(self):
        self._write({"600900": {"name": "长江电力", "report": "2025年 年报",
                                "date": "2026-11-20"}})
        t, b = rr.decorate("x", "600900", "markdown",
                           today=__import__("datetime").date(2026, 11, 23))
        self.assertEqual((t, b), ("x", "600900"))

    def test_beyond_warn_window_not_flagged(self):
        self._write({"600900": {"name": "长江电力", "report": "2025年 年报",
                                "date": "2027-04-30"}})
        t, b = rr.decorate("x", "600900", "markdown",
                           today=__import__("datetime").date(2026, 11, 23))
        self.assertEqual((t, b), ("x", "600900"))

    def test_refresh_builds_and_filters(self):
        rows = [
            {"SECURITY_CODE": "600900", "SECURITY_NAME_ABBR": "长江电力",
             "REPORT_TYPE_NAME": "2025年 年报", "APPOINT_PUBLISH_DATE": "2026-12-01 00:00:00",
             "ACTUAL_PUBLISH_DATE": None},
            {"SECURITY_CODE": "000001", "SECURITY_NAME_ABBR": "平安银行",
             "REPORT_TYPE_NAME": "2025年 年报", "APPOINT_PUBLISH_DATE": "2026-12-02 00:00:00",
             "ACTUAL_PUBLISH_DATE": "2026-11-20 00:00:00"},   # 已实际披露 → 剔除
            {"SECURITY_CODE": "600519", "SECURITY_NAME_ABBR": "贵州茅台",
             "REPORT_TYPE_NAME": "2025年 年报", "APPOINT_PUBLISH_DATE": "2026-12-01 00:00:00",
             "ACTUAL_PUBLISH_DATE": None},
            {"SECURITY_CODE": "600519", "SECURITY_NAME_ABBR": "贵州茅台",
             "REPORT_TYPE_NAME": "2026年 一季报", "APPOINT_PUBLISH_DATE": "2027-02-01 00:00:00",
             "ACTUAL_PUBLISH_DATE": None},                    # 更远的一期 → 保留更近
        ]
        today = __import__("datetime").date(2026, 11, 23)
        with mock.patch.object(rr, "_today", return_value=today), \
                mock.patch.object(rr, "fetch_appointments", return_value=rows):
            payload = rr.refresh(verbose=False)
        self.assertEqual(sorted(payload["entries"]), ["600519", "600900"])
        self.assertEqual(payload["entries"]["600519"]["date"], "2026-12-01")
        self.assertEqual(payload["stats"]["fetched"], 4)
        self.assertFalse(payload["stats"]["scan_empty"])
        self.assertTrue(os.path.exists(self.path))

    def test_decorate_never_touches_network(self):
        self._write({"600900": {"name": "长江电力", "report": "2025年 年报",
                                "date": "2026-12-05"}})
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network!")):
            t, b = rr.decorate("x", "600900", "markdown",
                               today=__import__("datetime").date(2026, 11, 23))
        self.assertIn("长江电力", b)

    def test_missing_file_is_safe(self):
        t, b = rr.decorate("标题", "600900", "markdown")
        self.assertEqual((t, b), ("标题", "600900"))
        self.assertEqual(rr.load(), {})


if __name__ == "__main__":
    unittest.main()
