#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
高位上影线 · 量比区间寻优  volratio_sweep.py
=====================================================================
在 19,580 条历史信号上，回答「量比落在哪个范围，T+1 / T+3 / T+5
的收益率与准确率最高」，并同时给出三层判据防止被市场 beta 与
重复计数误导：

  1) 表面口径：原始收益均值 / 上涨准确率 / 下跌命中率
  2) 中性化口径：减去同日全市场平均收益后的超额（eN）
  3) 稳健性：按信号日聚类 t 检验 + 每股去重 + 前后半分期复核

两个量比定义（与 high_shadow_screener.detect 一致）：
  vol_prev = 当日成交量 / 前一日成交量   （放量冲击）
  vol_ma5  = 当日成交量 / 前5日均量       （相对基准热度）

数据：klines_cache_330.json.gz（4606 只 × 330 根日K），
      信号明细 backtest_anchor_signals.csv（2025-09-17 ~ 2026-11-10）
运行：python3 high_shadow/volratio_sweep.py
产出：high_shadow/state/volratio_sweep.json
      /workspace/高位上影线_量比区间寻优_分桶明细.csv
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
CACHE = os.path.join(STATE, "klines_cache_330.json.gz")
SIG = os.path.join(STATE, "backtest_anchor_signals.csv")
OUT_JSON = os.path.join(STATE, "volratio_sweep.json")
OUT_CSV = os.path.join(WORKSPACE, "高位上影线_量比区间寻优_分桶明细.csv")

VP_BINS = [0, 0.6, 0.8, 1.0, 1.2, 1.5, 2.0, 3.0, np.inf]
VM_BINS = [0, 0.6, 0.8, 1.0, 1.2, 1.5, 2.0, np.inf]
VP_LABELS = ["<0.6", "0.6~0.8", "0.8~1.0", "1.0~1.2", "1.2~1.5", "1.5~2.0", "2.0~3.0", "≥3.0"]
VM_LABELS = ["<0.6", "0.6~0.8", "0.8~1.0", "1.0~1.2", "1.2~1.5", "1.5~2.0", "≥2.0"]
HORIZONS = [("T1", "t1", "e1", "m1"),
            ("T3", "t3", "e3", "m3"),
            ("T5", "t5", "e5", "m5")]


def build_market(cache):
    """按交易日聚合全市场 T+1/T+3/T+5 平均收益，作为中性化基准。"""
    frames = []
    t0 = time.time()
    for j, (code, bars) in enumerate(cache.items(), 1):
        if not bars or len(bars) < 66:
            continue
        c = np.asarray([b["close"] for b in bars], dtype=float)
        dates = [b["date"] for b in bars]
        n = len(c)
        # 覆盖到 n-4，保证最后一个信号日也有 T+1/T+3 基准；
        # T+5 越界的日子置 NaN，groupby.mean 会自动跳过
        idx = np.arange(61, n - 3)
        idx = idx[np.isfinite(c[idx]) & (c[idx] > 0)]
        if len(idx) == 0:
            continue
        with np.errstate(divide="ignore", invalid="ignore"):
            r1 = (c[idx + 1] / c[idx] - 1) * 100
            r3 = (c[idx + 3] / c[idx] - 1) * 100
        r5 = np.full(len(idx), np.nan)
        ok = idx + 5 < n
        r5[ok] = (c[idx[ok] + 5] / c[idx[ok]] - 1) * 100
        frames.append(pd.DataFrame({"date": [dates[i] for i in idx],
                                    "m1": r1, "m3": r3, "m5": r5}))
        if j % 1500 == 0:
            print("      [市场基准] %d/%d，%.0fs" % (j, len(cache), time.time() - t0))
    mkt = pd.concat(frames, ignore_index=True).groupby("date").mean()
    mkt.columns = ["m1", "m3", "m5"]
    print("[市场基准] %d 个交易日，耗时 %.0fs" % (len(mkt), time.time() - t0))
    return mkt


def add_t5(sig, cache):
    """为每条信号补算 T+5 收益（缓存不足则 NaN）。

    注意：同一只票在区间内会产生多条信号，必须按「行号」逐条写回，
    不能用 code 做字典 key（会互相覆盖）。
    """
    out = np.full(len(sig), np.nan)
    cmap = {}
    for code, bars in cache.items():
        if not bars:
            continue
        cmap[code] = (np.asarray([b["close"] for b in bars], dtype=float),
                      {b["date"]: i for i, b in enumerate(bars)})
    for k, row in enumerate(sig.itertuples(index=False)):
        entry = cmap.get(row.code)
        if not entry:
            continue
        c, imap = entry
        i = imap.get(row.date)
        if i is None or i + 5 >= len(c) or c[i] <= 0:
            continue
        out[k] = (c[i + 5] / c[i] - 1) * 100
    return out


