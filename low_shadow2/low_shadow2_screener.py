#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
低位上影模型2 · 合并流水线接入版  low_shadow2_screener.py
=========================================================
模型来源：由「低位上影线模型2(双栏) v2.3」拆出的低位栏（偏多·买入候选），
即云端 /workspace/low_shadow_model 包的同一套口径——config.json + 本文件
detect()/vol_score()/score() 与云端 model_core.py 逐行一致，仅数据入口换成
合并流水线共同前置层（universe + K线共享缓存），并经 kit 出口 gate（立信/ST/
样本池三重校验）写统一推荐台账 common/state/recommendations/low_shadow2/。

与注册表中「low_shadow（严格上影线模型）」是两个独立模型：
    - low_shadow   : 严格版低位上影线（形态更严的独立模型）
    - low_shadow2  : 本模型（低位上影模型2，双栏拆分低位栏口径）
两者各自登记、各自样本文件、各自回溯参数，互不混用。

可选二次过滤层（2026-11-15，config.filter 驱动）：独立于 detect() 核心信号，
    - prior2_cum_max=0.0 : 剔除「前两日累计涨跌 > 0」的净涨形态
    - vol_ratio_min=1.1  : 剔除「当日量/前5日均量 < 1.1」的缩量信号
    - enabled=false 即一键回滚到分栏原口径；量比上限仍由 volume.ma5_cap 控制
    依据：全窗口因子研究 2292 条信号，两步合并后 T+5 胜率 53.2%->66.3%、
          均值 +1.21%->+5.87%；detect()/vol_score()/score() 判定口径零改动。

运行方式（由 common/run_all.py 按 registry.json 调度）：
    python3 low_shadow2/low_shadow2_screener.py --mode review
    python3 low_shadow2/low_shadow2_screener.py --mode review --top 10
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(BASE_DIR, "config.json"), encoding="utf-8"))
SH, POS, VOL, SC = CFG["shape"], CFG["position"], CFG["volume"], CFG["score"]
FLT = CFG.get("filter", {})   # 可选二次过滤层；旧版 config 缺该段时为空 dict，向后兼容
KLINE_COUNT = 160          # 共享K线缓存单只读取根数（低位判定需 >= 61）

# ----------------------------------------------------------------------------
# 数据入口（会被 adapter.install 强制替换为共同前置层版本；未就绪则用下方兜底）
# ----------------------------------------------------------------------------
def get_universe():
    """兜底样本池（生产环境由共同前置层接管，本函数不会被调用）。"""
    import akshare as ak
    df = ak.stock_zh_a_spot_em()
    df["代码"] = df["代码"].astype(str).str.zfill(6)
    out = []
    for _, r in df.iterrows():
        nm = str(r.get("名称", ""))
        if "ST" in nm.upper() or nm.startswith("*"):
            continue
        out.append({"symbol": r["代码"], "name": nm, "pct": float(r.get("涨跌幅") or 0)})
    return out


def kline(code, count=None):
    """兜底K线（生产环境由共同前置层接管）。"""
    import akshare as ak
    import time as _t
    for i in range(3):
        try:
            df = ak.stock_zh_a_hist(symbol=code, period="daily", start_date="20240101",
                                    end_date=_t.strftime("%Y%m%d"), adjust="qfq")
            df = df.rename(columns={"日期": "date", "开盘": "open", "收盘": "close",
                                    "最高": "high", "最低": "low", "成交量": "volume"})
            return df[["date", "open", "high", "low", "close", "volume"]].to_dict("records")
        except Exception:
            pass
    return None


def _to_df(bars, code):
    df = pd.DataFrame(bars).rename(columns={"volume": "vol"})
    df["代码"] = code
    return df


