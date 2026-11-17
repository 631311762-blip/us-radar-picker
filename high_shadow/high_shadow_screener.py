#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
高位上影线模型 · 合并流水线接入版  high_shadow_screener.py
============================================================
模型来源：由「低位上影线模型2(双栏) v2.3」拆出的「高位栏」——洗盘再起观察名单。
与 low_shadow2（低位栏）共用同一套 model_core 逻辑：shape / volume / score
的指标与阈值一律未改，仅位置判定分支由「低位」翻转为「高位」。

v3.0（用户口径 2026-11-17）方向翻正为【看涨·高位洗盘后再起】：
    这是正面观察名单，不是风险名单；上涨记为命中（win_rule=up）。
    四只标杆（光线/华策/通合/金逸）信号次日全部高开大涨，证实方向为看涨。

v4.0（用户口径 2026-11-17）方向对齐重构：
    废除看跌基因的第二条件 F（filter.enabled=false），量比上限 1.4→2.7
    以容纳放量洗盘确认。根因：F 的「前一日分位<60」+ 量比上限 1.4 是看跌时代约束，
    把四只正面标杆全部误杀（强势洗盘本在持续高位且需放量）。废除后四只全部自然入选，
    规则仍能每天泛化自动扫描全市场；位置双条件与强形态保留为核心信号。

v4.0 生效条件：
    形态条件  detect() 的长上影阴线（上影≥2倍实体、上影/全幅≥50%、下影/全幅≤25%、
              上影/收盘≥1.5%、阴线、非跌停）——核心质量信号，阈值未改；
    位置条件  c >= 0.88*hh60  且  当日分位 pos_quant=(c-ll60)/(hh60-ll60) >= 0.45
              （双条件，剔除世纪瑞尔这类比率压线但当日分位低的假高位，保住金逸）；
    量能条件  volume.volume_band = [0.85, 2.7]，vol_ratio_prev 与 vol_ratio 同时满足；
    第二条件  config.filter = F 已废除（enabled=false），pass_filter() 恒通过。
    （filter.enabled=true 可一键恢复旧 F 口径，不推荐。）
detect() 只负责输出特征列、sig 判定口径零改动；filter 实现为独立过滤层，与 low_shadow2 同构。

数据入口（符合 high_shadow/README.md 契约）：
  1. 样本池：UNIVERSE_JSON 指向 common/state/universe.json（已 ST + 立信剔除）；
  2. K 线：优先读共同前置层 common/state/klines/<code>.csv；
     本地无共享缓存时，回退到新浪财经日K接口（自带，可离线复跑）；
  3. 产出：common/state/recommendations/high_shadow/<date>.json（统一台账）
     + high_shadow/state/<date>_样本.json / .csv（完整候选，非仅 topN）。

运行：
  python3 high_shadow/high_shadow_screener.py --mode review
  python3 high_shadow/high_shadow_screener.py --mode review --date 2026-11-13 --backtest
