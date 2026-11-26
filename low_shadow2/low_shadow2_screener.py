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

微信推送层（2026-11-16，config.push 驱动）：独立于选股主流程，
    - 主通道 PushPlus(PUSHPLUS_TOKEN)，失败/未配置时兜底 Server酱(SERVERCHAN_SENDKEY)
    - 空仓也推一条状态通知（避免「没消息=没跑」误判）；异常全吞，绝不影响选股/台账
    - enabled=false 或 --no-push 一键静默；--push-only --date 可只补发台账不重跑
    本模型原为仓库 8 个模型中唯一无推送通道者（只写台账），故补齐。

运行方式（由 common/run_all.py 按 registry.json 调度）：
    python3 low_shadow2/low_shadow2_screener.py --mode review
    python3 low_shadow2/low_shadow2_screener.py --mode review --top 10
    python3 low_shadow2/low_shadow2_screener.py --mode review --no-push   # 本地复跑/回溯
    python3 low_shadow2/low_shadow2_screener.py --push-only --date 2026-11-16  # 补发台账
"""
import argparse
import json
import os
import sys

import signal
import time as _time
import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(BASE_DIR, "config.json"), encoding="utf-8"))
SH, POS, VOL, SC = CFG["shape"], CFG["position"], CFG["volume"], CFG["score"]
FLT = CFG.get("filter", {})   # 可选二次过滤层；旧版 config 缺该段时为空 dict，向后兼容
PDV = CFG.get("pre_divergence", {})  # 可选「前置下跌+量能背离」层；缺省即关闭
PSH = CFG.get("push", {})     # 可选微信推送层；旧版 config 缺该段时为空 dict，自动静默
KLINE_COUNT = 160          # 共享K线缓存单只读取根数（低位判定需 >= 61）
RECO_DIR = os.path.join(BASE_DIR, "..", "common", "state", "recommendations", "low_shadow2")

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


# ----------------------------------------------------------------------------
# 逐只取数看门狗（2026-11-23）：防止单只行情源瞬时卡死拖垮整条流水线。
# 根因：兜底 akshare / 共享缓存读取偶发无响应，旧版逐只调用无超时，
#       任一只卡住即让整个模型挂起（11-20、11-23 两次事故的元凶）。
# 方案：每只 kline() 调用套 SIGALRM 硬超时；超时即跳过该只并计数，
#       保证流水线在有限时间内走完、正常推送。仅 Unix 主线程生效，
#       非 Unix/非主线程自动降级为无超时（功能不受影响）。
# ----------------------------------------------------------------------------
SYMBOL_TIMEOUT = float(os.environ.get("LS2_SYMBOL_TIMEOUT", "20"))  # 单只取数硬超时(秒)
UNIVERSE_TIMEOUT = float(os.environ.get("LS2_UNIVERSE_TIMEOUT", "90"))  # 样本池取数硬超时(秒)
LOOP_BUDGET = float(os.environ.get("LS2_LOOP_BUDGET", "300"))  # 选股循环总预算(秒)，到点强制收尾并推送
_watchdog_on = hasattr(signal, "SIGALRM") and hasattr(signal, "setitimer")

class _KlineTimeout(Exception):
    pass

def _alarm_raise(signum, frame):
    raise _KlineTimeout()

def _guarded(fn, timeout, *a, **k):
    """通用看门狗：在 timeout 秒内执行 fn(*a,**k)，超时抛 _KlineTimeout。"""
    if not _watchdog_on:
        return fn(*a, **k)
    old = signal.signal(signal.SIGALRM, _alarm_raise)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        return fn(*a, **k)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)

def kline_guarded(code, count=None):
    """带硬超时的单只 K 线读取；超时抛 _KlineTimeout 由主循环跳过。"""
    return _guarded(kline, SYMBOL_TIMEOUT, code, count=count)


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
    # 前置特征（全部只用信号日之前的数据，无前视）：供 pre_divergence 过滤/加分层使用
    _dw = int(PDV.get("decline_window", 10)); _vw = int(PDV.get("vol_window", 5))
    _cp = c.shift(1)
    pre_decline = _cp / c.shift(_dw + 1).where(lambda s: s > 0) - 1
    _v_roll = v.shift(1).rolling(_vw).mean()
    pre_vol_trend = _v_roll / _v_roll.shift(_vw).where(lambda s: s > 0)
    out = pd.DataFrame({
        "date": df["date"], "open": o, "close": c, "high": h, "low": l,
        "pct": (c / prec - 1) * 100,
        "upper_body": upper / body, "upper_range": upper / rng,
        "lower_range": lower / rng, "vol_ratio": v / vol_ma5,
        "vol_ratio_prev": vol_ratio_prev, "pos60": c / hh60,
        "upper_pct": upper_pct, "prior2_cum": prior2_cum,
        "pre_decline": pre_decline, "pre_vol_trend": pre_vol_trend, "sig": sig})
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
    # 前置下跌+缩量加分：跌得越深、量能萎缩越明显，加分越多（0~bonus_weight）
    if PDV.get("enabled", False) and PDV.get("bonus_weight"):
        try:
            d = float(row["pre_decline"]); vt = float(row["pre_vol_trend"])
            depth = min(max((-d + float(PDV.get("decline_max", -0.08))) / 0.12, 0.0), 1.0)
            shrink = min(max((float(PDV.get("vol_trend_max", 0.85)) - vt) / 0.35, 0.0), 1.0)
            if np.isfinite(depth) and np.isfinite(shrink):
                s += (0.6 * depth + 0.4 * shrink) * float(PDV["bonus_weight"])
        except (TypeError, ValueError, KeyError):
            pass
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
    # 前置下跌+量能背离层（config.pre_divergence 驱动）：只认信号日之前的数据
    if PDV.get("enabled", False):
        try:
            d = float(row["pre_decline"]); vt = float(row["pre_vol_trend"])
            if not (np.isfinite(d) and d <= float(PDV.get("decline_max", -0.08))):
                return False
            if not (np.isfinite(vt) and vt < float(PDV.get("vol_trend_max", 0.85))):
                return False
        except (TypeError, ValueError, KeyError):
            return False
    return True


# ----------------------------------------------------------------------------
# 微信推送层（2026-11-16，config.push 驱动）—— 独立于选股核心，异常全吞
# ----------------------------------------------------------------------------


# ----------------------------------------------------------------------------
# 前十日样本跟踪（2026-11-26）：历史推荐股按提示日价跟到最新收盘。
#   规则：最新收盘价 < 提示日收盘价 → 判定破位，结束跟踪（不再展示）。
#   展示：仅列仍在上方的活跃样本，按提示日倒序；涨幅=最新/提示日价-1。
# ----------------------------------------------------------------------------
TRACK = CFG.get("track", {})   # 可选；缺省用下方默认

def _track_days():
    return int(TRACK.get("lookback_days", 10))

def build_tracking(today=None, kline_fn=None):
    """扫描近 N 个交易日台账，跟踪历史样本，返回 markdown 行列表。"""
    import glob as _glob
    import datetime as _dt
    today = today or pd.Timestamp.now().strftime("%Y-%m-%d")
    kfn = kline_fn or kline
    files = sorted(_glob.glob(os.path.join(RECO_DIR, "*.json")))
    files = [f for f in files if os.path.basename(f)[:10] < today]
    files = files[-_track_days():]      # 最近 N 个批次(按台账日期)
    if not files:
        return []

    # 汇总历史事件 (code -> {name, events:[(date, base)]})
    hist = {}
    for f in files:
        d = os.path.basename(f)[:10]
        try:
            rec = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        for p in rec.get("picks", []):
            c = str(p.get("code", "")).zfill(6)
            base = p.get("close")
            if not c or not base or float(base) <= 0:
                continue
            e = hist.setdefault(c, {"name": p.get("name", ""), "events": []})
            e["events"].append((d, float(base)))
            if not e["name"]:
                e["name"] = p.get("name", "")
    if not hist:
        return []

    rows = []
    for code, info in hist.items():
        # 取该股最近一次提示作为跟踪基准(避免同股多次提示重复)
        d0, base = max(info["events"], key=lambda x: x[0])
        try:
            bars = kfn(code, count=120)
        except Exception:
            bars = None
        if not bars:
            continue
        # 最新收盘价(取日期<=today的最后一根，避免盘中未收盘数据)
        valid = [b for b in bars if str(b.get("date", ""))[:10] <= today]
        if not valid:
            continue
        last = max(valid, key=lambda b: str(b.get("date", ""))[:10])
        cur = float(last.get("close") or 0)
        if cur <= 0:
            continue
        chg = (cur / base - 1.0) * 100
        if cur < base:      # 破位 → 结束跟踪
            continue
        held = (pd.Timestamp(last["date"][:10]) - pd.Timestamp(d0)).days
        rows.append({"code": code, "name": info["name"], "date": d0,
                     "base": base, "cur": round(cur, 2),
                     "chg": round(chg, 2), "days": held})

    rows.sort(key=lambda r: r["date"], reverse=True)
    L = ["## 前十日样本跟踪（未破位）", ""]
    if not rows:
        L += ["> 近 %d 个交易日推荐样本均已跌破提示日收盘价，全部结束跟踪（无存活样本）。" % _track_days(), ""]
        return L
    L += ["> 规则：以提示日收盘价为成本线，最新收盘价跌破即结束跟踪；下表仅列仍在上方的 %d 只。" % len(rows), ""]
    L += ["| 提示日 | 代码 | 名称 | 提示价 | 现价 | 累计涨跌 | 跟踪天数 |",
          "|---|---|---|---|---|---|---|"]
    for r in rows:
        sign = "+" if r["chg"] > 0 else ""
        L.append("| %s | %s | %s | %s | %s | %s%.2f%% | %d天 |" % (
            r["date"], r["code"], r["name"], r["base"], r["cur"], sign, r["chg"], r["days"]))
    L.append("")
    return L


def build_title(date, n):
    if n == 0:
        return "低位上影线二 · %s · %s" % (date, PSH.get("empty_text") or "五谷丰登")
    tpl = PSH.get("title_template") or "低位上影线二 · {date} · {n}只"
    return tpl.format(date=date, n=n)


def build_markdown(date, picks, stats=None, replay=False):
    """构造推送正文（markdown）。

    picks 可来自内存完整结果（raw 含量比/上影等因子），也可来自台账
    （kit.record 会把嵌套 raw 裁成标量，因子字段可能缺失）——两种都兼容。
    """
    stats = stats or {}
    L = ["## 低位上影线二 · %s" % date, ""]
    if replay:
        L += ["> 本条为**补发**（当日流水线未接推送通道，现补齐后重发台账），非实时盘中提示。", ""]
    n = len(picks)
    if n:
        L.append("**新口径候选 %d 只**" % n)
        L.append("")
        has_factor = any(k in (picks[0].get("raw") or {})
                         for k in ("upper_pct", "vol_ratio", "prior2_cum", "pos60"))
        if has_factor:
            L.append("| # | 代码 | 名称 | 收盘 | 评分 | 上影/股价 | 量比 | 前两日累计 | 60日位置 |")
            L.append("|---|---|---|---|---|---|---|---|---|")
        else:
            L.append("| # | 代码 | 名称 | 收盘 | 评分 |")
            L.append("|---|---|---|---|---|")
        for i, p in enumerate(picks, 1):
            raw = p.get("raw") or {}
            if has_factor:
                L.append("| %d | %s | %s | %s | %s | %s%% | %s | %s%% | %s%% |" % (
                    i, p.get("code", ""), p.get("name", ""), p.get("close", ""), p.get("score", ""),
                    raw.get("upper_pct", "—"), raw.get("vol_ratio", "—"),
                    raw.get("prior2_cum", "—"), raw.get("pos60", "—")))
            else:
                L.append("| %d | %s | %s | %s | %s |" % (
                    i, p.get("code", ""), p.get("name", ""), p.get("close", ""), p.get("score", "")))
        L.append("")
    else:
        L += ["**%s**" % (PSH.get("empty_text") or "五谷丰登"), "",
              "今日无符合条件标的（空仓），属阈值预期结果，**不是漏跑或数据源故障**。", ""]

    # 运行口径与统计
    detail = ["- 口径：前两日累计涨跌 ≤ 0 且 当日量/前5日均量 ≥ 1.1（量比上限 2.2 不变）",
              "- 过滤层：`%s`" % ("on" if FLT.get("enabled", False) else "off")]
    if PDV.get("enabled", False):
        detail.append("- 前置背离层：前%d日跌幅 ≤ %.0f%% 且 前%d日量能趋势 < %.2f（已启用）"
                      % (PDV.get("decline_window", 10), PDV.get("decline_max", -0.08) * 100,
                         PDV.get("vol_window", 5), PDV.get("vol_trend_max", 0.85)))
    if stats:
        if stats.get("universe"):
            detail.append("- 样本池 %s 只 / 有效K线 %s" % (stats.get("universe"), stats.get("scanned", "—")))
        if stats.get("core") is not None:
            detail.append("- 核心信号 %s → 过滤剔除 %s → 候选 %s" % (
                stats.get("core"), stats.get("filtered", "—"), stats.get("picks", n)))
    detail += ["",
               "**确认规则**：次日高开才买入，低开放弃；T+10 结算，止损 -5% / 止盈 +20%。",
               "",
               "📌 模型原型：本模型源自低位上影线模型2(双栏 v2.3)拆分的低位栏，提示日为回踩企稳阴线当日，基本特征：低位长上影阴线+收盘处60日区间低位+缩量企稳，次日高开确认买入得出（样本股如东山精密、欧陆通）。",
               "📈 最优策略：提示日次日开盘买入，持有至第10天(D+11)最高价卖出（2026年回溯胜率64%、平均收益+6.0%，按盘中最高价理想成交）；跌破买入价-5%止损（止损用于控制回撤，回溯显示会略降理想收益）。",
               "",
               "数据来源：共同前置层（东财/新浪日线兜底）。本清单为模型信号，不构成投资建议。"]
    L += detail
    L.append("")
    # 前十日样本跟踪（config.track.enabled 可一键关闭）
    if TRACK.get("enabled", False):
        try:
            L += build_tracking(date)
        except Exception as _te:
            print("[WARN] 前十日样本跟踪异常(已忽略)：%s" % _te)
    return "\n".join(L)


def _post_json(url, payload, as_form=False, timeout=15):
    """极简 POST，返回 (ok, detail)。不引入新依赖，异常由调用方统一吞。"""
    import requests
    if as_form:
        r = requests.post(url, data=payload, timeout=timeout)
    else:
        r = requests.post(url, json=payload, timeout=timeout)
    try:
        j = r.json()
    except Exception:
        j = {}
    return j


def push_wechat(title, content, force=False):
    """把正文推送到微信。返回 (ok, channel)。

    主通道由 config.push.channel 指定（默认 PushPlus），失败/未配置且
    fallback_on_failure=true 时兜底 Server酱；两者都失败只打印、不抛异常。
    """
    if not force and not PSH.get("enabled", False):
        print("[push] 推送层未启用(config.push.enabled=false)，跳过")
        return False, "disabled"
    token = (os.environ.get("PUSHPLUS_TOKEN") or "").strip()
    sckey = (os.environ.get("SERVERCHAN_SENDKEY") or os.environ.get("FTQQ_KEY") or "").strip()
    template = PSH.get("template", "markdown")
    try:                       # 临近财报/预告：微信推送加醒目标识（离线读本地日历）
        import sys as _sys
        _sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))
        import report_risk as _rr
        title, content = _rr.decorate(title, content, template)
    except Exception:
        pass
    channel = (PSH.get("channel") or "pushplus").lower()
    fallback = bool(PSH.get("fallback_on_failure", True))
    order = ["pushplus", "serverchan"] if channel == "pushplus" else ["serverchan", "pushplus"]
    tried = []
    for ch in order:
        if ch == "pushplus":
            if not token:
                tried.append("pushplus(未配置)")
                continue
            try:
                j = _post_json("https://www.pushplus.plus/send", {
                    "token": token, "title": title, "content": content, "template": template})
                ok = j.get("code") == 200
                print("[push] PushPlus code=%s msg=%s" % (j.get("code"), j.get("msg")))
                tried.append("pushplus(%s)" % ("OK" if ok else "code=%s" % j.get("code")))
                if ok:
                    return True, "pushplus"
            except Exception as e:
                print("[push] PushPlus 异常: %s" % e)
                tried.append("pushplus(异常)")
        else:
            if not sckey:
                tried.append("serverchan(未配置)")
                continue
            try:
                j = _post_json("https://sctapi.ftqq.com/%s.send" % sckey,
                               {"title": title, "desp": content}, as_form=True)
                ok = j.get("code") == 0
                print("[push] Server酱 code=%s" % j.get("code"))
                tried.append("serverchan(%s)" % ("OK" if ok else "code=%s" % j.get("code")))
                if ok:
                    return True, "serverchan"
            except Exception as e:
                print("[push] Server酱 异常: %s" % e)
                tried.append("serverchan(异常)")
        if not fallback:
            break
    print("[push] 未送达：%s；正文已打印如下（不影响选股）" % "; ".join(tried))
    print("---- %s ----" % title)
    print(content)
    return False, ";".join(tried)


def push_report(date, picks, stats=None, replay=False, force=False):
    """主流程末尾调用：空仓是否通知由 config.push.empty_notice 控制。"""
    if not force and not PSH.get("enabled", False):
        return False, "disabled"
    if not picks and not PSH.get("empty_notice", True):
        print("[push] 空仓且 empty_notice=false，跳过")
        return False, "empty_skipped"
    title = build_title(date, len(picks))
    content = build_markdown(date, picks, stats=stats, replay=replay)
    return push_wechat(title, content, force=force)


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="低位上影模型2 收盘后选股（合并流水线版）")
    ap.add_argument("--mode", default="review", choices=["review"],
                    help="review（回溯结算由 run_all.py 统一执行）")
    ap.add_argument("--top", type=int, default=10, help="写入统一推荐台账的前N只")
    ap.add_argument("--no-push", action="store_true",
                    help="本次不推送微信（本地复跑/回溯用，不影响选股与台账）")
    ap.add_argument("--push-only", action="store_true",
                    help="不重跑选股，只读取指定日期台账并补发微信")
    ap.add_argument("--date", default=None,
                    help="配合 --push-only：台账日期 YYYY-MM-DD（默认取当日）")
    args = ap.parse_args()

    # ---- 补发模式：只读台账、只推送，不触碰前置层/选股/台账 ----
    if args.push_only:
        d = args.date or pd.Timestamp.now().strftime("%Y-%m-%d")
        path = os.path.join(RECO_DIR, "%s.json" % d)
        if not os.path.exists(path):
            print("[push] 台账不存在：%s，无法补发" % path)
            return 1
        try:
            ledger = json.load(open(path, encoding="utf-8"))
        except Exception as e:
            print("[push] 台账读取失败：%s" % e)
            return 1
        lp = ledger.get("picks", [])
        st = {"picks": ledger.get("count", len(lp))}
        ok, ch = push_report(d, lp, stats=st, replay=True)
        print("[push] 补发 %s：%s（channel=%s, %d 只）" % (d, "成功" if ok else "未送达", ch, len(lp)))
        return 0 if ok else 2

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

    try:
        uni = _guarded(get_universe, UNIVERSE_TIMEOUT)
    except _KlineTimeout:
        print("[FATAL] 样本池取数超时(>%ss)，数据源异常，终止本轮避免空仓误报。" % UNIVERSE_TIMEOUT)
        return 2
    except Exception as e:
        print("[FATAL] 样本池取数失败：%s，终止本轮。" % e)
        return 2
    target = pd.Timestamp.now().normalize()
    picks, scanned, kmiss, filtered, timeout_skip = [], 0, 0, 0, 0
    loop_start = _time.time()
    for x in uni:
        code = str(x.get("symbol") or x.get("code") or "").zfill(6)
        nm = x.get("name", "")
        if "ST" in nm.upper() or nm.startswith("*"):
            continue
        try:
            bars = kline_guarded(code, count=KLINE_COUNT)
        except _KlineTimeout:
            timeout_skip += 1
            continue
        except Exception:
            kmiss += 1
            continue
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
                "pre_decline": round(float(r["pre_decline"]) * 100, 2),
                "pre_vol_trend": round(float(r["pre_vol_trend"]), 2),
                "filter": ("v20261115_hard" if FLT.get("enabled", False) else "off")
                          + ("+pre_div" if PDV.get("enabled", False) else ""),
                "source": "low_shadow2",
            }})
        # 总预算保护：无论卡在哪只，到点强制收尾，保证推送层一定执行
        if scanned + kmiss + timeout_skip > 0 and _time.time() - loop_start > LOOP_BUDGET:
            print("[WARN] 选股循环达总预算 %ss，强制收尾（已扫%d只），继续推送。" % (LOOP_BUDGET, scanned))
            break
    picks.sort(key=lambda p: p.get("score") or 0, reverse=True)

    if _ad:
        try:
            _ad.record("low_shadow2", picks[:args.top])
        except Exception as e:
            print("[WARN] 统一台账写入失败：%s" % e)
    else:
        print("[WARN] 无共同前置层，仅打印不写台账（独立调试模式）")

    print("==== 低位上影模型2 · %s · 样本池%d 有效K线%d K线缺失%d 取数超时跳过%d 核心信号%d 过滤剔除%d 候选%d (filter=%s) ===="
          % (target.strftime("%Y-%m-%d"), len(uni), scanned, kmiss, timeout_skip,
             len(picks) + filtered, filtered, len(picks),
             "on" if FLT.get("enabled", False) else "off"))
    if timeout_skip > 0:
        print("[WARN] 本日有 %d 只取数超时被跳过(单只上限%ss)；若占比过高通常是行情源瞬时异常，请复核样本池覆盖率。" % (timeout_skip, SYMBOL_TIMEOUT))
    for i, p in enumerate(picks[:args.top], 1):
        print("%d. %s %s 收%s 评分%s 上影/股价%.2f%% 量比(较上日)%s 60日位置%.1f%%"
              % (i, p["code"], p["name"], p["close"], p["score"],
                 p["raw"]["upper_pct"], p["raw"]["vol_ratio_prev"], p["raw"]["pos60"]))
    if not picks:
        print("本日无候选。次日高开才确认买入，低开放弃。")

    # ---- 微信推送层（独立附加，异常全吞，绝不影响选股/台账结果）----
    if not args.no_push:
        stats = {"universe": len(uni), "scanned": scanned, "core": len(picks) + filtered,
                 "filtered": filtered, "picks": len(picks), "timeout_skip": timeout_skip}
        try:
            ok, ch = push_report(target.strftime("%Y-%m-%d"), picks[:args.top], stats=stats)
            print("[push] %s channel=%s" % ("成功" if ok else "未送达/已跳过", ch))
        except Exception as e:
            print("[push] 推送层异常（已忽略，不影响选股）：%s" % e)
    else:
        print("[push] --no-push：本次跳过微信推送")
    return 0


if __name__ == "__main__":
    sys.exit(main())