# ----------------------------------------------------------------------------
# 模型核心（与云端 model_core.py 一致，config 驱动）
# ----------------------------------------------------------------------------
def detect(df):
    df = df.rename(columns={"date": "date", "open": "open", "close": "close",
                            "high": "high", "low": "low", "vol": "vol"})
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    o, h, l, c, v = df["open"], df["high"], df["low"], df["close"], df["vol"]
    body = (o - c).abs(); upper = h - np.maximum(o, c)
    lower = np.minimum(o, c) - l; rng = h - l
    upper_pct = upper / c
    vol_ma5 = v.shift(1).rolling(VOL["ma5_window"]).mean()
    hh60 = h.rolling(POS["window"]).max().shift(1)
    ll60 = l.rolling(POS["window"]).min().shift(1)
    vol_prev = v.shift(1); vol_ratio_prev = v / vol_prev
    prec = c.shift(1)
    limit_down = (c / prec - 1) <= SH["limit_down_pct"]
    is_yin = c < o
    long_upper = ((upper >= SH["upper_body_min"] * body)
                  & (upper / rng >= SH["upper_range_min"])
                  & (lower / rng <= SH["lower_range_max"])
                  & (body > 0) & (upper_pct >= SH["upper_pct_min"]))
    den = (hh60 - ll60).where(lambda s: s > 0)
    pos_quant = (c - ll60) / den
    common = (is_yin & long_upper & (~limit_down) & (v > 0)
              & vol_ma5.notna() & hh60.notna() & vol_ratio_prev.notna())
    if POS["rule"] == "low_hh60_ratio":
        pos_ok = c <= POS["max_ratio"] * hh60
    else:
        pos_ok = c >= POS["min_ratio"] * hh60
    vol_ok = ((v / vol_ma5) <= VOL["ma5_cap"]) & (vol_ratio_prev >= VOL["prev_min"])
    sig = common & pos_ok & vol_ok
    # 前两日累计涨跌 = 信号日前1日收盘 / 前3日收盘 - 1（即信号之前两根K线的累计涨跌）。
    # 仅作为特征列输出给可选过滤层使用，不参与 sig 判定 —— 核心口径逐行不变。
    prior2_cum = c.shift(1) / c.shift(3).where(lambda s: s > 0) - 1
    out = pd.DataFrame({
        "date": df["date"], "open": o, "close": c, "high": h, "low": l,
        "pct": (c / prec - 1) * 100,
        "upper_body": upper / body, "upper_range": upper / rng,
        "lower_range": lower / rng, "vol_ratio": v / vol_ma5,
        "vol_ratio_prev": vol_ratio_prev, "pos60": c / hh60,
        "upper_pct": upper_pct, "prior2_cum": prior2_cum, "sig": sig})
    return out


def vol_score(vr, vrp):
    t = SC["vol_tiers"]
    if vr > t["cap"]: return t["cap_score"]
    if vr >= t["flat_micro_vr"] and vrp >= t["flat_micro_vrp"]: return t["flat_micro"]
    if vrp >= t["prev_up_vrp"]: return t["prev_up"]
    if t["flat_slight_vr_lo"] <= vr < 1.0: return t["flat_slight"]
    if vr < t["dry_vr"]: return t["dry"]
    return t["other"]


def score(row):
    s = min(row["upper_pct"], SC["upper_cap"]) / SC["upper_cap"] * SC["upper_weight"]
    s += vol_score(row["vol_ratio"], row["vol_ratio_prev"])
    s += min(abs(row["pos60"] - SC["position_target"]) / SC["position_span"], 1.0) * SC["position_weight"]
    return round(float(s), 1)


