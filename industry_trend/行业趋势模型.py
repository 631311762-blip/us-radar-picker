# -*- coding: utf-8 -*-
"""
行业趋势模型（周线共振版 · 2026-11-27）
=========================================
定位：行业层面的"周度共振判断 + 个股二次确认"过滤器，不独立选股。

两大功能：
  【A】行业周线共振分析（每周更新）
       - 对 33 个主流行业主题的代表 ETF 做周线分析：
         周线向上（本周收盘>上周 且 周MA5>周MA10）+ 周成交量放大（>近5周均量×1.2）
         → 判定为【强共振行业】（周线向上=趋势，放量=资金流入）。
       - 结合近 1 个月（约20交易日）涨幅作为"历史月份行业表现"辅助因子。
       - 输出强共振行业清单 + 对应 ETF。

  【B】三模型选股按强行业过滤（每周推送强调）
       - 读取破位/奥雅/怡达最近选股台账，按行业成分映射归类；
         落在强共振行业的个股标记【行业共振·重点】，在推送中强调显示。

  【C】个股二次确认（保留原接口）
       - confirm/batch_confirm/filter_focus：对单只个股做金叉+MA20拐头确认。

数据源：
  - ETF 周线：akshare fund_etf_hist_em(adjust='qfq') 抓日线 → 聚合周线。
    （ETF 暂不在共享行情表；如需纳入，运行 backfill_etf_to_store.py）
  - 行业成分映射：industry_trend/sector_members.json（akshare 东财行业板块，本地缓存）
  - 代表 ETF 清单：industry_trend/etf_universe.json
  - 个股确认：共享行情表 common/state/market_daily（qfq，只读本地）

调用方式：
  from 行业趋势模型 import weekly_resonance_all, build_weekly_push
  resonance = weekly_resonance_all()                 # 全部行业周线共振分析
  text = build_weekly_push(resonance, model_picks)   # 生成周度推送文本

依赖：akshare / pandas / numpy
"""

import os
import json
import sys
import time
import warnings
from datetime import datetime

import pandas as pd
import numpy as np

warnings.filterwarnings("ignore")

# ============ 参数（可调整）============
WEEK_UP_MA       = 5       # 周线均线：MA5 > MA10 判多头
VOL_SURGE_RATIO  = 1.2     # 周放量阈值：本周量 > 近5周均量 × 1.2
VOL_LOOKBACK     = 5       # 周量能回看周数
MONTH_DAYS       = 20      # 近1个月表现（交易日）
GOLDEN_MAX_AGE   = 5       # 个股金叉有效天数
WINDOW_DAYS      = 5       # 个股确认窗口
MA20_LOOKBACK    = 3       # 个股MA20拐头回看

HERE = os.path.dirname(os.path.abspath(__file__))
ETF_UNIVERSE_FILE = os.path.join(HERE, "etf_universe.json")
SECTOR_MEMBERS_FILE = os.path.join(HERE, "sector_members.json")
ETF_DAILY_FILE = os.path.join(HERE, "etf_daily_qfq.json.gz")   # 本地缓存（qfq日线，gzip，每周刷新）
STATE_DIR = os.path.join(HERE, "state")                        # 周度状态落盘目录
STRONG_FILE = os.path.join(STATE_DIR, "strong_codes.json")     # 强共振行业成分股清单（供每日推送读取）
_ETF_CACHE = None   # 惰性加载：{code: {theme,name,bars:[{d,o,h,l,c,v}]}}


# ============ 数据加载 ============
def _load_json(path, default=None):
    try:
        if path.endswith(".gz"):
            import gzip
            with gzip.open(path, "rt", encoding="utf-8") as f:
                return json.load(f)
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _dump_json(path, obj):
    """按扩展名决定明文/gzip 写入。"""
    if path.endswith(".gz"):
        import gzip
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    else:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def load_etf_universe():
    return _load_json(ETF_UNIVERSE_FILE, []) or []


def load_sector_members():
    return _load_json(SECTOR_MEMBERS_FILE, {}) or {}


def load_etf_cache(force=False):
    """惰性加载本地 ETF 日线缓存（优先，避免每次联网现抓）。"""
    global _ETF_CACHE
    if _ETF_CACHE is None or force:
        _ETF_CACHE = _load_json(ETF_DAILY_FILE, {}) or {}
    return _ETF_CACHE


