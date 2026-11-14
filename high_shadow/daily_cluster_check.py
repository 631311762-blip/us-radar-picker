#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
按「信号日」聚类的配对检验  daily_cluster_check.py
=================================================
把同一交易日的信号合成一个组合，与该日全市场平均收益配对求超额，
再对逐日超额做 t 检验 —— 可消除同日横截面相关导致的显著性高估。
"""
import gzip
import json
import os
import sys
import time
from collections import defaultdict

import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(BASE_DIR, "state")
sys.path.insert(0, BASE_DIR)
import high_shadow_screener as hs  # noqa: E402

CACHE = os.path.join(STATE, "klines_cache_330.json.gz")
SIG = os.path.join(STATE, "backtest_anchor_signals.csv")
OUT = os.path.join(STATE, "daily_cluster_check.json")


def main():
    with gzip.open(CACHE, "rt", encoding="utf-8") as f:
        cache = json.load(f)
    sig = pd.read_csv(SIG, dtype={"code": str})
    sig["code"] = sig["code"].str.zfill(6)

    # 每日全市场 T+1/T+3 收益（按日聚合）
    mkt = defaultdict(lambda: [0.0, 0.0, 0])  # sum_t1, sum_t3, count
    print("[按日聚类] 计算每日全市场收益 ...")
    t0 = time.time()
    for j, (code, bars) in enumerate(cache.items(), 1):
        if not bars or len(bars) < 65:
            continue
        df = pd.DataFrame(bars)
        c = df["close"].to_numpy(float)
        dates = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d").tolist()
        for i in range(61, len(c) - 3):
            if c[i] <= 0 or not np.isfinite(c[i]):
                continue
            ds = dates[i]
            m = mkt[ds]
            m[0] += (c[i + 1] / c[i] - 1) * 100
            m[1] += (c[i + 3] / c[i] - 1) * 100
            m[2] += 1
        if j % 1500 == 0:
            print("      %d/%d，%.0fs" % (j, len(cache), time.time() - t0))

    # 各口径逐日信号均值
    tiers = {"base": sig, "L2": sig[sig.L2 == True], "L2PLUS": sig[sig.L2PLUS == True]}
    gdaily = {}
    for name, sub in tiers.items():
        g = sub.groupby("date").agg(n=("t1", "size"), t1=("t1", "mean"), t3=("t3", "mean"))
        gdaily[name] = g

    def paired(g, col, mcol_idx):
        rows = []
        for ds, r in g.iterrows():
            if ds not in mkt or mkt[ds][2] == 0:
                continue
            mmean = mkt[ds][mcol_idx] / mkt[ds][2]
            rows.append((ds, r[col] - mmean, r["n"]))
        arr = np.array([x[1] for x in rows], float)
        if len(arr) == 0:
            return {"days": 0}
        se = arr.std(ddof=1) / np.sqrt(len(arr)) if len(arr) > 1 else 0
        return {
            "days": len(arr), "mean_excess": round(float(arr.mean()), 3),
            "median_excess": round(float(np.median(arr)), 3),
            "t_stat": round(float(arr.mean() / se), 2) if se > 0 else None,
            "positive_day_rate": round(float((arr > 0).mean()) * 100, 2),
            "avg_signals_per_day": round(float(np.mean([x[2] for x in rows])), 1),
        }

    result = {"market_days": len(mkt), "description": "按信号日聚类：口径日均收益 − 同日全市场日均收益，对逐日超额做 t 检验",
              "tiers": {}}
    for name, g in gdaily.items():
        result["tiers"][name] = {"T1": paired(g, "t1", 0), "T3": paired(g, "t3", 1),
                                 "signal_days": len(g)}
    json.dump(result, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2, default=float)

    print("\n=== 按日聚类配对超额（口径日均 − 同日市场日均）===")
    print("%-10s %6s %12s %8s %12s" % ("口径/周期", "天数", "平均超额", "t值", "超额>0天数占比"))
    for name in ("base", "L2", "L2PLUS"):
        for t in ("T1", "T3"):
            v = result["tiers"][name][t]
            print("%-10s %6d %11.3f%% %8s %11.2f%%"
                  % (name + "/" + t, v["days"], v["mean_excess"], v["t_stat"], v["positive_day_rate"]))
    print("\n产出:", OUT)


if __name__ == "__main__":
    main()
