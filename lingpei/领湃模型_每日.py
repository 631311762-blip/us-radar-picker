# -*- coding: utf-8 -*-
"""领湃模型_每日.py：收盘后主流程。
行情更新 -> 全市场扫描 B_V3.4.1 -> 立信/ST/SW_B 闸门过滤 -> PushPlus(html) 推送 -> 写入推荐记录。
2026-11-23：推送清单新增「业绩预告」风险备注列（负面预告红色警示）。
"""
import os
import sys
import json
import datetime
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import 领湃工具箱 as tb
from 埋伏点_回踩均线模型 import is_st
from 回踩长期均线企稳模型 import detect_coil_v2

REC_FILE = "领湃_推荐记录.json"

# 推送末尾附原型说明（供用户回忆模型设计源头）
ORIGIN_NOTE = "📌 模型原型：以上选股基于领湃科技，提示日为回踩长期均线企稳当日，基本特征：前期试探→回落→踩长期均线企稳(B_V3.4.1)，立信/ST/SW_B三重过滤得出。"
STRATEGY_NOTE = "📈 最优策略：提示日次日开盘买入，持有至次日(D+2)最高价卖出（2026年回溯胜率67%、平均收益+1.5%，按盘中最高价理想成交）；跌破买入价-5%止损（止损用于控制回撤，回溯显示会略降理想收益）。"


def _settle_digest():
    """从本地台账读取 3/5/10 结算进度，合并进当日唯一一条推送。

    2026-11-16：用户要求每天只收一份领湃推送。原「跟踪修正脚本单独推结算简报」
    已默认静默，改由此处把结算摘要附在选股清单里，信息不丢失。
    """
    try:
        track = tb.load_track()
        samples = track.get("samples", [])
        if not samples:
            return ""
        parts = []
        for h in (3, 5, 10):
            key = "t%d" % h
            rets = [s[key]["ret"] for s in samples
                    if (s.get(key) or {}).get("done") and s[key].get("ret") is not None]
            if rets:
                wr = sum(1 for x in rets if x > 0) / len(rets)
                parts.append("T+%d %d只 / 胜率%.0f%%" % (h, len(rets), wr * 100))
            else:
                parts.append("T+%d 待结算" % h)
        return ("<p style='color:#555'><b>低位样本跟踪</b>（台账 %d 只 · 条件冻结）：%s</p>"
                % (len(samples), " · ".join(parts)))
    except Exception:
        return ""


