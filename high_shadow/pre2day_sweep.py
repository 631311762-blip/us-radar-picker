#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
高位上影线 · 第二个条件研究：信号日【前两日】股价情况 / 最优位置
=====================================================================
第一条件已定：量比 0.85~1.4（v1.3）。本脚本研究第二条件——
信号日之前两个交易日(T-2、T-1)的股价表现与位置，回答「前两日
走成什么样、处在什么位置，后续 T+1/T+3/T+5 收益与准确率最优」。

特征（i = 信号日索引，前两日 = i-2 / i-1）：
  d2        T-2 单日涨跌%              (C[i-2]/C[i-3]-1)
  d1        T-1 单日涨跌%              (C[i-1]/C[i-2]-1)
  ret2      前两日累计涨跌%            (C[i-1]/C[i-3]-1)   ← 核心
  ret5prev  前5日累计涨跌%             (C[i-1]/C[i-6]-1)
  amp2      前两日振幅%               (max H - min L)/C[i-3]
  pos60_prev  T-1 收盘 / 60日高%      hh60 = max(H[i-60:i]) 与模型同口径
  posq_prev   T-1 在 60 日区间分位%   (C[i-1]-ll60)/(hh60-ll60)
  ma5_dev    T-1 偏离 5 日均线%
  ma20_dev   T-1 偏离 20 日均线%
  pattern    前两日阴阳组合            阳阳/阳阴/阴阳/阴阴
  gap        信号日开盘跳空%           (O[i]/C[i-1]-1)
  vol_prev2  T-1 量 / T-2 量

判据（延续前几轮，防止被 beta 与重复计数误导）：
  1) 表面：收益均值 / 上涨准确率 / 下跌命中率
  2) 中性化：减去同日全市场平均收益的超额
  3) 稳健性：按信号日聚类 t 检验 + 每股去重 + 前后半分期 + 多重比较清点

主口径 = v1.3（量比 0.85~1.4）子集；另给全量 19,580 条作对照。
运行：python3 high_shadow/pre2day_sweep.py
产出：state/pre2day_sweep.json
      state/pre2day_features.csv
      /workspace/高位上影线_前两日位置_分桶明细.csv
      /workspace/高位上影线_2026-11-13_前两日位置标注.csv
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
import volratio_sweep as vs  # build_market / add_t5 / stat_block / HORIZONS

CACHE = os.path.join(STATE, "klines_cache_330.json.gz")
SIG = os.path.join(STATE, "backtest_anchor_signals.csv")
SAMPLE = os.path.join(STATE, "2026-11-13_样本.json")
FEAT_OUT = os.path.join(STATE, "pre2day_features.csv")
OUT_JSON = os.path.join(STATE, "pre2day_sweep.json")
OUT_CSV = os.path.join(WORKSPACE, "高位上影线_前两日位置_分桶明细.csv")
OUT_MAP = os.path.join(WORKSPACE, "高位上影线_2026-11-13_前两日位置标注.csv")

LO, HI = 0.85, 1.4  # 第一条件 v1.3

