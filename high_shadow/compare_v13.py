#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v1.2 量比带 [0.8, 1.4]  vs  v1.3 量比带 [0.85, 1.4]  长历史回溯对比
======================================================================
用户口径 2026-11-15：把下限从 0.8 收到 0.85（上限 1.4 不变）。

v1.3 是 v1.2 的严格子集，两者都是 v1.0 信号的子集，
可直接在 backtest_anchor_signals.csv（19,580 条 v1.0 信号）上做子集筛选。

重点考察「本次被剔除的 [0.8, 0.85) 带」本身的表现，以及
「v1.3 − v1.2」同日配对净效果（剔除市场 beta）。

运行：python3 high_shadow/compare_v13.py
产出：high_shadow/state/v13_backtest_compare.json
      /workspace/高位上影线_v1.3_量比带_回溯对比.csv
"""
import gzip
import json
import os
import sys
import time

import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(BASE_DIR, "state")
ROOT = os.path.abspath(os.path.join(BASE_DIR, ".."))
WORKSPACE = os.path.abspath(os.path.join(ROOT, ".."))
sys.path.insert(0, BASE_DIR)
import volratio_sweep as vs  # noqa: E402

CACHE = os.path.join(STATE, "klines_cache_330.json.gz")
SIG = os.path.join(STATE, "backtest_anchor_signals.csv")
OUT_JSON = os.path.join(STATE, "v13_backtest_compare.json")
OUT_CSV = os.path.join(WORKSPACE, "高位上影线_v1.3_量比带_回溯对比.csv")
LO11, LO12, LO13, HI = 0.9, 0.8, 0.85, 1.4
HZ = vs.HORIZONS
HZN = {"T1": "T+1", "T3": "T+3", "T5": "T+5"}


def main():
    with gzip.open(CACHE, "rt", encoding="utf-8") as f:
        cache = json.load(f)
    sig = pd.read_csv(SIG, dtype={"code": str})
    sig["code"] = sig["code"].str.zfill(6)
    sig = sig.reset_index(drop=True)
    print("[数据] 缓存 %d 只，v1.0 信号 %d 条" % (len(cache), len(sig)))

    mkt = vs.build_market(cache)
    sig["t5"] = vs.add_t5(sig, cache)
    sig = sig.merge(mkt, left_on="date", right_index=True, how="left").reset_index(drop=True)
    for tag, tc, ec, mc in HZ:
        sig[ec] = sig[tc] - sig[mc]

    b11 = sig.vol_prev.between(LO11, HI) & sig.vol_ma5.between(LO11, HI)
    b12 = sig.vol_prev.between(LO12, HI) & sig.vol_ma5.between(LO12, HI)
    b13 = sig.vol_prev.between(LO13, HI) & sig.vol_ma5.between(LO13, HI)
    cut = b12 & ~b13  # 本次下限收紧净剔除
    sig["v12"], sig["v13"], sig["cut"] = b12, b13, cut
    print("[数据] v1.2 %d 条 → v1.3 %d 条；本次剔除 [0.8,0.85) %d 条（占 %.1f%%）"
          % (int(b12.sum()), int(b13.sum()), int(cut.sum()), cut.mean() * 100))

    L2 = sig.L2.astype(bool)
    groups = {
        "v1.0 旧口径(0.5~2.2)": sig,
        "v1.1 口径(0.9~1.4)": sig[b11],
        "v1.2 口径(0.8~1.4)": sig[b12],
        "v1.3 口径(0.85~1.4)": sig[b13],
        "本次剔除(0.8~0.85带)": sig[cut],
        "被v1.3剔除的部分": sig[~b13],
        "v1.2 中的 L2 标杆精筛": sig[b12 & L2],
        "v1.3 中的 L2 标杆精筛": sig[b13 & L2],
    }
    result = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "config": {"field": "volume.volume_band.min", "from": LO12, "to": LO13,
                   "max": HI, "apply_to": ["vol_ratio_prev", "vol_ratio"]},
        "data": {"signal_range": [sig.date.min(), sig.date.max()],
                 "t5_valid": int(sig.t5.notna().sum()),
                 "v10_signals": int(len(sig)), "v11_signals": int(b11.sum()),
                 "v12_signals": int(b12.sum()), "v13_signals": int(b13.sum()),
                 "cut_band_signals": int(cut.sum())},
        "groups": {}, "dedup": {}, "half": {}, "daily": {},
    }
    for name, sub in groups.items():
        result["groups"][name] = vs.stat_block(sub)

    # 去重（每股只留首个信号）
    dq = sig.sort_values("date").drop_duplicates("code")
    result["dedup"] = {
        "v1.2 口径(0.8~1.4)": vs.stat_block(dq[dq.v12]),
        "v1.3 口径(0.85~1.4)": vs.stat_block(dq[dq.v13]),
        "独立股票数": int(len(dq)),
    }
    # 前后半分期
    uniq = sorted(sig.date.unique())
    mid = uniq[len(uniq) // 2]
    for name, sub in (("前半", sig[sig.date <= mid]), ("后半", sig[sig.date > mid])):
        result["half"][name] = {"date_split": mid,
                                "v1.2": vs.stat_block(sub[sub.v12]),
                                "v1.3": vs.stat_block(sub[sub.v13]),
                                "剔除带": vs.stat_block(sub[sub.cut])}
    # 逐日配对：v1.3 日均 − 同日 v1.2 日均（同口径内部配对，剔除市场 beta）
    g12 = sig[b12].groupby("date")[["t1", "t3", "t5"]].mean()
    g13 = sig[b13].groupby("date")[["t1", "t3", "t5"]].mean()
    common = g12.index.intersection(g13.index)
    for tc, tag in (("t1", "T1"), ("t3", "T3"), ("t5", "T5")):
        diff = (g13.loc[common, tc] - g12.loc[common, tc]).dropna().to_numpy(float)
        if len(diff) > 1:
            se = diff.std(ddof=1) / np.sqrt(len(diff))
            result["daily"][tag] = {
                "days": int(len(diff)), "diff_mean": round(float(diff.mean()), 3),
                "t_stat": round(float(diff.mean() / se), 2) if se > 0 else None,
                "positive_day_rate": round(float((diff > 0).mean()) * 100, 2),
            }

    json.dump(result, open(OUT_JSON, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2, default=float)

    # 对比明细 CSV
    rows = []
    for name, b in result["groups"].items():
        for tag, _, _, _ in HZ:
            s = b.get(tag)
            if not s:
                continue
            rows.append({"口径": name, "周期": HZN[tag], "样本": s["n"],
                         "收益均值%": s["mean"], "收益中位%": s["median"],
                         "上涨准确率%": s["up_rate"], "下跌命中率%": s["down_rate"],
                         "超额均值%": s.get("excess_mean"), "跑赢市场率%": s.get("excess_up_rate"),
                         "聚类t": s.get("cluster_t"), "聚类超额%": s.get("cluster_excess")})
    pd.DataFrame(rows).to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

    print("\n=== v1.1 / v1.2 / v1.3 / 剔除带 主结果 ===")
    print("%-26s %7s | %9s %9s | %9s %9s | %9s %9s"
          % ("口径", "样本", "T1收益", "T1准确", "T3收益", "T3准确", "T5收益", "T5准确"))
    for name in ("v1.1 口径(0.9~1.4)", "v1.2 口径(0.8~1.4)", "v1.3 口径(0.85~1.4)",
                 "本次剔除(0.8~0.85带)"):
        b = result["groups"][name]
        c = []
        for tag, _, _, _ in HZ:
            s = b[tag]
            c += [s["mean"], s["up_rate"]]
        print("%-26s %7d | %8.3f%% %8.2f%% | %8.3f%% %8.2f%% | %8.3f%% %8.2f%%"
              % (name, b["n"], *c))
    print("\n=== 中性化超额 / 聚类 t ===")
    for name in ("v1.2 口径(0.8~1.4)", "v1.3 口径(0.85~1.4)", "本次剔除(0.8~0.85带)"):
        b = result["groups"][name]
        print("  %-24s" % name, end="")
        for tag, _, _, _ in HZ:
            s = b[tag]
            print(" | %s 超额%+.3f%% t=%s" % (HZN[tag], s.get("excess_mean", 0), s.get("cluster_t")), end="")
        print()
    print("\n=== 同日配对（v1.3 − v1.2，剔除市场 beta）===")
    for tag, v in result["daily"].items():
        print("  %s: %d 天，差 %+.3f%%，t=%s，正日率 %.2f%%"
              % (HZN[tag], v["days"], v["diff_mean"], v["t_stat"], v["positive_day_rate"]))
    print("\n产出:", OUT_JSON, "\n     ", OUT_CSV)
    return result


if __name__ == "__main__":
    main()