def stat_block(sub, horizons=HORIZONS):
    """单组统计：表面收益/准确率 + 中性化超额 + 按日聚类 t 检验。"""
    res = {"n": int(len(sub))}
    if len(sub) == 0:
        return res
    for tag, tc, ec, mc in horizons:
        a = sub[tc].dropna().to_numpy(float)
        e = sub[ec].dropna().to_numpy(float)
        if len(a) == 0:
            continue
        b = {"n": int(len(a)),
             "mean": round(float(a.mean()), 3),
             "median": round(float(np.median(a)), 3),
             "up_rate": round(float((a > 0).mean()) * 100, 2),
             "down_rate": round(float((a < 0).mean()) * 100, 2),
             "gt3_rate": round(float((a > 3).mean()) * 100, 2),
             "lt_neg3_rate": round(float((a < -3).mean()) * 100, 2)}
        if len(e):
            b["excess_mean"] = round(float(e.mean()), 3)
            b["excess_median"] = round(float(np.median(e)), 3)
            b["excess_up_rate"] = round(float((e > 0).mean()) * 100, 2)
        # 按信号日聚类配对：桶内日均 − 同日全市场日均，逐日超额做 t 检验
        g = sub.dropna(subset=[ec]).groupby("date")[ec].mean()
        arr = g.to_numpy(float)
        if len(arr) > 1:
            se = arr.std(ddof=1) / np.sqrt(len(arr))
            b["cluster_days"] = int(len(arr))
            b["cluster_excess"] = round(float(arr.mean()), 3)
            b["cluster_t"] = round(float(arr.mean() / se), 2) if se > 0 else None
            b["cluster_pos_day_rate"] = round(float((arr > 0).mean()) * 100, 2)
        res[tag] = b
    return res


def sweep(sig, dim, labels, prefix, result):
    """单维度分桶扫描。"""
    for lab in labels:
        sub = sig[sig[dim + "_bin"] == lab]
        result[prefix][lab] = stat_block(sub)
    return result


