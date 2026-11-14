#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v1.0 旧量比口径  vs  v1.1 新量比带 [0.9, 1.4]  长历史回溯对比
================================================================
用户口径 2026-11-15：把「量比只选择 0.9-1.4」放进模型后，
验证 T+1 / T+3 / T+5 的收益率、准确率、中性化超额是否改善。

v1.1 信号是 v1.0 的严格子集（旧条件 0.5~2.2 → 新条件 0.9~1.4），
因此可直接在 backtest_anchor_signals.csv（19,580 条 v1.0 信号）上做子集筛选，
无需重新 detect，结果完全等价。

运行：python3 high_shadow/compare_v11.py
产出：high_shadow/state/v11_backtest_compare.json
      /workspace/高位上影线_v1.1_量比带_回溯对比.csv
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
OUT_JSON = os.path.join(STATE, "v11_backtest_compare.json")
OUT_CSV = os.path.join(WORKSPACE, "高位上影线_v1.1_量比带_回溯对比.csv")
LO, HI = 0.9, 1.4
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

    band = ((sig.vol_prev >= LO) & (sig.vol_prev <= HI)
            & (sig.vol_ma5 >= LO) & (sig.vol_ma5 <= HI))
    sig["v11"] = band
    print("[数据] v1.1 信号 %d 条（占 %.1f%%），剔除 %d 条"
          % (int(band.sum()), band.mean() * 100, int((~band).sum())))

    groups = {
        "v1.0 旧口径(0.5~2.2)": sig,
        "v1.1 新口径(0.9~1.4)": sig[band],
        "被新口径剔除的部分": sig[~band],
        "v1.0 中的 L2 标杆精筛": sig[sig.L2.astype(bool)],
        "v1.1 中的 L2 标杆精筛": sig[band & sig.L2.astype(bool)],
    }
    result = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "config": {"field": "volume.volume_band", "min": LO, "max": HI,
                   "apply_to": ["vol_ratio_prev", "vol_ratio"]},
        "data": {"signal_range": [sig.date.min(), sig.date.max()],
                 "t5_valid": int(sig.t5.notna().sum()),
                 "v10_signals": int(len(sig)), "v11_signals": int(band.sum())},
        "groups": {}, "dedup": {}, "half": {}, "daily": {},
    }
    for name, sub in groups.items():
        result["groups"][name] = vs.stat_block(sub)

    # 去重（每股只留首个信号）
    dq = sig.sort_values("date").drop_duplicates("code")
    result["dedup"] = {
        "v1.0 旧口径": vs.stat_block(dq),
        "v1.1 新口径": vs.stat_block(dq[dq.v11]),
        "独立股票数": int(len(dq)),
    }
    # 前后半分期
    uniq = sorted(sig.date.unique()); mid = uniq[len(uniq) // 2]
    for name, sub in (("前半", sig[sig.date <= mid]), ("后半", sig[sig.date > mid])):
        result["half"][name] = {"date_split": mid,
                                "v1.0": vs.stat_block(sub), "v1.1": vs.stat_block(sub[sub.v11])}
    # 逐日配对：v1.1 日均 − 同日 v1.0 日均（同口径内部配对，剔除市场 beta）
    g10 = sig.groupby("date")[["t1", "t3", "t5"]].mean()
    g11 = sig[band].groupby("date")[["t1", "t3", "t5"]].mean()
    common = g10.index.intersection(g11.index)
    for tc, tag in (("t1", "T1"), ("t3", "T3"), ("t5", "T5")):
        diff = (g11.loc[common, tc] - g10.loc[common, tc]).dropna().to_numpy(float)
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

    print("\n=== v1.0 vs v1.1 主结果 ===")
    print("%-28s %7s | %9s %9s | %9s %9s | %9s %9s"
          % ("口径", "样本", "T1收益", "T1准确", "T3收益", "T3准确", "T5收益", "T5准确"))
    for name in ("v1.0 旧口径(0.5~2.2)", "v1.1 新口径(0.9~1.4)", "被新口径剔除的部分"):
        b = result["groups"][name]
        c = []
        for tag, _, _, _ in HZ:
            s = b[tag]; c += [s["mean"], s["up_rate"]]
        print("%-28s %7d | %8.3f%% %8.2f%% | %8.3f%% %8.2f%% | %8.3f%% %8.2f%%"
              % (name, b["n"], *c))
    print("\n=== 中性化超额 / 聚类 t ===")
    for name in ("v1.0 旧口径(0.5~2.2)", "v1.1 新口径(0.9~1.4)"):
        b = result["groups"][name]
        print("  %-24s" % name, end="")
        for tag, _, _, _ in HZ:
            s = b[tag]
            print(" | %s 超额%+.3f%% t=%s" % (HZN[tag], s.get("excess_mean", 0), s.get("cluster_t")), end="")
        print()
    print("\n=== 同日配对（v1.1 − v1.0，剔除市场 beta）===")
    for tag, v in result["daily"].items():
        print("  %s: %d 天，差 %+.3f%%，t=%s，正日率 %.2f%%"
              % (HZN[tag], v["days"], v["diff_mean"], v["t_stat"], v["positive_day_rate"]))
    print("\n产出:", OUT_JSON, "\n     ", OUT_CSV)
    return result


if __name__ == "__main__":
    main()
