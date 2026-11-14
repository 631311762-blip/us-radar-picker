#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
高位上影线 · 标杆校准与精筛  anchor_refine.py
=============================================
用三只「印象里符合过」的标杆样本反推条件，再反筛当日候选：
    光线传媒 300251 · 2026-11-09
    华策影视 300133 · 2026-11-09
    通合科技 300491 · 2025-08-21

产出三档：
    L1 严格同型  —— 完全复刻三只标杆的指标下界
    L2 精筛      —— 保留「成交量 + K线」核心特征，按当日市场结构适度放宽
    L3 观察层    —— 标杆画像相似度 ≥70 分

数据源：state/klines_cache.json.gz（主运行抓取的新浪日K）+ 2026-11-13_样本.json
运行：python3 high_shadow/anchor_refine.py
"""
import csv
import gzip
import json
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(BASE_DIR, ".."))
OUT = "/workspace"
DATE = "2026-11-13"
STATE = os.path.join(BASE_DIR, "state")

# 三只标杆票实测指标（2026-11-14 由新浪日K 400根计算，含前60日窗口）
ANCHORS = [
    {"code": "300251", "name": "光线传媒", "date": "2026-11-09",
     "o": 12.34, "h": 12.93, "l": 12.13, "c": 12.18, "vol_wan": 8438,
     "pct": -1.62, "upper_pct": 4.84, "upper_body": 3.69, "upper_range": 73.8,
     "lower_range": 6.2, "close_in_range": 6.2, "vol_prev": 1.15, "vol_ma5": 0.88,
     "pos60": 94.5, "high60": 100.3, "pos_quant": 70.9, "ret5": -0.16, "ret20": 4.10,
     "t1": 6.08, "t3": 2.05},
    {"code": "300133", "name": "华策影视", "date": "2026-11-09",
     "o": 9.99, "h": 11.04, "l": 9.69, "c": 9.70, "vol_wan": 17225,
     "pct": -3.67, "upper_pct": 10.82, "upper_body": 3.62, "upper_range": 77.8,
     "lower_range": 0.7, "close_in_range": 0.7, "vol_prev": 1.32, "vol_ma5": 1.08,
     "pos60": 92.9, "high60": 105.7, "pos_quant": 80.5, "ret5": 0.10, "ret20": 6.83,
     "t1": 16.39, "t3": 5.98},
    {"code": "300491", "name": "通合科技", "date": "2025-08-21",
     "o": 23.30, "h": 25.81, "l": 22.82, "c": 22.89, "vol_wan": 2270,
     "pct": -1.08, "upper_pct": 10.97, "upper_body": 6.12, "upper_range": 83.9,
     "lower_range": 2.3, "close_in_range": 2.3, "vol_prev": 1.82, "vol_ma5": 1.39,
     "pos60": 96.2, "high60": 108.4, "pos_quant": 86.9, "ret5": 10.05, "ret20": 20.54,
     "t1": 12.32, "t3": 30.62, "t5": 59.46},
]

# L2 精筛条件：成交量（用户强调）+ K线形态 + 位置，共 5 条核心。
# 量比MA5、盘中创新高改为「标注项 / L2+ 层」，避免 11-13 当天被卡成 0 只。
RULES = [
    ("量比前日 ≥1.05（放量）", "vol_prev", ">=", 1.05),
    ("上影/股价 ≥3.5%", "upper_pct", ">=", 3.5),
    ("上影/全幅 ≥65%", "upper_range", ">=", 65),
    ("下影/全幅 ≤12%（近光脚）", "lower_range", "<=", 12),
    ("pos60 ≥88%", "pos60", ">=", 88),
]
# L2+ 「全标杆特征」：在 L2 之上再叠加量能不过热 + 盘中接近/突破前高
RULES_PLUS = [
    ("量比MA5 ≤1.6（不过热）", "vol_ma5", "<=", 1.6),
    ("盘中高/前60日高 ≥95%", "high60", ">=", 95),
]
# L1 严格复刻（直接用标杆最小值）
STRICT = [
    ("量比前日 ≥1.15", "vol_prev", ">=", 1.15),
    ("量比MA5 ≤1.5", "vol_ma5", "<=", 1.5),
    ("上影/股价 ≥4.8%", "upper_pct", ">=", 4.8),
    ("上影/全幅 ≥70%", "upper_range", ">=", 70),
    ("下影/全幅 ≤8%", "lower_range", "<=", 8),
    ("上影/实体 ≥3.6", "upper_body", ">=", 3.6),
    ("pos60 ≥92%", "pos60", ">=", 92),
    ("盘中高/前60日高 ≥100%", "high60", ">=", 100),
    ("pos_quant ≥70%", "pos_quant", ">=", 70),
]


def clip(x, a, b):
    return float(np.clip(x, a, b))


def similarity(r):
    """标杆画像相似度 0-100：成交量40 / K线形态40 / 位置20。"""
    s = clip(r["vol_prev"] / 1.15, 0, 1) * 20
    s += 20 if 0.8 <= r["vol_ma5"] <= 1.5 else max(0, 20 - abs(r["vol_ma5"] - 1.15) * 25)
    s += clip(r["upper_pct"] / 4.8, 0, 1) * 15
    s += clip(r["upper_range"] / 70, 0, 1) * 10
    s += clip((12 - r["lower_range"]) / 12, 0, 1) * 10
    s += clip((15 - r["close_in_range"]) / 15, 0, 1) * 5
    s += clip((r["pos60"] - 80) / 12, 0, 1) * 8
    s += clip((r["high60"] - 95) / 5, 0, 1) * 8
    s += clip((r["pos_quant"] - 50) / 20, 0, 1) * 4
    return round(s, 1)


def metrics(code, cache):
    df = pd.DataFrame(cache[code])
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    i = int(df.index[df["date"] == pd.Timestamp(DATE)][0])
    o, h, l, c, v = [float(df[k].iloc[i]) for k in ("open", "high", "low", "close", "volume")]
    body = abs(o - c)
    upper = h - max(o, c)
    lower = min(o, c) - l
    rng = h - l
    hh60 = float(df["high"].iloc[i - 60:i].max())
    ll60 = float(df["low"].iloc[i - 60:i].min())
    prev_v = float(df["volume"].iloc[i - 1])
    ma5 = float(df["volume"].iloc[i - 5:i].mean())
    return {
        "o": o, "h": h, "l": l, "c": c, "vol_wan": v / 1e4,
        "upper_pct": upper / c * 100, "upper_body": upper / body,
        "upper_range": upper / rng * 100, "lower_range": lower / rng * 100,
        "close_in_range": (c - l) / rng * 100,
        "vol_prev": v / prev_v, "vol_ma5": v / ma5,
        "pos60": c / hh60 * 100, "high60": h / hh60 * 100,
        "pos_quant": (c - ll60) / (hh60 - ll60) * 100,
        "ret5": (c / float(df["close"].iloc[i - 5]) - 1) * 100,
        "ret20": (c / float(df["close"].iloc[i - 20]) - 1) * 100,
        "pct": (c / float(df["close"].iloc[i - 1]) - 1) * 100,
    }


def apply_rules(row, rules):
    ok, failed = True, []
    for label, key, op, val in rules:
        hit = row[key] >= val if op == ">=" else row[key] <= val
        if not hit:
            ok = False
            failed.append(label)
    return ok, failed


def main():
    with gzip.open(os.path.join(STATE, "klines_cache.json.gz"), "rt", encoding="utf-8") as f:
        cache = json.load(f)
    picks = json.load(open(os.path.join(STATE, "%s_样本.json" % DATE), encoding="utf-8"))["picks"]

    rows = []
    for p in picks:
        r = metrics(p["code"], cache)
        r.update(code=p["code"], name=p["name"], score=p["score"], board=p["board"], close=p["close"])
        r["sim"] = similarity(r)
        ok1, f1 = apply_rules(r, STRICT)
        ok2, f2 = apply_rules(r, RULES)
        okp, fp = apply_rules(r, RULES_PLUS)
        r["L1"], r["L2"] = ok1, ok2
        r["L2PLUS"] = ok2 and okp
        r["failed_L2"] = f2
        r["failed_PLUS"] = fp
        rows.append(r)
    d = pd.DataFrame(rows)

    L1 = d[d.L1].sort_values("sim", ascending=False)
    L2 = d[d.L2].sort_values("sim", ascending=False)
    L2PLUS = d[d.L2PLUS].sort_values("sim", ascending=False)
    L3 = d[(d.sim >= 70) & (~d.L2)].sort_values("sim", ascending=False)

    # 剔除原因
    drop_reasons = {
        "缩量（量比前日<1.10）": d.vol_prev < 1.10,
        "上影偏短（<3.5%）": d.upper_pct < 3.5,
        "上影占全幅不足（<65%）": d.upper_range < 65,
        "下影偏长（>12%）": d.lower_range > 12,
        "位置不够高（pos60<88%）": d.pos60 < 88,
        "量能过热（量比MA5>1.6）": d.vol_ma5 > 1.6,
        "盘中未接近前高（high60<95%）": d.high60 < 95,
    }

    # 落盘 JSON
    out_json = os.path.join(STATE, "%s_标杆精筛.json" % DATE)
    json.dump({
        "date": DATE, "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "anchors": ANCHORS, "rules_L2": [r[0] for r in RULES],
        "rules_L2PLUS": [r[0] for r in RULES_PLUS], "rules_L1": [r[0] for r in STRICT],
        "total_candidates": len(d),
        "counts": {"L1_严格同型": len(L1), "L2_精筛": len(L2), "L2PLUS_全标杆特征": len(L2PLUS),
                   "L3_观察层": len(L3), "剔除": len(d) - len(L2)},
        "drop_reasons": {k: int(v.sum()) for k, v in drop_reasons.items()},
        "picks_L1": L1.to_dict("records"), "picks_L2": L2.to_dict("records"),
        "picks_L2PLUS": L2PLUS.to_dict("records"), "picks_L3": L3.to_dict("records"),
    }, open(out_json, "w", encoding="utf-8"), ensure_ascii=False, indent=2, default=float)

    # 交付 CSV（L2 + L3，带层级）
    cols = ["tier", "code", "name", "close", "score", "sim", "pct", "upper_pct", "upper_body",
            "upper_range", "lower_range", "close_in_range", "vol_prev", "vol_ma5",
            "pos60", "high60", "pos_quant", "ret5", "ret20", "board", "failed_L2"]
    csv_path = os.path.join(OUT, "高位上影线_2026-11-13_标杆精筛.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for _, r in pd.concat([L2, L3]).iterrows():
            tier = ("L2+_全标杆特征" if bool(r["L2PLUS"])
                    else "L2_精筛" if bool(r["L2"]) else "L3_观察层")
            w.writerow([tier] + [(", ".join(r["failed_L2"]) if k == "failed_L2"
                                  else round(float(r[k]), 2) if isinstance(r[k], (int, float, np.floating))
                                  else r[k]) for k in cols[1:]])

    print("总候选 %d | L1严格同型 %d | L2精筛 %d (其中全标杆特征 %d) | L3观察层 %d | 剔除 %d"
          % (len(d), len(L1), len(L2), len(L2PLUS), len(L3), len(d) - len(L2)))
    print("\n[L2 精筛名单]")
    print(L2[["code", "name", "score", "sim", "upper_pct", "upper_range", "lower_range",
              "vol_prev", "vol_ma5", "pos60", "high60", "L2PLUS"]].round(2).to_string(index=False))
    print("\n[剔除原因]")
    for k, v in drop_reasons.items():
        print("  %-32s %3d 只 (%.1f%%)" % (k, int(v.sum()), v.mean() * 100))
    print("\n产出:", out_json, "|", csv_path)
    return {"d": d, "L1": L1, "L2": L2, "L3": L3, "drop": drop_reasons}


if __name__ == "__main__":
    main()
