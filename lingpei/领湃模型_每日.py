# -*- coding: utf-8 -*-
"""领湃模型_每日.py：收盘后主流程。
行情更新 -> 全市场扫描 B_V3.3 -> 立信/ST/SW_B 闸门过滤 -> PushPlus(html) 推送 -> 写入推荐记录。
重建版：按原 prompt 口径推断实现。
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

    # 基准交易日：以东财最新 K 线日期为准
    sample = tb.em_hist("600519", 8) or tb.em_hist("000001", 8)
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
        tb.push("领湃模型 %s 闸门未过" % today,
                "SW_B 广度 %.1f%% <= 20日均 %.1f%%，今日不出股。" % ((b or 0) * 100, (ma or 0) * 100),
                template="html")
        return

    lixin = tb.load_lixin_exclude()
    print("[立信排除集] %d 只" % len(lixin))

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
        bars = tb.em_hist(code, 180)
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

    # 推送
    if top:
        rows = "".join(
            "<tr><td>%d</td><td>%s</td><td>%s</td><td>%.1f</td><td>%s</td><td>%d</td><td>%.1f%%</td></tr>"
            % (i + 1, h["name"], h["code"], h["score"], h["near_ma"], h["gap_days"], h["drop_from_peak"] * 100)
            for i, h in enumerate(top))
        msg = ("<h3>领湃模型 B_V3.3 %s 入选 %d 只</h3>"
               "<table border='1' cellspacing='0' cellpadding='4'>"
               "<tr><th>序号</th><th>名称</th><th>代码</th><th>评分</th><th>贴线</th><th>间距</th><th>距前高</th></tr>"
               "%s</table>") % (today, len(top), rows)
        tb.push("领湃模型 %s 入选%d只" % (today, len(top)), msg, template="html")
    else:
        tb.push("领湃模型 %s 无信号" % today, "今日无 B_V3.3 回踩企稳信号。", template="html")

    print("[完成] %s" % today)


if __name__ == "__main__":
    main()