"""
import argparse
import csv
import gzip
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

try:
    import requests
except ImportError:  # 无 requests 时推送降级为不可用，不影响选股主流程
    requests = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(BASE_DIR, ".."))
CFG = json.load(open(os.path.join(BASE_DIR, "config.json"), encoding="utf-8"))
SH, POS, VOL, SC = CFG["shape"], CFG["position"], CFG["volume"], CFG["score"]
FLT = CFG.get("filter", {})   # 第二条件（可选过滤层）；旧版 config 缺该段时为空 dict，向后兼容
PSH = CFG.get("push", {})     # 微信推送层（与 low_shadow2/config.push 同构）；缺该段时默认关闭
BH = CFG.get("bearish_hint", {})  # 偏空提醒层（v4.1）：量比超 v3.0 旧上限即标注，仅提示不影响入选
KLINE_COUNT = 70

UNIVERSE_JSON = os.environ.get("UNIVERSE_JSON", os.path.join(ROOT, "common", "state", "universe.json"))
KLINE_DIR = os.path.join(ROOT, "common", "state", "klines")
REC_DIR = os.path.join(ROOT, "common", "state", "recommendations", "high_shadow")
STATE_DIR = os.path.join(BASE_DIR, "state")
CACHE_PATH = os.path.join(STATE_DIR, "klines_cache.json.gz")

SINA_KLINE = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
              "CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={n}")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


# ----------------------------------------------------------------------------
# 数据入口
# ----------------------------------------------------------------------------
def sina_symbol(code):
    c = str(code).zfill(6)
    if c.startswith(("43", "83", "87", "88", "92")):
        return "bj" + c
    if c[0] == "6":
        return "sh" + c
    if c[0] in "0123":
        return "sz" + c
    return "sh" + c


def fetch_kline(code, retries=3):
    """新浪财经日K（当前交易日之前的 70 根），返回 [{day,open,high,low,close,volume}]。"""
    url = SINA_KLINE.format(sym=sina_symbol(code), n=KLINE_COUNT)
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            raw = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore")
            data = json.loads(raw)
            if isinstance(data, list) and data:
                out = []
                for b in data:
                    out.append({"date": b["day"], "open": float(b["open"]),
                                "high": float(b["high"]), "low": float(b["low"]),
                                "close": float(b["close"]), "volume": float(b["volume"])})
                return out
        except Exception:
            time.sleep(0.3 * (i + 1))
    return None


def shared_kline(code):
    """共同前置层共享缓存（若流水线已生成）。"""
    p = os.path.join(KLINE_DIR, "%s.csv" % str(code).zfill(6))
    if not os.path.exists(p):
        return None
    try:
        df = pd.read_csv(p)
        ren = {"日期": "date", "开盘": "open", "最高": "high", "最低": "low",
               "收盘": "close", "成交量": "volume", "open": "open", "high": "high",
               "low": "low", "close": "close", "volume": "volume", "vol": "volume"}
        df = df.rename(columns=ren)
        need = ["date", "open", "high", "low", "close", "volume"]
        if not all(k in df.columns for k in need):
            return None
        return df[need].to_dict("records")
    except Exception:
        return None


def load_cache():
    if os.path.exists(CACHE_PATH):
        try:
            with gzip.open(CACHE_PATH, "rt", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_cache(cache):
    os.makedirs(STATE_DIR, exist_ok=True)
    with gzip.open(CACHE_PATH, "wt", encoding="utf-8") as f:
        json.dump(cache, f)


def load_universe():
    with open(UNIVERSE_JSON, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("universe") or data.get("data") or []
    return [x for x in data if x.get("code")]


# ----------------------------------------------------------------------------
# 模型核心（与 low_shadow2/model_core.py 逐行一致，仅 config 驱动位置分支）
# ----------------------------------------------------------------------------
def detect(bars):
    df = pd.DataFrame(bars)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    o, h, l, c, v = df["open"], df["high"], df["low"], df["close"], df["volume"]
    body = (o - c).abs()
    upper = h - np.maximum(o, c)
    lower = np.minimum(o, c) - l
    rng = h - l
    upper_pct = upper / c
    vol_ma5 = v.shift(1).rolling(VOL["ma5_window"]).mean()
    hh60 = h.rolling(POS["window"]).max().shift(1)
    ll60 = l.rolling(POS["window"]).min().shift(1)
    vol_prev = v.shift(1)
    vol_ratio_prev = v / vol_prev
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
        # v3.0 位置双条件：比率下限 + 当日 60 日区间分位下限（同时满足）。
        # 单用比率对窄幅震荡股近似恒真，叠加当日分位可剔除"比率压线、实为区间中下部"的假高位。
        pos_ok = c >= POS["min_ratio"] * hh60
        if POS.get("pos_quant_enabled", False):
            pos_ok = pos_ok & (pos_quant >= float(POS["pos_quant_min"]))
    # 量比硬区间（v1.3 口径 0.85~1.4，具体数值一律以 config.volume.volume_band 为准）。
    # 同时约束 vol_ratio_prev（当日量/前日量）与 vol_ratio（当日量/前5日均量），
    # 由 config.volume.volume_band.apply_to 决定作用对象；未启用时回退到旧阈值。
    band = VOL.get("volume_band")
    if band and band.get("enabled"):
        lo, hi = float(band["min"]), float(band["max"])
        ratios = {"vol_ratio_prev": vol_ratio_prev, "vol_ratio": v / vol_ma5}
        vol_ok = pd.Series(True, index=df.index)
        for fld in band["apply_to"]:
            vol_ok &= (ratios[fld] >= lo) & (ratios[fld] <= hi)
    else:
        vol_ok = ((v / vol_ma5) <= VOL["ma5_cap"]) & (vol_ratio_prev >= VOL["prev_min"])
    sig = common & pos_ok & vol_ok
    # ---- 第二条件特征：只输出、不参与 sig（由 pass_filter 消费）----------------
    # 口径与 pre2day_sweep.features_of() 完全一致：
    #   hh60/ll60 = H/L[i-60:i] 的区间（含 T-1、不含信号日 T），
    #   正好等于上面 rolling(POS.window).shift(1) 在第 i 行的值；
    #   posq_prev 把 T-1 收盘放进这个区间算分位。
    den_prev = (hh60 - ll60).where(lambda s: s > 0)
    posq_prev = (c.shift(1) - ll60) / den_prev * 100
    yang2 = c.shift(2) > o.shift(2)
    yang1 = c.shift(1) > o.shift(1)
    pattern = pd.Series(
        [("阳" if a else "阴") + ("阳" if b else "阴") for a, b in zip(yang2, yang1)],
        index=df.index)
    ret2 = (c.shift(1) / c.shift(3).where(lambda s: s > 0) - 1) * 100
    d1 = (c.shift(1) / c.shift(2).where(lambda s: s > 0) - 1) * 100
    gap = (o / c.shift(1).where(lambda s: s > 0) - 1) * 100
    return pd.DataFrame({
        "date": df["date"], "open": o, "close": c, "high": h, "low": l,
        "pct": (c / prec - 1) * 100,
        "upper_pct": upper_pct, "upper_range": upper / rng,
        "lower_range": lower / rng,
        "vol_ratio": v / vol_ma5, "vol_ratio_prev": vol_ratio_prev,
        "pos60": c / hh60, "pos_quant": pos_quant, "sig": sig,
        "posq_prev": posq_prev, "pre2_pattern": pattern,
        "ret2": ret2, "d1": d1, "gap": gap})


def _post_json(url, payload, as_form=False, timeout=15):
    """极简 POST，返回 json dict；异常由调用方统一吞。"""
    if requests is None:
        return {}
    try:
        if as_form:
            r = requests.post(url, data=payload, timeout=timeout)
        else:
            r = requests.post(url, json=payload, timeout=timeout)
        return r.json()
    except Exception:
        return {}


def build_title(date_s, n):
    tpl = PSH.get("title_template", "高位上影线 · {date} · {n}只")
    return tpl.format(date=date_s, n=n)


def build_markdown(date_s, picks, stats=None, replay=False, top=20):
    """组装推送正文——只显示 代码 / 名称 / 评分 / 收盘 / 涨跌 五项。"""
    L = ["### 高位上影线 · %s · %d只" % (date_s, len(picks)), ""]
    n = len(picks)
    if n:
        L += ["| 代码 | 名称 | 评分 | 收盘 | 涨跌 | 偏空 |",
              "|---|---|---|---|---|---|"]
        for p in picks[:top]:
            r = p.get("raw") or {}
            hint = "⚠偏空" if r.get("bearish_hint") else ""
            L.append("| %s | %s | %s | %s | %s | %s |" % (
                p.get("code", ""), p.get("name", ""), p.get("score", ""),
                p.get("close", ""), r.get("pct_chg", "—"), hint))
        L.append("")
        if any((p.get("raw") or {}).get("bearish_hint") for p in picks[:top]):
            L.append("⚠偏空＝量比超 1.4 的放量票，警惕高位见顶，请重点关注。")
    else:
        L += ["今日空仓（0 只）。", ""]
    if replay:
        L.append("（补发）")
    L.append("模型信号，非投资建议。")
    return "\n".join(L)


def push_wechat(title, content, force=False):
    """推送到微信，返回 (ok, channel)。config.push 驱动，PushPlus 主 + Server酱兜底。"""
    if not force and not PSH.get("enabled", False):
        print("[push] 推送层未启用(config.push.enabled=false)，跳过")
        return False, "disabled"
    token = (os.environ.get("PUSHPLUS_TOKEN") or "").strip()
    sckey = (os.environ.get("SERVERCHAN_SENDKEY") or os.environ.get("FTQQ_KEY") or "").strip()
    template = PSH.get("template", "markdown")
    channel = (PSH.get("channel") or "pushplus").lower()
    fallback = bool(PSH.get("fallback_on_failure", True))
    order = ["pushplus", "serverchan"] if channel == "pushplus" else ["serverchan", "pushplus"]
    tried = []
    for ch in order:
        if ch == "pushplus":
            if not token:
                tried.append("pushplus(未配置)")
                continue
            j = _post_json("https://www.pushplus.plus/send", {
                "token": token, "title": title, "content": content, "template": template})
            ok = j.get("code") == 200
            print("[push] PushPlus code=%s msg=%s" % (j.get("code"), j.get("msg")))
            tried.append("pushplus(%s)" % ("OK" if ok else "code=%s" % j.get("code")))
            if ok:
                return True, "pushplus"
        else:
            if not sckey:
                tried.append("serverchan(未配置)")
                continue
            j = _post_json("https://sctapi.ftqq.com/%s.send" % sckey,
                           {"title": title, "desp": content}, as_form=True)
            ok = j.get("code") == 0
            print("[push] Server酱 code=%s" % j.get("code"))
            tried.append("serverchan(%s)" % ("OK" if ok else "code=%s" % j.get("code")))
            if ok:
                return True, "serverchan"
        if not fallback:
            break
    print("[push] 未送达：%s；正文已打印如下（不影响选股）" % "; ".join(tried))
    print("---- %s ----" % title)
    print(content)
    return False, ";".join(tried)


def push_report(date_s, picks, stats=None, replay=False, force=False, top=20):
    """主流程末尾调用：空仓是否通知由 config.push.empty_notice 控制。"""
    if not force and not PSH.get("enabled", False):
        return False, "disabled"
    if not picks and not PSH.get("empty_notice", True):
        print("[push] 空仓且 empty_notice=false，跳过")
        return False, "empty_skipped"
    return push_wechat(build_title(date_s, len(picks)),
                       build_markdown(date_s, picks, stats=stats, replay=replay, top=top),
                       force=force)


def pass_filter(row):
    """第二条件 F（config.filter 驱动）—— 独立于 detect() 核心信号。

    enabled=false 时恒通过，等价于 v1.3 纯第一条件口径，可一键回滚。
    F：20 <= posq_prev < 60  且  pre2_pattern 不在 exclude_patterns（默认「阴阳」）。
    研究依据见 config.filter.research / high_shadow/state/pre2day_conditions.json。
    """
    if not FLT.get("enabled", False):
        return True
    try:
        q = float(row["posq_prev"])
    except (TypeError, ValueError, KeyError):
        return False
    if not np.isfinite(q):
        return False
    lo = float(FLT["posq_prev_min"])
    hi = float(FLT["posq_prev_max"])
    hi_ok = q <= hi if FLT.get("posq_prev_max_inclusive", True) else q < hi
    if not (lo <= q and hi_ok):
        return False
    if str(row.get("pre2_pattern", "")) in FLT.get("exclude_patterns", []):
        return False
    return True


def bearish_hint_of(row):
    """偏空提醒（v4.1，config.bearish_hint 驱动）：入选票中量比超 v3.0 旧上限即标注。

    vol_ratio_prev 或 vol_ratio 任一 > threshold（默认 1.4）→ 返回提醒标签，否则返回空串。
    **只作风险提示列**，不参与 sig 判定、不影响入选与评分排序。
    enabled=false 或旧版 config 缺该段时恒返回空串，向后兼容。
    """
    if not BH.get("enabled", False):
        return ""
    try:
        th = float(BH.get("threshold", 1.4))
        fields = BH.get("fields", ["vol_ratio_prev", "vol_ratio"])
        hot = [f for f in fields if row.get(f) is not None
               and np.isfinite(float(row[f])) and float(row[f]) > th]
    except (TypeError, ValueError, KeyError):
        return ""
    if hot:
        vals = "/".join("%s=%.2f" % (f, float(row[f])) for f in hot)
        return "%s(%s)" % (BH.get("label", "偏空提醒·放量"), vals)
    return ""


def vol_score(vr, vrp):
    t = SC["vol_tiers"]
    if vr > t["cap"]:
        return t["cap_score"]
    if vr >= t["flat_micro_vr"] and vrp >= t["flat_micro_vrp"]:
        return t["flat_micro"]
    if vrp >= t["prev_up_vrp"]:
        return t["prev_up"]
    if t["flat_slight_vr_lo"] <= vr < 1.0:
        return t["flat_slight"]
    if vr < t["dry_vr"]:
        return t["dry"]
    return t["other"]


def score(row):
    s = min(row["upper_pct"], SC["upper_cap"]) / SC["upper_cap"] * SC["upper_weight"]
    s += vol_score(row["vol_ratio"], row["vol_ratio_prev"])
    s += min(abs(row["pos60"] - SC["position_target"]) / SC["position_span"], 1.0) * SC["position_weight"]
    return round(float(s), 1)


def raw_of(r):
    return {
        "pct_chg": round(float(r["pct"]), 2),
        "upper_pct": round(float(r["upper_pct"]) * 100, 2),
        "upper_range": round(float(r["upper_range"]) * 100, 1),
        "lower_range": round(float(r["lower_range"]) * 100, 1),
        "vol_ratio": round(float(r["vol_ratio"]), 2),
        "vol_ratio_prev": round(float(r["vol_ratio_prev"]), 2),
        "pos60": round(float(r["pos60"]) * 100, 1),
        "pos_quant": round(float(r["pos_quant"]) * 100, 1) if pd.notna(r["pos_quant"]) else None,
        "posq_prev": round(float(r["posq_prev"]), 1) if pd.notna(r["posq_prev"]) else None,
        "pre2_pattern": str(r["pre2_pattern"]),
        "ret2": round(float(r["ret2"]), 2) if pd.notna(r["ret2"]) else None,
        "d1": round(float(r["d1"]), 2) if pd.notna(r["d1"]) else None,
        "gap": round(float(r["gap"]), 2) if pd.notna(r["gap"]) else None,
        "second_condition": (FLT.get("rule", "") if FLT.get("enabled") else "off"),
        "bearish_hint": bearish_hint_of(r),
        "source": "high_shadow",
    }


# ----------------------------------------------------------------------------
# 轻量回溯（v3.0 看涨口径：后续上涨记为命中；v2.1 曾为看跌风险名单）
# ----------------------------------------------------------------------------
def run_backtest(cache, out_dir, date):
    rows = []
    for code, bars in cache.items():
        if not bars or len(bars) < POS["window"] + 7:
            continue
        try:
            d = detect(bars)
        except Exception:
            continue
        closes = d["close"].tolist()
        for i in range(len(d)):
            if not bool(d["sig"].iloc[i]) or i + 5 >= len(d):
                continue
            # 轻量回溯同样套用当前生效的第二条件，保证口径与当日名单一致
            if not pass_filter(d.iloc[i]):
                continue
            base = closes[i]
            if base <= 0:
                continue
            rows.append({
                "code": code,
                "date": d["date"].iloc[i].strftime("%Y-%m-%d"),
                "t1": (closes[i + 1] / base - 1) * 100,
                "t3": (closes[i + 3] / base - 1) * 100,
                "t5": (closes[i + 5] / base - 1) * 100,
            })
    summary = {"date": date, "signals": len(rows), "stocks": len(set(r["code"] for r in rows))}
    for k in ("t1", "t3", "t5"):
        arr = np.array([r[k] for r in rows], dtype=float)
        if len(arr):
            summary[k] = {"mean": round(float(arr.mean()), 3),
                          "median": round(float(np.median(arr)), 3),
                          "up_rate(命中率)": round(float((arr > 0).mean()) * 100, 2),
                          "_note": "v4.0 看涨口径：上涨记为命中"}
    with open(os.path.join(out_dir, "backtest_%s.json" % date.replace("-", "")), "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "signals": rows[:500]}, f, ensure_ascii=False, indent=2)
    return summary


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="高位上影线模型（洗盘再起观察名单·看涨）· 收盘后选股")
    ap.add_argument("--mode", default="review", choices=["review"])
    ap.add_argument("--date", default=None, help="目标交易日 YYYY-MM-DD，缺省取K线最新交易日")
    ap.add_argument("--top", type=int, default=20, help="写入统一推荐台账的前N只")
    ap.add_argument("--workers", type=int, default=20, help="并发抓取线程数")
    ap.add_argument("--refresh", action="store_true", help="忽略本地K线缓存重新抓取")
    ap.add_argument("--backtest", action="store_true", help="附带轻量回溯（看涨口径 T+1/3/5）")
    ap.add_argument("--no-push", action="store_true", help="本地复跑/回溯：本次不推送微信")
    ap.add_argument("--push-only", action="store_true", help="不重跑选股，只读取指定日期台账补发微信")
    args = ap.parse_args()

    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(REC_DIR, exist_ok=True)

    # ---- 补发模式：只读台账、只推送，不触碰前置层/选股/台账 ----
    if args.push_only:
        d = args.date or pd.Timestamp.now().strftime("%Y-%m-%d")
        path = os.path.join(REC_DIR, "%s.json" % d)
        if not os.path.exists(path):
            print("[push] 台账不存在：%s，无法补发" % path)
            return 1
        try:
            ledger = json.load(open(path, encoding="utf-8"))
        except Exception as e:
            print("[push] 台账读取失败：%s" % e)
            return 1
        lp = ledger.get("picks", [])
        st = {"universe": None, "core": ledger.get("count", len(lp))}
        ok, ch = push_report(d, lp, stats=st, replay=True, force=True, top=args.top)
        print("[push] 补发 %s：%s（channel=%s, %d 只）" % (
            d, "成功" if ok else "未送达", ch, len(lp)))
        return 0 if ok else 2

    uni = load_universe()
    cache = {} if args.refresh else load_cache()
    need = [str(x["code"]).zfill(6) for x in uni if str(x["code"]).zfill(6) not in cache]
    t0 = time.time()

    def grab(code):
        return code, shared_kline(code) or fetch_kline(code)

    if need:
        print("[1/3] 抓取K线：%d 只待取（共享缓存目录 %s）..."
              % (len(need), KLINE_DIR if os.path.isdir(KLINE_DIR) else "不存在，走新浪回退"))
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for code, bars in ex.map(grab, need):
                if bars:
                    cache[code] = bars
        save_cache(cache)
        print("      完成，耗时 %.1fs，有效K线 %d 只" % (time.time() - t0, len(cache)))
    else:
        print("[1/3] 命中本地K线缓存 %d 只" % len(cache))

    # 目标交易日
    if args.date:
        target = pd.Timestamp(args.date)
    else:
        latest = []
        for bars in cache.values():
            if bars:
                latest.append(bars[-1]["date"])
        target = pd.Timestamp(max(latest)) if latest else pd.Timestamp.now().normalize()

    uni_map = {str(x["code"]).zfill(6): x for x in uni}
    picks, scanned, kmiss, mismatch, filtered = [], 0, 0, 0, 0
    print("[2/3] 扫描 %s 的高位上影线信号 ..." % target.strftime("%Y-%m-%d"))
    for code, bars in cache.items():
        x = uni_map.get(code, {})
        nm = x.get("name", "")
        if "ST" in nm.upper() or nm.startswith("*"):
            continue
        if not bars or len(bars) < POS["window"] + 2:
            kmiss += 1
            continue
        # 数据源一致性校验：K线末收盘 vs 样本池收盘价
        if x.get("price") and target.strftime("%Y-%m-%d") == bars[-1]["date"]:
            if abs(bars[-1]["close"] - float(x["price"])) / max(float(x["price"]), 0.01) > 0.02:
                mismatch += 1
                continue
        scanned += 1
        try:
            d = detect(bars)
        except Exception:
            continue
        day = d[d["date"] == target]
        if len(day) == 0 or not bool(day["sig"].iloc[0]):
            continue
        r = day.iloc[0]
        # 第二条件 F：核心 sig 命中后再过一层独立过滤（enabled=false 时恒通过）
        if not pass_filter(r):
            filtered += 1
            continue
        raw = raw_of(r)
        picks.append({
            "code": code, "name": nm, "close": round(float(r["close"]), 2),
            "score": score(r), "board": x.get("board", ""),
            "price": x.get("price"), "pct": x.get("pct"),
            "turnover": x.get("turnover"), "vr": x.get("vr"),
            "mktcap": x.get("mktcap"),
            "raw": raw,
        })
    picks.sort(key=lambda p: p["score"], reverse=True)

    date_s = target.strftime("%Y-%m-%d")
    # 统一推荐台账（与 low_shadow2 等模型同格式）
    rec = {"date": date_s, "model": "high_shadow", "count": len(picks),
           "picks": picks[:args.top]}
    with open(os.path.join(REC_DIR, "%s.json" % date_s), "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)

    # 完整候选样本
    with open(os.path.join(STATE_DIR, "%s_样本.json" % date_s), "w", encoding="utf-8") as f:
        json.dump({"date": date_s, "model": "high_shadow", "column": "高位",
                   "direction": CFG["direction"], "win_rule": CFG["win_rule"],
                   "version": CFG["version"],
                   "count": len(picks), "core_signals": len(picks) + filtered,
                   "filtered_by_second_condition": filtered,
                   "first_condition": {"volume_band": [VOL["volume_band"]["min"],
                                                       VOL["volume_band"]["max"]],
                                       "apply_to": VOL["volume_band"]["apply_to"]},
                   "second_condition": {"enabled": bool(FLT.get("enabled")),
                                       "rule": FLT.get("rule", ""),
                                       "posq_prev": [FLT.get("posq_prev_min"),
                                                     FLT.get("posq_prev_max")],
                                       "exclude_patterns": FLT.get("exclude_patterns", [])},
                   "position_rule": POS["rule"], "min_ratio": POS["min_ratio"],
                   "pos_quant_min": POS.get("pos_quant_min") if POS.get("pos_quant_enabled") else None,
                   "picks": picks}, f, ensure_ascii=False, indent=2)
    csv_path = os.path.join(STATE_DIR, "%s_样本.csv" % date_s)
    cols = ["code", "name", "close", "pct", "turnover", "score", "board",
            "upper_pct", "upper_range", "lower_range", "vol_ratio", "vol_ratio_prev",
            "pos60", "pos_quant", "posq_prev", "pre2_pattern", "ret2", "d1", "gap",
            "bearish_hint"]
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for p in picks:
            r = p["raw"]
            w.writerow([p["code"], p["name"], p["close"], p.get("pct"), p.get("turnover"),
                        p["score"], p["board"], r["upper_pct"], r["upper_range"],
                        r["lower_range"], r["vol_ratio"], r["vol_ratio_prev"],
                        r["pos60"], r["pos_quant"], r["posq_prev"], r["pre2_pattern"],
                        r["ret2"], r["d1"], r["gap"], r.get("bearish_hint", "")])

    print("[3/3] ==== 高位上影线（洗盘再起·看涨）· %s · 样本池%d 有效K线%d K线缺失%d 价格校验剔除%d "
          "核心信号%d 第二条件剔除%d 最终候选%d ===="
          % (date_s, len(uni), scanned, kmiss, mismatch,
             len(picks) + filtered, filtered, len(picks)))
    for i, p in enumerate(picks[:args.top], 1):
        r = p["raw"]
        print("%2d. %s %s 收%.2f 评分%.1f 上影/股价%.2f%% 上影/全幅%.0f%% "
              "量比MA5 %.2f 60日位置%.1f%% 前一分位%.1f%% %s%s"
              % (i, p["code"], p["name"], p["close"], p["score"], r["upper_pct"],
                 r["upper_range"], r["vol_ratio"], r["pos60"],
                 r["posq_prev"] or 0, r["pre2_pattern"],
                 ("  ⚠"+r["bearish_hint"]) if r.get("bearish_hint") else ""))
    if not picks:
        print("本交易日无高位上影线候选。")

    # 微信推送：config.push 驱动；--no-push 跳过。推送失败不影响已落盘台账。
    if not args.no_push:
        push_report(date_s, picks,
                    stats={"universe": len(uni), "scanned": scanned, "core": len(picks) + filtered,
                           "filtered": filtered},
                    top=args.top)

    if args.backtest:
        s = run_backtest(cache, STATE_DIR, date_s)
        print("[回溯] 区间信号 %d 个 / %d 只，T+1均值%.2f%% 上涨命中%.2f%%，T+5均值%.2f%% 上涨命中%.2f%%"
              % (s["signals"], s["stocks"], s.get("t1", {}).get("mean", 0),
                 s.get("t1", {}).get("up_rate(命中率)", 0),
                 s.get("t5", {}).get("mean", 0), s.get("t5", {}).get("up_rate(命中率)", 0)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