# (字段, 中文名, 边界, 标签)
BINS = [
    ("ret2", "前两日累计涨幅%", [-np.inf, -6, -3, 0, 3, 6, 10, np.inf],
     ["<-6", "-6~-3", "-3~0", "0~3", "3~6", "6~10", "≥10"]),
    ("d1", "前一日(T-1)涨跌%", [-np.inf, -3, -1, 0, 1, 3, 6, np.inf],
     ["<-3", "-3~-1", "-1~0", "0~1", "1~3", "3~6", "≥6"]),
    ("d2", "前二日(T-2)涨跌%", [-np.inf, -3, -1, 0, 1, 3, 6, np.inf],
     ["<-3", "-3~-1", "-1~0", "0~1", "1~3", "3~6", "≥6"]),
    ("ret5prev", "前5日累计涨幅%", [-np.inf, -10, -5, 0, 5, 10, 20, np.inf],
     ["<-10", "-10~-5", "-5~0", "0~5", "5~10", "10~20", "≥20"]),
    ("pos60_prev", "T-1收盘/60日高%", [-np.inf, 80, 85, 90, 95, 98, np.inf],
     ["<80", "80~85", "85~90", "90~95", "95~98", "≥98"]),
    ("posq_prev", "T-1在60日区间分位%", [-np.inf, 20, 40, 60, 80, 95, np.inf],
     ["<20", "20~40", "40~60", "60~80", "80~95", "≥95"]),
    ("ma5_dev", "T-1偏离5日线%", [-np.inf, -3, -1, 0, 1, 3, 6, np.inf],
     ["<-3", "-3~-1", "-1~0", "0~1", "1~3", "3~6", "≥6"]),
    ("ma20_dev", "T-1偏离20日线%", [-np.inf, -8, -3, 0, 3, 8, 15, np.inf],
     ["<-8", "-8~-3", "-3~0", "0~3", "3~8", "8~15", "≥15"]),
    ("amp2", "前两日振幅%", [0, 3, 5, 8, 12, 18, np.inf],
     ["<3", "3~5", "5~8", "8~12", "12~18", "≥18"]),
    ("gap", "信号日跳空%", [-np.inf, -2, -0.5, 0.5, 2, np.inf],
     ["<-2", "-2~-0.5", "-0.5~0.5", "0.5~2", "≥2"]),
    ("vol_prev2", "T-1量/T-2量", [0, 0.7, 0.9, 1.1, 1.4, 2.0, np.inf],
     ["<0.7", "0.7~0.9", "0.9~1.1", "1.1~1.4", "1.4~2.0", "≥2.0"]),
]
PATTERNS = ["阳阳", "阳阴", "阴阳", "阴阴"]
CORE = ["ret2", "d1", "posq_prev", "pos60_prev", "ma5_dev", "pattern"]


def build_cmap(cache):
    out = {}
    for code, bars in cache.items():
        if not bars:
            continue
        out[code] = {
            "date": [b["date"] for b in bars],
            "o": np.asarray([b["open"] for b in bars], float),
            "h": np.asarray([b["high"] for b in bars], float),
            "l": np.asarray([b["low"] for b in bars], float),
            "c": np.asarray([b["close"] for b in bars], float),
            "v": np.asarray([b["volume"] for b in bars], float),
        }
    return out


def features_of(cmap, code, dt):
    e = cmap.get(code)
    if not e:
        return None
    dates = e["date"]
    try:
        i = dates.index(dt)
    except ValueError:
        return None
    if i < 61 or i >= len(dates):  # 需完整 60 日窗口
        return None
    C, O, H, L, V = e["c"], e["o"], e["h"], e["l"], e["v"]
    hh60 = float(H[i - 60:i].max())
    ll60 = float(L[i - 60:i].min())
    ma5 = float(C[i - 5:i].mean())
    ma20 = float(C[i - 20:i].mean())
    s2 = "阳" if C[i - 2] > O[i - 2] else "阴"
    s1 = "阳" if C[i - 1] > O[i - 1] else "阴"
    r = {
        "code": code, "date": dt,
        "d2": (C[i - 2] / C[i - 3] - 1) * 100,
        "d1": (C[i - 1] / C[i - 2] - 1) * 100,
        "ret2": (C[i - 1] / C[i - 3] - 1) * 100,
        "ret5prev": (C[i - 1] / C[i - 6] - 1) * 100,
        "amp2": (max(H[i - 2], H[i - 1]) - min(L[i - 2], L[i - 1])) / C[i - 3] * 100,
        "pos60_prev": C[i - 1] / hh60 * 100 if hh60 > 0 else np.nan,
        "posq_prev": (C[i - 1] - ll60) / (hh60 - ll60) * 100 if hh60 > ll60 else np.nan,
        "ma5_dev": (C[i - 1] / ma5 - 1) * 100 if ma5 > 0 else np.nan,
        "ma20_dev": (C[i - 1] / ma20 - 1) * 100 if ma20 > 0 else np.nan,
        "pattern": s2 + s1,
        "gap": (O[i] / C[i - 1] - 1) * 100,
        "vol_prev2": V[i - 1] / V[i - 2] if V[i - 2] > 0 else np.nan,
        "close_prev": float(C[i - 1]),
        "hh60_prev": hh60,
    }
    return r


