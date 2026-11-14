#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
高位上影线 · 第二条件候选组合回测
=====================================================================
把「前两日股价情况」的若干候选硬条件，在 v1.3（量比 0.85~1.4）历史
信号上直接回测：信号数 / T+1,T+3,T+5 收益与准确率 / 中性化超额 /
按日聚类 t / 去重 / 前后半分期，并映射到 2026-11-13 的 47 只名单。

只读 state/pre2day_features.csv（由 pre2day_sweep.py 产出），不抓数据。
产出：state/pre2day_conditions.json
      /workspace/高位上影线_第二条件_候选对比.csv
"""
import json
import os
import sys

import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(BASE_DIR, "state")
ROOT = os.path.abspath(os.path.join(BASE_DIR, ".."))
WORKSPACE = os.path.abspath(os.path.join(ROOT, ".."))
sys.path.insert(0, BASE_DIR)
import volratio_sweep as vs

FEAT = os.path.join(STATE, "pre2day_features.csv")
MAPCSV = os.path.join(WORKSPACE, "高位上影线_2026-11-13_前两日位置标注.csv")
OUT_JSON = os.path.join(STATE, "pre2day_conditions.json")
OUT_CSV = os.path.join(WORKSPACE, "高位上影线_第二条件_候选对比.csv")
LO, HI = 0.85, 1.4


def main():
    d = pd.read_csv(FEAT, dtype={"code": str})
    d["code"] = d["code"].str.zfill(6)
    v = d[d.vol_prev.between(LO, HI) & d.vol_ma5.between(LO, HI)].copy()
    print("[数据] 全量 %d 条 → v1.3 %d 条" % (len(d), len(v)))

    conds = [
        ("C0 基准：仅第一条件(v1.3)", lambda x: pd.Series(True, index=x.index)),
        ("A 前两日涨幅 3~6%", lambda x: x.ret2.between(3, 6, inclusive="left")),
        ("B 前一日位置分位 20~60", lambda x: x.posq_prev.between(20, 60, inclusive="left")),
        ("C 组合最优 A∩B", lambda x: x.ret2.between(3, 6, inclusive="left")
         & x.posq_prev.between(20, 60, inclusive="left")),
        ("D 放宽涨幅 0~6% ∩ B", lambda x: x.ret2.between(0, 6, inclusive="left")
         & x.posq_prev.between(20, 60, inclusive="left")),
        ("E 涨幅 3~6% ∩ 位置 20~80", lambda x: x.ret2.between(3, 6, inclusive="left")
         & x.posq_prev.between(20, 80, inclusive="left")),
        ("F 位置 20~60 ∩ 避开阴阳", lambda x: x.posq_prev.between(20, 60, inclusive="left")
         & (x.pattern != "阴阳")),
        ("G 只剔除回避区", lambda x: (x.ret2 < 10) & (x.posq_prev < 80)
         & (x.gap > -2) & (x.pattern != "阴阳")),
        ("H 最优涨幅3~6 ∩ 避开阴阳", lambda x: x.ret2.between(3, 6, inclusive="left")
         & (x.pattern != "阴阳")),
        ("I 位置20~60 ∩ 前一日小跌-1~0", lambda x: x.posq_prev.between(20, 60, inclusive="left")
         & x.d1.between(-1, 0, inclusive="left")),
    ]

    uniq = sorted(v["date"].unique())
    mid = uniq[len(uniq) // 2]
    dq_all = v.sort_values("date").drop_duplicates("code")

    R = {"generated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
         "data": {"v13_signals": len(v), "date_split": str(mid),
                  "dedup_stocks": len(dq_all)},
         "conditions": {}, "definition": {
             "ret2": "前两日累计涨幅% = (C[T-1]/C[T-3]-1)",
             "posq_prev": "T-1 收盘在 60 日高低区间的分位%",
             "pattern": "T-2/T-1 阴阳组合（阴阳=先阴后阳）",
             "gap": "信号日开盘相对 T-1 收盘跳空%",
             "d1": "T-1 单日涨跌%"}}
    rows = []
    for name, fn in conds:
        mask = fn(v)
        sub = v[mask]
        b = vs.stat_block(sub)
        # 去重 / 分期
        dq = vs.stat_block(sub.sort_values("date").drop_duplicates("code"))
        h1 = vs.stat_block(sub[sub.date <= mid])
        h2 = vs.stat_block(sub[sub.date > mid])
        R["conditions"][name] = {"main": b, "dedup": dq,
                                 "前半": h1, "后半": h2,
                                 "date_split": str(mid)}
        for t in ("T1", "T3", "T5"):
            s = b.get(t)
            if not s:
                continue
            rows.append({"条件": name, "周期": t, "信号数": b["n"],
                         "收益均值%": s["mean"], "上涨准确率%": s["up_rate"],
                         "下跌命中率%": s["down_rate"],
                         "超额均值%": s.get("excess_mean"),
                         "跑赢市场率%": s.get("excess_up_rate"),
                         "聚类t": s.get("cluster_t"), "聚类超额%": s.get("cluster_excess"),
                         "去重收益%": dq.get(t, {}).get("mean"),
                         "去重准确率%": dq.get(t, {}).get("up_rate"),
                         "前半收益%": h1.get(t, {}).get("mean"),
                         "后半收益%": h2.get(t, {}).get("mean")})
        print("%-32s n=%-5d | T1 %+.3f/%.2f%% t=%-6s | T3 %+.3f/%.2f%% t=%-6s | T5 %+.3f/%.2f%%"
              % (name, b["n"],
                 b.get("T1", {}).get("mean", 0), b.get("T1", {}).get("up_rate", 0), b.get("T1", {}).get("cluster_t"),
                 b.get("T3", {}).get("mean", 0), b.get("T3", {}).get("up_rate", 0), b.get("T3", {}).get("cluster_t"),
                 b.get("T5", {}).get("mean", 0), b.get("T5", {}).get("up_rate", 0)))

    # 映射 11-13
    M = pd.read_csv(MAPCSV, dtype={"code": str})
    if len(M):
        for name, fn in conds:
            try:
                hit = int(fn(M).sum())
            except Exception:
                hit = None
            R["conditions"][name]["1113_hits"] = hit
        R["mapping_1113"] = {"count": len(M),
                             "hits": {k: v["1113_hits"] for k, v in R["conditions"].items()}}
        print("\n[映射] 11-13 命中数：")
        for k, val in R["mapping_1113"]["hits"].items():
            print("   %-32s %s 只" % (k, val))

    json.dump(R, open(OUT_JSON, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2, default=float)
    pd.DataFrame(rows).to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print("\n产出:", OUT_JSON, "\n     ", OUT_CSV)
    return R


if __name__ == "__main__":
    main()