def main():
    with gzip.open(CACHE, "rt", encoding="utf-8") as f:
        cache = json.load(f)
    sig = pd.read_csv(SIG, dtype={"code": str})
    sig["code"] = sig["code"].str.zfill(6)
    sig["L2"] = sig["L2"].astype(bool)
    sig["L2PLUS"] = sig["L2PLUS"].astype(bool)
    sig = sig.reset_index(drop=True)
    print("[数据] 缓存 %d 只，信号 %d 条" % (len(cache), len(sig)))

    mkt = build_market(cache)
    sig["t5"] = add_t5(sig, cache)
    sig = sig.merge(mkt, left_on="date", right_index=True, how="left").reset_index(drop=True)
    for tag, tc, ec, mc in HORIZONS:
        sig[ec] = sig[tc] - sig[mc]

    print("[数据] T+5 有效 %d / %d，信号区间 %s ~ %s"
          % (int(sig.t5.notna().sum()), len(sig), sig.date.min(), sig.date.max()))
    print("[数据] T+5 覆盖至 %s" % sig.loc[sig.t5.notna(), "date"].max())

    sig["vp_bin"] = pd.cut(sig.vol_prev, VP_BINS, labels=VP_LABELS, right=True).astype(str)
    sig["vm_bin"] = pd.cut(sig.vol_ma5, VM_BINS, labels=VM_LABELS, right=True).astype(str)

    result = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "definition": {
            "vol_prev": "当日成交量 / 前一日成交量（放量冲击）",
            "vol_ma5": "当日成交量 / 前5日均量（相对基准热度）",
            "T+1/T+3/T+5": "信号日收盘 → 第 1/3/5 个交易日收盘涨跌幅",
            "上涨准确率": "T+N 上涨占比（做多胜率）",
            "下跌命中率": "T+N 下跌占比（风险名单 win_rule=down 命中率）",
            "超额": "信号收益 − 同日全市场平均收益（中性化）",
            "聚类t": "按信号日聚类后逐日超额的 t 值，|t|<2 视为不显著",
        },
        "data": {"signals": int(len(sig)), "signal_range": [sig.date.min(), sig.date.max()],
                 "market_days": int(len(mkt)),
                 "t5_valid": int(sig.t5.notna().sum()),
                 "t5_last_date": str(sig.loc[sig.t5.notna(), "date"].max()),
                 "vp_bins": VP_LABELS, "vm_bins": VM_LABELS},
        "by_vol_prev": {}, "by_vol_ma5": {}, "grid": {},
        "dedup": {}, "half": {}, "l2_reference": {},
    }

    # ① 两个量比维度分桶
    sweep(sig, "vp", VP_LABELS, "by_vol_prev", result)
    sweep(sig, "vm", VM_LABELS, "by_vol_ma5", result)

    # ② 二维联合网格（样本量下限 100）
    for vpl in VP_LABELS:
        for vml in VM_LABELS:
            sub = sig[(sig.vp_bin == vpl) & (sig.vm_bin == vml)]
            if len(sub) < 100:
                continue
            key = "%s × %s" % (vpl, vml)
            result["grid"][key] = {"vp": vpl, "vm": vml, **stat_block(sub)}

    # ③ 去重稳健性（每股只留首个信号，再按同样分桶）
    dq = sig.sort_values("date").drop_duplicates("code")
    for lab in VP_LABELS:
        result["dedup"]["vol_prev::" + lab] = stat_block(dq[dq.vp_bin == lab])
    for lab in VM_LABELS:
        result["dedup"]["vol_ma5::" + lab] = stat_block(dq[dq.vm_bin == lab])

    # ④ 前后半分期稳健性
    uniq = sorted(sig["date"].unique())
    mid = uniq[len(uniq) // 2]
    for name, sub in (("前半", sig[sig.date <= mid]), ("后半", sig[sig.date > mid])):
        result["half"][name] = {"date_split": str(mid), "by_vol_prev": {}, "by_vol_ma5": {}}
        for lab in VP_LABELS:
            result["half"][name]["by_vol_prev"][lab] = stat_block(sub[sub.vp_bin == lab])
        for lab in VM_LABELS:
            result["half"][name]["by_vol_ma5"][lab] = stat_block(sub[sub.vm_bin == lab])

    # ⑤ L2 精筛子样本参考
    for lab in VP_LABELS:
        result["l2_reference"]["vol_prev::" + lab] = stat_block(sig[sig.L2 & (sig.vp_bin == lab)])

    json.dump(result, open(OUT_JSON, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2, default=float)

    # 分桶明细 CSV（供复核）
    rows = []
    for dim, labels, key in (("vol_prev", VP_LABELS, "by_vol_prev"),
                             ("vol_ma5", VM_LABELS, "by_vol_ma5")):
        for lab in labels:
            b = result[key][lab]
            for tag, _, _, _ in HORIZONS:
                if tag not in b:
                    continue
                s = b[tag]
                rows.append({"维度": dim, "区间": lab, "周期": tag,
                             "样本": s["n"], "收益均值%": s["mean"], "收益中位%": s["median"],
                             "上涨准确率%": s["up_rate"], "下跌命中率%": s["down_rate"],
                             "超额均值%": s.get("excess_mean"), "跑赢市场率%": s.get("excess_up_rate"),
                             "聚类t": s.get("cluster_t"), "聚类超额%": s.get("cluster_excess"),
                             "聚类正日率%": s.get("cluster_pos_day_rate")})
    pd.DataFrame(rows).to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

    # 控制台摘要
    print("\n=== 量比前日(vol_prev)分桶 · T+1 / T+3 / T+5 ===")
    print("%-10s %6s | %8s %8s %9s %6s | %8s %8s %9s %6s | %8s %8s %9s %6s"
          % ("区间", "样本", "T1收益", "T1准确", "T1超额", "T1_t",
             "T3收益", "T3准确", "T3超额", "T3_t",
             "T5收益", "T5准确", "T5超额", "T5_t"))
    for lab in VP_LABELS:
        b = result["by_vol_prev"][lab]
        if not b.get("n"):
            continue
        cells = []
        for tag, _, _, _ in HORIZONS:
            s = b.get(tag, {})
            cells += [s.get("mean", 0), s.get("up_rate", 0), s.get("excess_mean", 0), s.get("cluster_t", 0)]
        print("%-10s %6d | %8.2f %8.2f %9.3f %6s | %8.2f %8.2f %9.3f %6s | %8.2f %8.2f %9.3f %6s"
              % (lab, b["n"], *cells))

    print("\n=== 量比MA5(vol_ma5)分桶 ===")
    print("%-10s %6s | %8s %8s %9s %6s | %8s %8s %9s %6s | %8s %8s %9s %6s"
          % ("区间", "样本", "T1收益", "T1准确", "T1超额", "T1_t",
             "T3收益", "T3准确", "T3超额", "T3_t",
             "T5收益", "T5准确", "T5超额", "T5_t"))
    for lab in VM_LABELS:
        b = result["by_vol_ma5"][lab]
        if not b.get("n"):
            continue
        cells = []
        for tag, _, _, _ in HORIZONS:
            s = b.get(tag, {})
            cells += [s.get("mean", 0), s.get("up_rate", 0), s.get("excess_mean", 0), s.get("cluster_t", 0)]
        print("%-10s %6d | %8.2f %8.2f %9.3f %6s | %8.2f %8.2f %9.3f %6s | %8.2f %8.2f %9.3f %6s"
              % (lab, b["n"], *cells))

    print("\n产出:", OUT_JSON, "\n     ", OUT_CSV)
    return result


if __name__ == "__main__":
    main()