def add_bins(d):
    for fld, name, edges, labels in BINS:
        d[fld + "_bin"] = pd.cut(d[fld], edges, labels=labels, right=True).astype(str)
    return d


def sweep_dims(sig, dims, min_n=1):
    """对给定维度逐桶 stat_block。"""
    out = {}
    for fld, name, edges, labels in BINS:
        if fld not in dims:
            continue
        out[fld] = {"name": name, "labels": labels, "buckets": {}}
        for lab in labels:
            sub = sig[sig[fld + "_bin"] == lab]
            out[fld]["buckets"][lab] = vs.stat_block(sub)
    if "pattern" in dims:
        out["pattern"] = {"name": "前两日阴阳组合", "labels": PATTERNS, "buckets": {}}
        for lab in PATTERNS:
            out["pattern"]["buckets"][lab] = vs.stat_block(sig[sig["pattern"] == lab])
    return out


def main():
    t0 = time.time()
    with gzip.open(CACHE, "rt", encoding="utf-8") as f:
        cache = json.load(f)
    sig = pd.read_csv(SIG, dtype={"code": str})
    sig["code"] = sig["code"].str.zfill(6)
    sig = sig.reset_index(drop=True)
    print("[数据] 缓存 %d 只，信号 %d 条" % (len(cache), len(sig)))

    cmap = build_cmap(cache)
    targets = list(zip(sig["code"].tolist(), sig["date"].tolist()))
    feat = [r for c, d in targets for r in [features_of(cmap, c, d)] if r is not None]
    F = pd.DataFrame(feat)
    print("[特征] 有效 %d / %d 条，耗时 %.0fs" % (len(F), len(sig), time.time() - t0))

    # 市场基准 + T+5 + 中性化
    mkt = vs.build_market(cache)
    sig["t5"] = vs.add_t5(sig, cache)
    sig = sig.merge(mkt, left_on="date", right_index=True, how="left").reset_index(drop=True)
    for tag, tc, ec, mc in vs.HORIZONS:
        sig[ec] = sig[tc] - sig[mc]

    df = sig.merge(F, on=["code", "date"], how="inner")
    df = add_bins(df)
    df.to_csv(FEAT_OUT, index=False, encoding="utf-8-sig")
    print("[特征] 合并后 %d 条，T+5 有效 %d 条" % (len(df), int(df.t5.notna().sum())))

    m13 = df.vol_prev.between(LO, HI) & df.vol_ma5.between(LO, HI)
    v13 = df[m13].copy()
    print("[口径] 全量 %d 条 → v1.3(量比0.85~1.4) %d 条" % (len(df), len(v13)))

    result = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "definition": {
            "第一条件": "量比 vol_prev 与 vol_ma5 同时落在 [0.85, 1.4]（v1.3）",
            "前两日": "信号日之前的两个交易日 T-2、T-1；基准收盘为 T-3",
            "hh60口径": "max(H[i-60:i])，即截至 T-1 的 60 日高，与 high_shadow_screener.detect 的 rolling(60).shift(1) 一致",
            "主口径": "v1.3 子集（第一条件已生效）；full 为未加第一条件的全量对照",
            "上涨准确率": "T+N 上涨占比（做多胜率）；下跌命中率对应风险名单 win_rule=down",
            "超额": "信号收益 − 同日全市场平均收益（中性化）",
            "聚类t": "按信号日聚类后逐日超额 t 值，|t|<2 视为不显著",
        },
        "data": {"signals_full": len(df), "signals_v13": len(v13),
                 "signal_range": [df.date.min(), df.date.max()],
                 "market_days": len(mkt), "t5_valid": int(df.t5.notna().sum()),
                 "band": [LO, HI]},
        "by_dim_v13": {}, "by_dim_full": {}, "grid": {},
        "dedup": {}, "half": {}, "signif": {}, "best": {},
    }

    all_dims = [b[0] for b in BINS] + ["pattern"]
    result["by_dim_v13"] = sweep_dims(v13, all_dims)
    result["by_dim_full"] = sweep_dims(df, all_dims)

    # 二维网格（只在 v1.3 上，样本下限 100）
    def bin_col(fld):
        return "pattern" if fld == "pattern" else fld + "_bin"

    for a, b in (("ret2", "posq_prev"), ("ret2", "pattern"),
                 ("d1", "pattern"), ("posq_prev", "ma5_dev"),
                 ("ret2", "d1")):
        for la in result["by_dim_v13"][a]["labels"]:
            for lb in result["by_dim_v13"][b]["labels"]:
                sub = v13[(v13[bin_col(a)] == la) & (v13[bin_col(b)] == lb)]
                if len(sub) < 100:
                    continue
                result["grid"]["%s=%s × %s=%s" % (a, la, b, lb)] = {
                    "a": a, "la": la, "b": b, "lb": lb,
                    **vs.stat_block(sub)}

    # 去重 / 分期（仅核心维度）
    dq = v13.sort_values("date").drop_duplicates("code")
    result["dedup"] = {"独立股票数": len(dq), "dims": sweep_dims(dq, CORE)}
    uniq = sorted(v13["date"].unique())
    mid = uniq[len(uniq) // 2]
    for name, sub in (("前半", v13[v13.date <= mid]), ("后半", v13[v13.date > mid])):
        result["half"][name] = {"date_split": str(mid), "n": len(sub),
                                "dims": sweep_dims(sub, CORE)}

    # 显著项清点（多重比较）
    tests = 0
    sig_list = []
    for scope, dims in (("v13", result["by_dim_v13"]), ("full", result["by_dim_full"])):
        for fld, blk in dims.items():
            for lab, b in blk["buckets"].items():
                for t in ("T1", "T3", "T5"):
                    s = b.get(t)
                    if not s:
                        continue
                    tests += 1
                    tv = s.get("cluster_t")
                    if tv is not None and abs(float(tv)) >= 2:
                        sig_list.append({"scope": scope, "dim": fld, "bucket": lab,
                                         "period": t, "t": tv,
                                         "excess": s.get("cluster_excess"), "n": s["n"]})
    for k, b in result["grid"].items():
        for t in ("T1", "T3", "T5"):
            s = b.get(t)
            if not s:
                continue
            tests += 1
            tv = s.get("cluster_t")
            if tv is not None and abs(float(tv)) >= 2:
                sig_list.append({"scope": "grid", "dim": k, "bucket": "", "period": t,
                                 "t": tv, "excess": s.get("cluster_excess"), "n": s["n"]})
    # Bonferroni 校正：α=0.05 双侧，按检验次数查常用正态近似阈值
    tbl = [(10, 2.81), (20, 3.02), (30, 3.15), (50, 3.29), (100, 3.48),
           (200, 3.66), (300, 3.76), (500, 3.88), (1000, 4.03)]
    thr = tbl[-1][1]
    for n, v in tbl:
        if tests <= n:
            thr = v
            break
    result["signif"] = {"总检验数": tests, "显著项": sig_list,
                        "bonferroni_t阈值": thr}

    # 最优区间排序（v1.3，样本>=150，排除极端小桶）
    def rank(dims, key, period, reverse=True):
        arr = []
        for fld, blk in dims.items():
            for lab, b in blk["buckets"].items():
                s = b.get(period)
                if not s or s["n"] < 150:
                    continue
                v = s.get(key)
                if v is None:
                    continue
                arr.append((fld, blk["name"], lab, s["n"], v, s.get("cluster_t")))
        return sorted(arr, key=lambda x: -x[4] if reverse else x[4])[:10]

    result["best"] = {
        "按T1上涨准确率": rank(result["by_dim_v13"], "up_rate", "T1"),
        "按T1收益均值": rank(result["by_dim_v13"], "mean", "T1"),
        "按T3收益均值": rank(result["by_dim_v13"], "mean", "T3"),
        "按T3下跌命中率": rank(result["by_dim_v13"], "down_rate", "T3"),
        "按T5下跌命中率": rank(result["by_dim_v13"], "down_rate", "T5"),
        "按T1中性化超额": rank(result["by_dim_v13"], "excess_mean", "T1"),
        "按T3中性化超额": rank(result["by_dim_v13"], "excess_mean", "T3"),
    }

    json.dump(result, open(OUT_JSON, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2, default=float)

    # 分桶明细 CSV
    rows = []
    for scope, dims in (("v1.3主口径", result["by_dim_v13"]), ("全量对照", result["by_dim_full"])):
        for fld, blk in dims.items():
            for lab, b in blk["buckets"].items():
                for t in ("T1", "T3", "T5"):
                    s = b.get(t)
                    if not s:
                        continue
                    rows.append({"口径": scope, "维度": blk["name"], "区间": lab, "周期": t,
                                 "样本": s["n"], "收益均值%": s["mean"], "收益中位%": s["median"],
                                 "上涨准确率%": s["up_rate"], "下跌命中率%": s["down_rate"],
                                 "超额均值%": s.get("excess_mean"), "跑赢市场率%": s.get("excess_up_rate"),
                                 "聚类t": s.get("cluster_t"), "聚类超额%": s.get("cluster_excess"),
                                 "聚类正日率%": s.get("cluster_pos_day_rate")})
    pd.DataFrame(rows).to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

    # 11-13 实盘映射
    sample = json.load(open(SAMPLE, encoding="utf-8"))["picks"]
    maprows = []
    for p in sample:
        code = str(p["code"]).zfill(6)
        r = features_of(cmap, code, "2026-11-13")
        if not r:
            continue
        for fld, name, edges, labels in BINS:
            r[fld + "_bin"] = (pd.cut([r[fld]], edges, labels=labels, right=True).astype(str)[0])
        base = {k: v for k, v in p.items() if k != "raw"}
        maprows.append({**base, **p.get("raw", {}), **r})
    M = pd.DataFrame(maprows)
    if len(M):
        # 推荐区间：由 ret2 维度 T+1 准确率最高 / T+3 下跌命中率最高的桶自动生成
        rb = result["by_dim_v13"]["ret2"]["buckets"]
        lc = [(lab, b) for lab, b in rb.items() if b.get("T1") and b["T1"]["n"] >= 150]
        rc = [(lab, b) for lab, b in rb.items() if b.get("T3") and b["T3"]["n"] >= 150]
        best_long = max(lc, key=lambda x: x[1]["T1"]["up_rate"])[0] if lc else ""
        best_risk = max(rc, key=lambda x: x[1]["T3"]["down_rate"])[0] if rc else ""
        M["做多视角最优区间"] = np.where(M["ret2_bin"] == best_long, "Y", "")
        M["风险视角最优区间"] = np.where(M["ret2_bin"] == best_risk, "Y", "")
        cols = ["code", "name", "close", "score", "board",
                "vol_ratio_prev", "vol_ratio", "upper_pct", "pos60",
                "d2", "d1", "ret2", "ret5prev", "amp2", "pos60_prev", "posq_prev",
                "ma5_dev", "ma20_dev", "pattern", "gap", "vol_prev2",
                "ret2_bin", "d1_bin", "posq_prev_bin", "pos60_prev_bin",
                "ma5_dev_bin", "pattern", "做多视角最优区间", "风险视角最优区间"]
        cols = [c for c in dict.fromkeys(cols) if c in M.columns]
        out = M[cols].copy()
        for c in out.columns:
            if out[c].dtype.kind == "f":
                out[c] = out[c].round(3)
        out.to_csv(OUT_MAP, index=False, encoding="utf-8-sig")
        print("[映射] 11-13 候选 %d 只 → 标注 CSV；推荐区间 做多=%s 风险=%s"
              % (len(out), best_long, best_risk))
        result["mapping"] = {"count": len(out), "best_long_bucket": best_long,
                             "best_risk_bucket": best_risk,
                             "long_hits": int((M["做多视角最优区间"] == "Y").sum()),
                             "risk_hits": int((M["风险视角最优区间"] == "Y").sum())}
        json.dump(result, open(OUT_JSON, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2, default=float)

    # 控制台摘要（核心维度 ret2 / posq_prev / pattern，v1.3）
    print("\n=== v1.3 主口径：前两日累计涨幅 ret2 分桶 ===")
    print("%-10s %6s | %9s %9s %9s %6s | %9s %9s %9s %6s | %9s %9s %9s %6s"
          % ("区间", "样本", "T1收益", "T1准确", "T1超额", "T1_t",
             "T3收益", "T3准确", "T3超额", "T3_t", "T5收益", "T5准确", "T5超额", "T5_t"))
    for lab, b in result["by_dim_v13"]["ret2"]["buckets"].items():
        if not b.get("n"):
            continue
        cells = []
        for t in ("T1", "T3", "T5"):
            s = b.get(t, {})
            cells += [s.get("mean", 0), s.get("up_rate", 0), s.get("excess_mean", 0), s.get("cluster_t", 0)]
        print("%-10s %6d | %9.3f %9.2f %9.3f %6s | %9.3f %9.2f %9.3f %6s | %9.3f %9.2f %9.3f %6s"
              % (lab, b["n"], *cells))

    print("\n=== v1.3：T-1 在60日区间分位 posq_prev 分桶 ===")
    for lab, b in result["by_dim_v13"]["posq_prev"]["buckets"].items():
        if not b.get("n"):
            continue
        cells = []
        for t in ("T1", "T3", "T5"):
            s = b.get(t, {})
            cells += [s.get("mean", 0), s.get("up_rate", 0), s.get("excess_mean", 0), s.get("cluster_t", 0)]
        print("%-10s %6d | %9.3f %9.2f %9.3f %6s | %9.3f %9.2f %9.3f %6s | %9.3f %9.2f %9.3f %6s"
              % (lab, b["n"], *cells))

    print("\n=== v1.3：前两日阴阳组合 ===")
    for lab, b in result["by_dim_v13"]["pattern"]["buckets"].items():
        if not b.get("n"):
            continue
        cells = []
        for t in ("T1", "T3", "T5"):
            s = b.get(t, {})
            cells += [s.get("mean", 0), s.get("up_rate", 0), s.get("excess_mean", 0), s.get("cluster_t", 0)]
        print("%-10s %6d | %9.3f %9.2f %9.3f %6s | %9.3f %9.2f %9.3f %6s | %9.3f %9.2f %9.3f %6s"
              % (lab, b["n"], *cells))

    print("\n=== 显著项 |t|>=2（共 %d 项 / %d 次检验，Bonferroni阈值 %.2f）==="
          % (len(sig_list), tests, thr))
    for x in sig_list[:20]:
        print("  ", x)

    print("\n耗时 %.0fs" % (time.time() - t0))
    print("产出:", OUT_JSON, "\n     ", OUT_CSV, "\n     ", OUT_MAP)
    return result


if __name__ == "__main__":
    main()