def pass_filter(row):
    """可选二次过滤层（config.filter 驱动）—— 独立于 detect() 核心信号。

    enabled=false 时恒通过，等价于分栏原口径，可一键回滚。
    prior2_cum_max : 前两日累计涨跌上限，0.0 表示剔除「前两日净涨」；
    vol_ratio_min  : 当日量/前5日均量下限，1.1 表示剔除缩量信号。
    研究依据：2026-11-15 全窗口因子研究（160 交易日 x 4570 只，2292 条信号，
    T+3/5/10 按全市场交易日历对齐，剔除窗口内疑似除权与长期停牌）。
    """
    if not FLT.get("enabled", False):
        return True
    cap = FLT.get("prior2_cum_max")
    if cap is not None:
        try:
            if not (np.isfinite(float(row["prior2_cum"])) and float(row["prior2_cum"]) <= cap):
                return False
        except (TypeError, ValueError, KeyError):
            return False
    lo = FLT.get("vol_ratio_min")
    if lo is not None:
        try:
            if not (np.isfinite(float(row["vol_ratio"])) and float(row["vol_ratio"]) >= lo):
                return False
        except (TypeError, ValueError, KeyError):
            return False
    return True


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="低位上影模型2 收盘后选股（合并流水线版）")
    ap.add_argument("--mode", default="review", choices=["review"],
                    help="review（回溯结算由 run_all.py 统一执行）")
    ap.add_argument("--top", type=int, default=10, help="写入统一推荐台账的前N只")
    args = ap.parse_args()

    # 共同前置层接入（强制）：样本池/K线入口切到共享层；未就绪则回退兜底并告警
    _ad = None
    try:
        _s = os.path.join(BASE_DIR, "..", "common")
        sys.path.insert(0, _s)
        import adapter as _ad
        _ad.install("low_shadow2", globals())
    except Exception as e:
        print("[WARN] 共同前置层未接入(%s)，使用兜底数据源" % e)
        _ad = None

    uni = get_universe()
    target = pd.Timestamp.now().normalize()
    picks, scanned, kmiss, filtered = [], 0, 0, 0
    for x in uni:
        code = str(x.get("symbol") or x.get("code") or "").zfill(6)
        nm = x.get("name", "")
        if "ST" in nm.upper() or nm.startswith("*"):
            continue
        bars = kline(code, count=KLINE_COUNT)
        if not bars or len(bars) < POS["window"] + 2:
            kmiss += 1
            continue
        scanned += 1
        df = _to_df(bars, code)
        d = detect(df)
        day = d[d["date"] == target]
        if len(day) == 0 or not bool(day["sig"].iloc[0]):
            continue
        r = day.iloc[0]
        # 可选二次过滤层：核心 sig 已命中后再做形态/量能过滤，判定逻辑不变
        if not pass_filter(r):
            filtered += 1
            continue
        picks.append({
            "code": code, "name": nm, "close": round(float(r["close"]), 2),
            "score": score(r), "board": "",
            "raw": {
                "pct_chg": round(float(r["pct"]), 2),
                "upper_pct": round(float(r["upper_pct"]) * 100, 2),
                "upper_range": round(float(r["upper_range"]) * 100, 1),
                "vol_ratio": round(float(r["vol_ratio"]), 2),
                "vol_ratio_prev": round(float(r["vol_ratio_prev"]), 2),
                "prior2_cum": round(float(r["prior2_cum"]) * 100, 2),
                "pos60": round(float(r["pos60"]) * 100, 1),
                "filter": "v20261115_hard" if FLT.get("enabled", False) else "off",
                "source": "low_shadow2",
            }})
    picks.sort(key=lambda p: p.get("score") or 0, reverse=True)

    if _ad:
        try:
            _ad.record("low_shadow2", picks[:args.top])
        except Exception as e:
            print("[WARN] 统一台账写入失败：%s" % e)
    else:
        print("[WARN] 无共同前置层，仅打印不写台账（独立调试模式）")

    print("==== 低位上影模型2 · %s · 样本池%d 有效K线%d K线缺失%d 核心信号%d 过滤剔除%d 候选%d (filter=%s) ===="
          % (target.strftime("%Y-%m-%d"), len(uni), scanned, kmiss,
             len(picks) + filtered, filtered, len(picks),
             "on" if FLT.get("enabled", False) else "off"))
    for i, p in enumerate(picks[:args.top], 1):
        print("%d. %s %s 收%s 评分%s 上影/股价%.2f%% 量比(较上日)%s 60日位置%.1f%%"
              % (i, p["code"], p["name"], p["close"], p["score"],
                 p["raw"]["upper_pct"], p["raw"]["vol_ratio_prev"], p["raw"]["pos60"]))
    if not picks:
        print("本日无候选。次日高开才确认买入，低开放弃。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
