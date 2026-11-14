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

数据：新浪财经日K 330 根（约 2025-07-07 ~ 2026-11-13），全市场 4606 只。
      60 日窗口后仍有约 270 个信号日，覆盖通合科技 2025-08-21 那天。
运行：python3 high_shadow/backtest_anchor.py [--refresh]
"""
import argparse
import gzip
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(BASE_DIR, ".."))
STATE = os.path.join(BASE_DIR, "state")
CACHE = os.path.join(STATE, "klines_cache_330.json.gz")
UNIVERSE = os.path.join(ROOT, "common", "state", "universe.json")
OUT_JSON = os.path.join(STATE, "backtest_anchor_T1T3.json")
HIST_LEN = 330

sys.path.insert(0, BASE_DIR)
import high_shadow_screener as hs  # noqa: E402

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
    cache = {}
    if os.path.exists(CACHE) and not refresh:
        with gzip.open(CACHE, "rt", encoding="utf-8") as f:
            cache = json.load(f)
        print("[数据] 命中长历史缓存 %d 只" % len(cache))
    uni = json.load(open(UNIVERSE, encoding="utf-8"))
    codes = [str(x["code"]).zfill(6) for x in uni]
    need = [c for c in codes if c not in cache]
    if not need:
        return cache
    print("[数据] 需抓取 %d 只 × %d 根日K ..." % (len(need), HIST_LEN))
    url_tpl = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               "CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen=%d" % HIST_LEN)

    def grab(code):
        for k in range(3):
            try:
                req = urllib.request.Request(url_tpl.format(sym=hs.sina_symbol(code)), headers=hs.UA)
                d = json.loads(urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore"))
                if isinstance(d, list) and d:
                    return code, [{"date": b["day"], "open": float(b["open"]), "high": float(b["high"]),
                                   "low": float(b["low"]), "close": float(b["close"]),
                                   "volume": float(b["volume"])} for b in d]
            except Exception:
                time.sleep(0.3 * (k + 1))
        return code, None

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=20) as ex:
        for idx, (code, bars) in enumerate(ex.map(grab, need), 1):
            if bars:
                cache[code] = bars
            if idx % 500 == 0:
                with gzip.open(CACHE, "wt", encoding="utf-8") as f:
                    json.dump(cache, f)
                print("      进度 %d/%d，耗时 %.0fs" % (idx, len(need), time.time() - t0))
    with gzip.open(CACHE, "wt", encoding="utf-8") as f:
        json.dump(cache, f)
    print("[数据] 完成，共 %d 只，耗时 %.0fs" % (len(cache), time.time() - t0))
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