def main():
    # 共同前置层接入（强制）：patch 工具箱的取数函数到共享样本池/K线缓存。
    _ad = None
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "..", "common"))
        import adapter as _ad
        _ad.install("lingpei", vars(tb))
    except Exception:
        _ad = None
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--no-gate", action="store_true", help="跳过 SW_B 广度闸门（验证/特殊用）")
    args = ap.parse_args()

    today = tb.today_str()
    print("=" * 64)
    print("领湃模型 B_V3.3（重建版）  %s" % today)
    print("=" * 64)

    # 基准交易日：三源兜底（东财→新浪→腾讯）。2026-11-24 修复：原仅用 em_hist，
    # 11-23 东财在 runner 整体 Connection aborted 导致基准=None、当天零推送。
    # 先用东财探测基准，若失败则置熔断位——后续全市场扫描直接走新浪，避免每只重试东财 3 host。
    sample = tb.em_hist("600519", 8)
    if sample:
        base_date = sample[-1]["d"]
    else:
        tb.mark_em_broken()
        sample = tb.sina_hist("600519", 8) or tb.tx_hist("600519", 8)
        base_date = sample[-1]["d"] if sample else None
    print("[基准] 最新行情日 = %s" % base_date)
    if not base_date or base_date < today:
        print("[退出] 行情日期异常（%s < %s），疑似盘前或数据未更新" % (base_date, today))
        return

    # SW_B 广度闸门
    ok, b, ma = tb.sw_b_breadth_ok()
    gate = "跳过(--no-gate)" if args.no_gate else ("通过" if ok else "未过")
    print("[SW_B] 今日广度 %.3f / 20日均 %.3f / 闸门=%s" % (b if b is not None else 0, ma if ma is not None else 0, gate))
    if not args.no_gate and not ok:
        tb.push_once("领湃模型 %s 闸门未过" % today,
                     "SW_B 广度 %.1f%% <= 20日均 %.1f%%，今日不出股。" % ((b or 0) * 100, (ma or 0) * 100),
                     template="html", kind="notice")
        return

    lixin = tb.load_lixin_exclude()
    print("[立信排除集] %d 只" % len(lixin))

    # 财报/预告事件风险备注（2026-11-23 用户要求）：刷新当日缓存，失败时用旧缓存不影响主流程
    try:
        fc_n, fc_new = tb.refresh_forecast(year=int(today[:4]))
        fc_by_code = tb.load_forecast()
    except Exception as _e:
        fc_by_code = {}
        print("[业绩预告] 取数失败：%s" % _e)
    try:
        rp_n, rp_new = tb.refresh_reports(year=int(today[:4]))
        rp_by_code = tb.load_reports()
        print("[财报/预告] 财报缓存 %d 条、预告缓存 %d 条" % (rp_n, fc_n))
    except Exception as _e:
        rp_by_code = {}
        print("[财报] 取数失败：%s" % _e)

    universe = tb.get_universe()
    print("[股票池] 共 %d 只" % len(universe))

    hits = []
    st_skip = lixin_skip = 0
    for u in universe:
        code = u["code"]
        if code in lixin:
            lixin_skip += 1
            continue
        if is_st(u):
            st_skip += 1
            continue
        bars = tb.hist_with_fallback(code, 180)
        if not bars or len(bars) < 65:
            continue
        sig = detect_coil_v2(bars)
        if sig:
            sig.update({"code": code, "name": u["name"], "entry_close": bars[-1]["c"]})
            hits.append(sig)

    hits.sort(key=lambda x: x["score"], reverse=True)
    top = hits[: args.top]
    print("[扫描] 命中 %d 只，立信剔除 %d，ST剔除 %d，取前 %d" % (len(hits), lixin_skip, st_skip, len(top)))
    # 共同前置层：写统一推荐台账（出口 gate + 回溯/参数修正）
    if _ad:
        try:
            _ad.record("lingpei", top)
        except Exception:
            pass

    # 写入推荐记录
    recs = tb.load_records()
    recs.append({
        "date": today,
        "stocks": [{"code": h["code"], "name": h["name"], "score": h["score"],
                    "gap_days": h["gap_days"], "drop": h["drop_from_peak"],
                    "near_ma": h["near_ma"], "trial_date": h["trial_date"]} for h in top],
    })
    tb.save_records(recs)

    # 写入 V3.3 3/5/10 日跟踪台账（全量命中，按 code+日期去重；后续由 领湃跟踪修正.py 结算）
    added, total = tb.track_append(base_date, hits)
    print("[跟踪台账] 新增 %d 只，累计 %d 只待结算" % (added, total))

    # 推送（2026-11-23：新增「财报/预告」事件风险备注列，财报披露优先、负面红色警示）
    if top:
        _risks = {}
        for h in top:
            try:
                _risks[h["code"]] = tb.event_risk(h["code"], today, fc_by_code, rp_by_code, ahead=3)
            except Exception:
                _risks[h["code"]] = {"level": "none", "text": ""}

        def _fc_cell(h):
            fk = _risks.get(h["code"], {"level": "none", "text": ""})
            if fk["level"] == "bad":
                return '<td style="background:#fdecea;color:#c0392b;font-weight:700">%s</td>' % fk["text"]
            if fk["level"] == "good":
                return '<td style="background:#eaf7ee;color:#1e8449">%s</td>' % fk["text"]
            if fk["level"] == "flat":
                return '<td style="background:#fff8e1;color:#8a6d1f">%s</td>' % fk["text"]
            return "<td>--</td>"

        rows = "".join(
            "<tr><td>%d</td><td>%s</td><td>%s</td><td>%.1f</td><td>%s</td><td>%d</td><td>%.1f%%</td>%s</tr>"
            % (i + 1, h["name"], h["code"], h["score"], h["near_ma"], h["gap_days"],
               h["drop_from_peak"] * 100, _fc_cell(h))
            for i, h in enumerate(top))
        n_bad = sum(1 for v in _risks.values() if v["level"] == "bad")
        n_evt = sum(1 for v in _risks.values() if v["level"] != "none")
        warn = ""
        if n_bad:
            warn += '<p style="color:#c0392b;font-weight:700">⚠ 其中 %d 只近日财报/预告利空，请谨慎对待</p>' % n_bad
        if n_evt > n_bad:
            warn += '<p style="color:#8a6d1f">另 %d 只近日有财报/预告披露（含正面/中性），注意波动</p>' % (n_evt - n_bad)
        msg = ("<h3>领湃模型 B_V3.4.1 %s 入选 %d 只</h3>"
               "<table border='1' cellspacing='0' cellpadding='4'>"
               "<tr><th>序号</th><th>名称</th><th>代码</th><th>评分</th><th>贴线</th><th>间距</th><th>距前高</th><th>财报/预告</th></tr>"
               "%s</table>%s%s<p style='color:#888;font-size:12px;margin-top:8px'>%s</p>") % (today, len(top), rows, warn, _settle_digest(), ORIGIN_NOTE + STRATEGY_NOTE)
        tb.push_once("领湃模型 %s 入选%d只" % (today, len(top)), msg,
                     template="html", kind="signal")
    else:
        tb.push_once("领湃模型 %s 无信号" % today,
                     "<p>今日无 B_V3.4.1 回踩企稳信号。</p>" + _settle_digest() + "<p style='color:#888;font-size:12px'>%s</p>" % (ORIGIN_NOTE + STRATEGY_NOTE),
                     template="html", kind="notice")

    print("[完成] %s" % today)


if __name__ == "__main__":
    main()