def _bars_to_frame(bars):
    """把缓存里的紧凑 bar [{d,o,h,l,c,v}] 转成模型用的中文列 DataFrame。"""
    if not bars:
        return None
    df = pd.DataFrame(bars).rename(columns={
        "d": "日期", "o": "开盘", "h": "最高", "l": "最低", "c": "收盘", "v": "成交量"})
    df["日期"] = pd.to_datetime(df["日期"])
    for col in ("收盘", "开盘", "最高", "最低", "成交量"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.sort_values("日期").reset_index(drop=True)


# ============ 【A】行业周线共振分析 ============
def _fetch_etf_daily(code, end_date=None, days=300):
    """取 ETF 前复权日线：优先读本地缓存（qfq，每周刷新），未命中才 akshare 现抓。"""
    code = str(code).zfill(6)
    # 1) 本地缓存优先
    cache = load_etf_cache()
    node = cache.get(code)
    if node and node.get("bars"):
        df = _bars_to_frame(node["bars"])
        if df is not None and len(df) >= 30:
            if end_date:
                df = df[df["日期"] <= pd.to_datetime(end_date)]
            return df.reset_index(drop=True) if len(df) >= 30 else None
    # 2) 回退：akshare 现抓
    import akshare as ak
    end = (end_date or datetime.now().strftime("%Y%m%d")).replace("-", "")
    d_obj = pd.to_datetime(end)
    start = (d_obj - pd.Timedelta(days=days * 2)).strftime("%Y%m%d")
    for attempt in range(3):
        try:
            df = ak.fund_etf_hist_em(symbol=code, period="daily",
                                     start_date=start, end_date=end, adjust="qfq")
            if df is not None and len(df) >= 30:
                df["日期"] = pd.to_datetime(df["日期"])
                for col in ("收盘", "开盘", "最高", "最低", "成交量"):
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                return df.sort_values("日期").reset_index(drop=True)
        except Exception:
            time.sleep(1.0)
    return None


def weekly_resonance(etf_code, theme="", board="", end_date=None):
    """对单只行业 ETF 做周线共振判定。返回 dict。"""
    df = _fetch_etf_daily(etf_code, end_date)
    empty = {
        "theme": theme, "etf_code": etf_code, "etf_name": "", "board": board,
        "等级": "数据异常", "周线向上": False, "周放量": False, "强共振": False,
        "本周收盘": 0, "上周收盘": 0, "周MA5": 0, "周MA10": 0,
        "周量比": 0, "近月涨幅%": 0, "基准周": "",
    }
    if df is None:
        empty["说明"] = "ETF 日线获取失败"
        return empty

    # 日线 → 周线（周收盘、周成交量）
    w = df.set_index("日期").resample("W-FRI").agg(
        {"收盘": "last", "成交量": "sum"}).dropna()
    if len(w) < WEEK_UP_MA + 3:
        empty["说明"] = "周线数据不足"
        return empty
    w["wma5"] = w["收盘"].rolling(WEEK_UP_MA).mean()
    w["wma10"] = w["收盘"].rolling(10).mean()
    w["vma5"] = w["成交量"].rolling(VOL_LOOKBACK).mean()
    last = w.iloc[-1]

    week_up = bool(last["收盘"] > w["收盘"].iloc[-2] and
                   pd.notna(last["wma5"]) and pd.notna(last["wma10"]) and
                   last["wma5"] > last["wma10"])
    vol_ratio = float(last["成交量"] / last["vma5"]) if last["vma5"] and last["vma5"] > 0 else 0.0
    vol_surge = bool(vol_ratio >= VOL_SURGE_RATIO)
    strong = week_up and vol_surge

    # 近1个月表现（交易日）
    month_ret = float((df["收盘"].iloc[-1] / df["收盘"].iloc[-min(MONTH_DAYS, len(df))] - 1) * 100)

    if strong:
        level, desc = "强共振行业", "周线向上 + 放量，资金流入，趋势与资金共振"
    elif week_up:
        level, desc = "趋势向上", "周线向上但量能未放大，趋势成立待资金确认"
    elif vol_surge:
        level, desc = "资金潜伏", "放量但周线未明确向上，资金异动待趋势确认"
    else:
        level, desc = "弱势", "周线与量能均未达共振标准"

    return {
        "theme": theme, "etf_code": etf_code, "etf_name": "", "board": board,
        "等级": level, "周线向上": week_up, "周放量": vol_surge, "强共振": strong,
        "本周收盘": round(float(last["收盘"]), 4),
        "上周收盘": round(float(w["收盘"].iloc[-2]), 4),
        "周MA5": round(float(last["wma5"]), 4) if pd.notna(last["wma5"]) else 0,
        "周MA10": round(float(last["wma10"]), 4) if pd.notna(last["wma10"]) else 0,
        "周量比": round(vol_ratio, 2), "近月涨幅%": round(month_ret, 2),
        "基准周": str(w.index[-1].date()), "说明": desc,
    }


def weekly_resonance_all(end_date=None, top_only=False):
    """全部行业主题周线共振分析。返回按等级/近月涨幅排序的列表。"""
    universe = load_etf_universe()
    out = []
    for item in universe:
        r = weekly_resonance(item["etf_code"], item["theme"], item["board"], end_date)
        r["etf_name"] = item.get("etf_name", "")
        out.append(r)
    order = {"强共振行业": 0, "趋势向上": 1, "资金潜伏": 2, "弱势": 3, "数据异常": 4}
    out.sort(key=lambda r: (order.get(r["等级"], 9), -r.get("近月涨幅%", 0)))
    return out


def strong_themes(resonance_list):
    """提取强共振行业主题名列表。"""
    return [r["theme"] for r in resonance_list if r.get("强共振")]


def save_strong_codes(resonance_list):
    """把强共振行业的成分股代码→行业映射落盘，供每日模型推送读取。

    写入 industry_trend/state/strong_codes.json：
      {updated, week, themes:[...], code2theme:{code:{theme,board,etf_code}}}
    即使本周无强共振也写空清单（覆盖旧的），避免每日推送误用过期数据。
    """
    themes = strong_themes(resonance_list)
    members = load_sector_members()
    code2theme = {}
    for t in themes:
        info = members.get(t, {})
        for c in info.get("codes", []):
            code2theme[c] = {"theme": t, "board": info.get("board", ""),
                             "etf_code": info.get("etf_code", "")}
    payload = {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "week": datetime.now().strftime("%G-W%V"),
        "themes": themes,
        "code2theme": code2theme,
    }
    os.makedirs(STATE_DIR, exist_ok=True)
    _dump_json(STRONG_FILE, payload)
    print("[周度] 强共振清单落盘: %d行业, %d只成分股 -> %s" % (
        len(themes), len(code2theme), STRONG_FILE))
    return payload


def load_strong_codes(repo_root=None):
    """读取强共振清单。每日模型调用；找不到/过期返回空，绝不因它报错。

    repo_root：仓库根目录；不传则用模块所在目录的父级（适配 industry_trend 布局）。
    """
    path = STRONG_FILE
    if repo_root:
        path = os.path.join(repo_root, "industry_trend", "state", "strong_codes.json")
    data = _load_json(path, {}) or {}
    return data


def refresh_etf_cache(end_date=None):
    """刷新本地 ETF 日线缓存（qfq）。供每周 workflow 调用；失败保留旧缓存。

    返回 (成功只数, 失败列表)。写入 etf_daily_qfq.json。
    """
    import akshare as ak
    uni = load_etf_universe()
    cache = {}
    fail = []
    end = (end_date or datetime.now().strftime("%Y%m%d")).replace("-", "")
    for item in uni:
        code = str(item["etf_code"]).zfill(6)
        ok = False
        for attempt in range(3):
            try:
                df = ak.fund_etf_hist_em(symbol=code, period="daily",
                                         start_date="20240101", end_date=end, adjust="qfq")
                if df is not None and len(df) >= 30:
                    bars = []
                    for _, r in df.iterrows():
                        bars.append({
                            "d": str(r["日期"])[:10], "o": round(float(r["开盘"]), 4),
                            "h": round(float(r["最高"]), 4), "l": round(float(r["最低"]), 4),
                            "c": round(float(r["收盘"]), 4), "v": float(r["成交量"]),
                        })
                    cache[code] = {"theme": item["theme"], "name": item.get("etf_name", ""), "bars": bars}
                    ok = True
                    break
            except Exception:
                time.sleep(1.0)
        if not ok:
            fail.append(code)
        time.sleep(0.15)
    # 仅当成功数足够时才覆盖，避免大面积失败把缓存清空
    if len(cache) >= max(10, len(uni) // 2):
        _dump_json(ETF_DAILY_FILE, cache)
        load_etf_cache(force=True)
    return len(cache), fail


def refresh_sector_members():
    """刷新行业成分股映射（akshare 东财板块）。供每周 workflow 调用；失败保留旧表。

    返回 (成功行业数, 失败列表)。写入 sector_members.json。
    """
    import akshare as ak
    members = load_sector_members()
    old = dict(members)
    fail = []
    try:
        boards = ak.stock_board_industry_name_em()
        board_names = set(boards["板块名称"].astype(str).tolist())
    except Exception as e:
        return 0, ["板块列表: %s" % str(e)[:50]]
    for theme, info in old.items():
        board = info.get("board", "")
        matched = board if board in board_names else next(
            (b for b in board_names if board in b or b in board), None)
        if not matched:
            fail.append(theme)
            continue
        try:
            cons = ak.stock_board_industry_cons_em(symbol=matched)
            codes = [str(c).zfill(6) for c in cons["代码"].tolist()]
            members[theme] = dict(info)
            members[theme]["board"] = matched
            members[theme]["codes"] = codes
            members[theme]["count"] = len(codes)
        except Exception:
            fail.append(theme)
        time.sleep(0.3)
    ok = len(old) - len(fail)
    if ok >= max(10, len(old) // 2):
        _dump_json(SECTOR_MEMBERS_FILE, members)
    return ok, fail


# ============ 【B】三模型选股按强行业过滤 ============
def _latest_ledger(model, repo_root=None, lookback=7):
    """读取某模型最近 lookback 天内的选股台账，返回 {code: {name,score,date}}。"""
    if repo_root is None:
        repo_root = os.path.abspath(os.path.join(HERE, ".."))
    d = os.path.join(repo_root, "common", "state", "recommendations", model)
    if not os.path.isdir(d):
        return {}
    files = sorted([f for f in os.listdir(d) if f.endswith(".json")])
    cutoff = (datetime.now() - pd.Timedelta(days=lookback)).strftime("%Y-%m-%d")
    files = [f for f in files if f[:10] >= cutoff]
    picks = {}
    for f in files[-lookback:]:
        try:
            j = json.load(open(os.path.join(d, f), encoding="utf-8"))
            for p in j.get("picks", []):
                code = str(p.get("code", "")).zfill(6)
                if not code:
                    continue
                old = picks.get(code)
                if not old or f > old.get("date", "") or p.get("score", 0) > old.get("score", 0):
                    picks[code] = {"code": code, "name": p.get("name", ""),
                                   "score": p.get("score", 0), "date": f[:10]}
        except Exception:
            continue
    return picks


def overlay_model_picks(resonance_list, model_picks=None, repo_root=None, lookback=7):
    """
    把破位/奥雅/怡达选股按行业归类，落在强共振行业的标记【行业共振·重点】。
    model_picks: 可选 dict {模型名: {code:{name,score,date}}}；不传则从台账读。
    返回: (强调清单, 全部归类统计)
    """
    strong = set(strong_themes(resonance_list))
    members = load_sector_members()
    code2theme = {}
    for theme, info in members.items():
        for c in info.get("codes", []):
            code2theme.setdefault(c, []).append((theme, info.get("board", ""), info.get("etf_code", "")))

    if model_picks is None:
        model_picks = {
            "破位": _latest_ledger("powei", repo_root, lookback),
            "奥雅": _latest_ledger("stock-v3", repo_root, lookback),
            "怡达": _latest_ledger("yida", repo_root, lookback),
        }

    emphasized = []
    stats = {m: {"total": len(picks), "in_strong": 0, "by_theme": {}} for m, picks in model_picks.items()}
    for model, picks in model_picks.items():
        for code, info in picks.items():
            themes = code2theme.get(code, [])
            hit = [t for t in themes if t[0] in strong]
            if hit:
                theme, board, etf = hit[0]
                emphasized.append({
                    "model": model, "code": code, "name": info.get("name", ""),
                    "score": info.get("score", 0), "date": info.get("date", ""),
                    "theme": theme, "board": board, "etf_code": etf,
                })
                stats[model]["in_strong"] += 1
                stats[model]["by_theme"][theme] = stats[model]["by_theme"].get(theme, 0) + 1
    emphasized.sort(key=lambda x: (-x["score"], x["theme"]))
    return emphasized, stats


# ============ 【C】周度推送文本 ============
def build_weekly_push(resonance_list, emphasized=None, stats=None):
    """生成行业趋势周度推送文本。"""
    lines = ["📊【行业趋势 · 周线共振播报】"]
    strong = [r for r in resonance_list if r.get("强共振")]
    trend = [r for r in resonance_list if r.get("等级") == "趋势向上"]
    if strong:
        lines.append("")
        lines.append(f"🔥 强共振行业（周线向上+放量，资金流入）{len(strong)}个：")
        for r in strong:
            lines.append("  • %s（%s %s）近月%+.1f%% | 周量比%.2f | 基准周%s" % (
                r["theme"], r["etf_code"], r["etf_name"], r.get("近月涨幅%", 0),
                r.get("周量比", 0), r.get("基准周", "")))
    else:
        lines.append("")
        lines.append("🔥 本周无强共振行业（无行业同时满足周线向上+放量）。")
    if trend:
        lines.append("")
        lines.append("📈 趋势向上（待资金确认）：" + "、".join(r["theme"] for r in trend[:8]))

    if emphasized:
        lines.append("")
        lines.append("⭐【行业共振·重点】三模型选股命中强共振行业：")
        for e in emphasized[:20]:
            lines.append("  • [%s] %s(%s) 分%s <- %s行业" % (
                e["model"], e["name"], e["code"], e["score"], e["theme"]))
        if len(emphasized) > 20:
            lines.append("  …… 另有 %d 只" % (len(emphasized) - 20))
    if stats:
        lines.append("")
        lines.append("📌 命中统计：" + "；".join(
            "%s %d/%d" % (m, s["in_strong"], s["total"]) for m, s in stats.items()))

    lines.append("")
    lines.append(f"（生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}，每周更新；非投资建议）")
    return "\n".join(lines)


# ============ 【D】个股二次确认（保留原接口）============
def _load_kline():
    here = os.path.dirname(os.path.abspath(__file__))
    for c in [os.path.join(here, "..", "common"), os.path.join(here, "common"), here]:
        c = os.path.abspath(c)
        if os.path.isfile(os.path.join(c, "common_lib.py")) and c not in sys.path:
            sys.path.insert(0, c)
    from common_lib import load_kline
    return load_kline


def confirm(code, name="", end_date=None):
    """对单只个股做金叉+MA20拐头确认（共享行情表 qfq，只读本地）。"""
    try:
        load_kline = _load_kline()
        bars = load_kline(str(code), count=210, end=end_date, offline=True)
    except Exception:
        bars = None
    if not bars or len(bars) < 25:
        return {"代码": code, "名称": name, "等级": "数据异常", "说明": "共享行情表无此代码或数据不足"}
    df = pd.DataFrame(bars)
    src = "date" if "date" in df.columns else ("day" if "day" in df.columns else None)
    df["日期"] = pd.to_datetime(df[src]) if src else pd.NaT
    cs = "close" if "close" in df.columns else ("last" if "last" in df.columns else None)
    vs = "volume" if "volume" in df.columns else ("vol" if "vol" in df.columns else None)
    df["收盘"] = pd.to_numeric(df[cs], errors="coerce") if cs else np.nan
    df["成交量"] = pd.to_numeric(df[vs], errors="coerce") if vs else np.nan
    df["涨跌幅"] = df["收盘"].pct_change() * 100
    df = df.sort_values("日期").reset_index(drop=True)
    c = df["收盘"]
    df["MA5"] = c.rolling(5).mean(); df["MA10"] = c.rolling(10).mean()
    df["MA20"] = c.rolling(20).mean(); df["MA60"] = c.rolling(60).mean()
    df["VMA20"] = df["成交量"].rolling(20).mean()
    last = df.iloc[-1]
    cross = []
    for i in range(-WINDOW_DAYS, 0):
        if (df["MA5"].iloc[i] > df["MA10"].iloc[i]) and (df["MA5"].iloc[i-1] <= df["MA10"].iloc[i-1]):
            cross.append(i)
    golden = len(cross) > 0
    golden_age = abs(cross[-1]) if cross else 999
    ma_bull = last["MA5"] > last["MA10"]
    ma20_slope = (last["MA20"] / df["MA20"].iloc[-1-MA20_LOOKBACK] - 1) * 100
    ma20_up = ma20_slope > 0
    above20 = last["收盘"] > last["MA20"]
    above60 = bool(last["收盘"] > last["MA60"]) if pd.notna(last["MA60"]) else False
    vol_ratio = last["成交量"] / last["VMA20"] if last["VMA20"] > 0 else 0

    if golden and ma20_up and above20:
        level, desc = "重点样本", "金叉+MA20拐头+站上MA20，三条件共振"
    elif ma20_up and above60 and ma_bull:
        level, desc = "重点样本", "多头排列+MA20拐头+站上MA60，趋势稳健"
    elif golden and not ma20_up:
        level, desc = "观察样本", "金叉成立但MA20仍下行，反弹初期"
    elif ma20_up and above20:
        level, desc = "观察样本", "MA20拐头但未金叉，蓄势中"
    else:
        level, desc = "未命中", "技术信号未达确认标准"
    return {
        "代码": code, "名称": name, "等级": level,
        "金叉": golden, "金叉距今天数": golden_age, "MA5>MA10": bool(ma_bull),
        "MA20斜率%": round(float(ma20_slope), 2), "MA20拐头": bool(ma20_up),
        "站上MA20": bool(above20), "站上MA60": above60, "量比": round(float(vol_ratio), 2),
        "说明": desc, "基准日": str(last["日期"].date()) if pd.notna(last["日期"]) else "",
    }


def batch_confirm(codes, names=None, end_date=None):
    return [confirm(c, (names or {}).get(c, ""), end_date) for c in codes]


def filter_focus(codes, names=None, end_date=None):
    return [r for r in batch_confirm(codes, names, end_date) if r["等级"] == "重点样本"]


# ============ 【E】微信推送 ============
def push_weekly(title, text):
    """通过 PushPlus 发微信（与全仓库统一通道）。无 token 时仅打印。返回是否成功。"""
    token = (os.environ.get("PUSHPLUS_TOKEN") or "").strip()
    if not token:
        print("[推送] 未配置 PUSHPLUS_TOKEN，仅打印正文")
        return False
    try:
        import requests
        r = requests.post("https://www.pushplus.plus/send", json={
            "token": token, "title": title, "content": text, "template": "txt"}, timeout=20)
        ok = bool(r.ok and r.json().get("code") == 200)
        print("[推送] %s" % ("成功" if ok else "失败: %s" % r.text[:120]))
        return ok
    except Exception as e:
        print("[推送] 异常: %s" % str(e)[:120])
        return False


# ============ 【F】周度主入口（供 workflow 一键调用）============
def run_weekly(do_refresh_etf=True, do_refresh_members=True, do_push=True,
               repo_root=None, end_date=None):
    """完整周度流程：刷新数据→共振分析→三模型叠加→推送。返回 (推送文本, 报告dict)。

    do_refresh_etf / do_refresh_members：是否联网刷新数据缓存（workflow 里为 True）。
    do_push：是否发微信（本地调试设 False）。
    """
    report = {}
    if do_refresh_etf:
        n, fail = refresh_etf_cache(end_date)
        report["etf_cache"] = {"ok": n, "fail": fail}
        print("[周度] ETF缓存刷新: %d只成功, 失败%s" % (n, fail))
    if do_refresh_members:
        n, fail = refresh_sector_members()
        report["sector_members"] = {"ok": n, "fail": fail}
        print("[周度] 行业成分刷新: %d行业成功, 失败%s" % (n, fail))

    res = weekly_resonance_all(end_date)
    emph, stats = overlay_model_picks(res, repo_root=repo_root)
    text = build_weekly_push(res, emph, stats)
    report["strong"] = strong_themes(res)
    report["emphasized"] = len(emph)
    report["stats"] = stats
    # 落盘强共振清单，供每日模型推送加共振标签用
    payload = save_strong_codes(res)
    report["strong_codes"] = {"themes": len(payload["themes"]),
                              "codes": len(payload["code2theme"])}

    if do_push:
        title = "行业趋势·周线共振播报 %s" % datetime.now().strftime("%m-%d")
        push_weekly(title, text)
    return text, report


if __name__ == "__main__":
    print("=" * 72)
    print("行业趋势模型 — 周线共振分析（%s）" % datetime.now().strftime("%Y-%m-%d"))
    print("=" * 72)
    res = weekly_resonance_all()
    strong = [r for r in res if r.get("强共振")]
    print("强共振行业 %d 个：" % len(strong))
    for r in strong:
        print("  🔥 %-8s %s %-14s 近月%+.1f%% 周量比%.2f" % (
            r["theme"], r["etf_code"], r["etf_name"], r["近月涨幅%"], r["周量比"]))
    print("\n等级分布：")
    from collections import Counter
    print("  ", dict(Counter(r["等级"] for r in res)))
    emph, stats = overlay_model_picks(res)
    print("\n" + build_weekly_push(res, emph, stats))
