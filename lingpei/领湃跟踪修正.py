# -*- coding: utf-8 -*-
"""领湃跟踪修正.py：回看到期推荐股 T+10 收益，汇总胜率，给参数修正建议。
重建版：按原 prompt 口径推断实现。
"""
import os
import sys
import json
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import 领湃工具箱 as tb


def _t10_return(bars, entry_date, horizon=10):
    """entry_date 之后第 horizon 个交易日的收盘相对 entry 当日收盘的收益率。"""
    # 找到 entry_date 对应的索引
    idx = None
    for i, b in enumerate(bars):
        if b["d"] >= entry_date:
            idx = i
            break
    if idx is None or idx + horizon >= len(bars):
        return None
    buy = bars[idx]["c"]
    sell = bars[idx + horizon]["c"]
    if buy <= 0:
        return None
    return (sell - buy) / buy


def main():
    today = tb.today_str()
    print("=" * 64)
    print("领湃跟踪修正（重建版）  %s" % today)
    print("=" * 64)

    recs = tb.load_records()
    if not recs:
        print("[无推荐记录]")
        return

    # 截止今天，推荐日距今 >= 10 交易日 的条目视为到期
    results = []  # (code, name, entry_date, ret)
    for entry in recs:
        edate = entry.get("date")
        # 计算 edate 距今交易日数（用任一标的的 K 线近似）
        for stk in entry.get("stocks", []):
            if stk.get("t10_done"):
                results.append((stk["code"], stk["name"], edate, stk.get("t10_ret")))
                continue
            bars = tb.em_hist(stk["code"], 200)
            if not bars:
                continue
            ret = _t10_return(bars, edate, 10)
            if ret is None:
                continue  # 尚未满 10 交易日
            stk["t10_done"] = True
            stk["t10_ret"] = round(ret, 4)
            results.append((stk["code"], stk["name"], edate, ret))

    if not results:
        print("[暂无到期样本]")
        tb.save_records(recs)
        return

    rets = [r[3] for r in results if r[3] is not None]
    wins = sum(1 for x in rets if x > 0)
    win_rate = wins / len(rets) * 100 if rets else 0
    avg = sum(rets) / len(rets) if rets else 0
    median = sorted(rets)[len(rets) // 2] if rets else 0

    print("[到期样本] %d 只 | 胜率 %.1f%% | 平均 %.2f%% | 中位 %.2f%%"
          % (len(rets), win_rate, avg * 100, median * 100))
    for code, name, edate, ret in results:
        print("  %s %s 推荐%s -> T+10 %+.2f%%" % (name, code, edate, (ret or 0) * 100))

    # 参数修正建议（基于胜率/平均收益粗略给出）
    advice = []
    if win_rate < 50:
        advice.append("胜率偏低，建议收紧 dd_hi / 提高试盘量能倍数 TRIAL_VOL_RATIO")
    if avg < 0:
        advice.append("平均收益为负，建议缩小 DROP 上限、要求更贴近长期均线")
    if win_rate >= 60 and avg > 0.03:
        advice.append("模型当前有效，可维持阈值")
    msg = ("<h3>领湃 B_V2 T+10 复盘 %s</h3>"
           "<p>到期样本 %d 只，胜率 %.1f%%，平均 %.2f%%，中位 %.2f%%</p>"
           "<p>修正建议：%s</p>"
           "<p>（注：重建版口径，参数建议仅供粗参考）</p>"
           ) % (today, len(rets), win_rate, avg * 100, median * 100,
                "<br>".join(advice) if advice else "维持现状")
    tb.push("领湃 B_V2 T+10 复盘 %s" % today, msg, template="html")

    tb.save_records(recs)
    print("[完成]")


if __name__ == "__main__":
    main()
