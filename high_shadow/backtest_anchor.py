#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
高位上影线 · 标杆口径回溯（T+1 / T+3 收益率与准确率）  backtest_anchor.py
=========================================================================
对比三种口径在长历史区间上的次日 / 三日表现，并与全市场同期基准对照：
    base    现行 high_shadow 基础口径（config.json 硬条件）
    L2      叠加标杆精筛 5 条核心条件（量比前日/上影/下影/位置）
    L2PLUS  再叠加「量能不过热 + 盘中接近前高」（全标杆特征）

准确率双口径同时给出：
    上涨准确率 = T+N 收盘 > 信号日收盘 的占比（做多胜率）
    下跌命中率 = T+N 收盘 < 信号日收盘 的占比（风险名单 win_rule=down 的命中）

数据：共同历史行情库 common/state/market_daily，离线读取最近 330 根日K。
      缺数据时明确报错/跳过，不再访问新浪或其他外网。
运行：python3 high_shadow/backtest_anchor.py
"""
import argparse
import json
import os
import sys
import time

import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(BASE_DIR, ".."))
STATE = os.path.join(BASE_DIR, "state")
UNIVERSE = os.path.join(ROOT, "common", "state", "universe.json")
OUT_JSON = os.path.join(STATE, "backtest_anchor_T1T3.json")
HIST_LEN = 330

sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(ROOT, "common"))
import high_shadow_screener as hs  # noqa: E402
import common_lib as cm  # noqa: E402

# 与 anchor_refine.py 一致的条件
def tier_of(row, close, high):
    vp = row["vol_ratio_prev"]
    vm = row["vol_ratio"]
    up_pct = row["upper_pct"] * 100
    up_rng = row["upper_range"] * 100
    lo_rng = row["lower_range"] * 100
    p60 = row["pos60"] * 100
    high60 = high * p60 / close if close else 0
    l2 = (vp >= 1.05 and up_pct >= 3.5 and up_rng >= 65 and lo_rng <= 12 and p60 >= 88)
    l2p = l2 and (vm <= 1.6 and high60 >= 95)
    return l2, l2p, high60


def fetch_all(refresh=False):
    """严格离线读取统一历史库；refresh 保留仅为兼容旧命令行。"""
    uni = json.load(open(UNIVERSE, encoding="utf-8"))
    codes = [str(x["code"]).zfill(6) for x in uni]
    print("[数据] 离线读取共同历史库：%d 只 × 最近 %d 根日K" % (len(codes), HIST_LEN))
    data = cm.MARKET_STORE.load_many(codes, count=HIST_LEN)
    cache = {code: bars for code, bars in data.items() if bars}
    if not cache:
        raise cm.ms.MarketDataError(
            "共同历史库为空，请先运行 python3 common/market_store.py update")
    print("[数据] 离线载入完成：有效 %d / %d 只" % (len(cache), len(codes)))
    return cache


def stat(t1, t3):
    a1, a3 = np.asarray(t1, float), np.asarray(t3, float)
    if len(a1) == 0:
        return {"n": 0}
    def one(a):
        return {
            "mean": round(float(a.mean()), 3), "median": round(float(np.median(a)), 3),
            "std": round(float(a.std()), 3),
            "up_rate": round(float((a > 0).mean()) * 100, 2),
            "down_rate": round(float((a < 0).mean()) * 100, 2),
            "gt3_rate": round(float((a > 3).mean()) * 100, 2),
            "lt_neg3_rate": round(float((a < -3).mean()) * 100, 2),
        }
    both = float(((a1 > 0) & (a3 > 0)).mean()) * 100
    return {"n": int(len(a1)), "T1": one(a1), "T3": one(a3),
            "both_up_rate": round(both, 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    cache = fetch_all(args.refresh)

    bench1, bench3 = [], []
    rows = []
    daily = {}
    date_min, date_max = None, None
    print("[回溯] 扫描 %d 只 ..." % len(cache))
    t0 = time.time()
    for j, (code, bars) in enumerate(cache.items(), 1):
        if not bars or len(bars) < 65:
            continue
        try:
            d = hs.detect(bars)
        except Exception:
            continue
        closes = d["close"].to_numpy(float)
        highs = d["high"].to_numpy(float)
        sig_arr = d["sig"].to_numpy(bool)
        dates = d["date"]
        n = len(d)
        for i in range(61, n - 3):
            c = closes[i]
            if not np.isfinite(c) or c <= 0:
                continue
            t1 = (closes[i + 1] / c - 1) * 100
            t3 = (closes[i + 3] / c - 1) * 100
            bench1.append(t1)
            bench3.append(t3)
            if not sig_arr[i]:
                continue
            r = d.iloc[i]
            l2, l2p, high60 = tier_of(r, c, highs[i])
            ds = dates.iloc[i].strftime("%Y-%m-%d")
            date_min = ds if date_min is None or ds < date_min else date_min
            date_max = ds if date_max is None or ds > date_max else date_max
            daily[ds] = daily.get(ds, 0) + 1
            rows.append({"code": code, "date": ds, "t1": t1, "t3": t3,
                         "base": True, "L2": l2, "L2PLUS": l2p,
                         "upper_pct": float(r["upper_pct"]) * 100,
                         "vol_prev": float(r["vol_ratio_prev"]),
                         "vol_ma5": float(r["vol_ratio"]),
                         "pos60": float(r["pos60"]) * 100, "high60": high60})
        if j % 1000 == 0:
            print("      %d/%d，%.0fs" % (j, len(cache), time.time() - t0))

    df = pd.DataFrame(rows)
    groups = {
        "全市场基准(同期所有股票×交易日)": (bench1, bench3),
        "base 现行基础口径": (df.t1.tolist(), df.t3.tolist()),
        "L2 标杆精筛": (df[df.L2].t1.tolist(), df[df.L2].t3.tolist()),
        "L2PLUS 全标杆特征": (df[df.L2PLUS].t1.tolist(), df[df.L2PLUS].t3.tolist()),
    }
    result = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
              "data": {"stocks": len(cache), "hist_len": HIST_LEN,
                       "signal_range": [date_min, date_max],
                       "signal_days": len(daily), "benchmark_obs": len(bench1)},
              "definition": {
                  "T+1/T+3": "信号日收盘 → 第1/第3个交易日收盘的涨跌幅",
                  "上涨准确率(up_rate)": "T+N 上涨的信号占比（做多胜率）",
                  "下跌命中率(down_rate)": "T+N 下跌的信号占比（风险名单 win_rule=down 的命中率）",
                  "去重检验": "同一只股票只保留区间内首个信号",
              },
              "groups": {k: stat(a, b) for k, (a, b) in groups.items()}}
    # 去重稳健性
    dq = df.sort_values("date").drop_duplicates("code")
    result["dedup"] = {
        "base": stat(dq.t1.tolist(), dq.t3.tolist()),
        "L2": stat(dq[dq.L2].t1.tolist(), dq[dq.L2].t3.tolist()),
        "L2PLUS": stat(dq[dq.L2PLUS].t1.tolist(), dq[dq.L2PLUS].t3.tolist()),
        "独立股票数": len(dq),
    }
    # 每日信号数
    result["daily_counts"] = dict(sorted(daily.items()))
    # 超额
    bm = result["groups"]["全市场基准(同期所有股票×交易日)"]
    for k in ("base 现行基础口径", "L2 标杆精筛", "L2PLUS 全标杆特征"):
        g = result["groups"][k]
        if g.get("n"):
            g["excess_T1"] = round(g["T1"]["mean"] - bm["T1"]["mean"], 3)
            g["excess_T3"] = round(g["T3"]["mean"] - bm["T3"]["mean"], 3)
            g["excess_up_T1"] = round(g["T1"]["up_rate"] - bm["T1"]["up_rate"], 2)
            g["excess_up_T3"] = round(g["T3"]["up_rate"] - bm["T3"]["up_rate"], 2)

    json.dump(result, open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=2, default=float)
    # 信号明细（供复核）
    df.to_csv(os.path.join(STATE, "backtest_anchor_signals.csv"), index=False, encoding="utf-8-sig")

    print("\n=== 回溯结果（信号区间 %s ~ %s，%d 个信号日）===" % (date_min, date_max, len(daily)))
    print("%-32s %6s %9s %9s %9s %9s" % ("口径", "样本", "T+1均值", "T+1上涨率", "T+3均值", "T+3上涨率"))
    for k, g in result["groups"].items():
        if not g.get("n"):
            print("%-32s %6d  (无样本)" % (k, 0)); continue
        print("%-32s %6d %8.2f%% %8.2f%% %8.2f%% %8.2f%%"
              % (k, g["n"], g["T1"]["mean"], g["T1"]["up_rate"], g["T3"]["mean"], g["T3"]["up_rate"]))
    print("\n产出:", OUT_JSON)
    return result


if __name__ == "__main__":
    main()
