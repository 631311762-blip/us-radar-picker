#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
美股异动雷达 × 易点启动买点 × 低位上影线 三合一云端选股
========================================================================
每个交易日盘前（北京时间 08:00）执行：
  1) 抓取隔夜美股行业/主题涨跌（用行业 ETF + 龙头股作代理，计算相对 SPY 的超额）
  2) 跑「易点模型」：模仿易点天下 7/31 启动形态的启动买点模型
  3) 跑「低位上影线模型」：低位 + 长上影 + 放量 + 未过 MA20
  4) A 股行情全部取东方财富公开接口，股票池 = 创业板 + 科创板 + 北交所
  5) 用美股板块超额做共振/逆风加权，输出最优 3 只 + 观察名单
  6) 推送微信（PushPlus），并落盘 state 供次日复盘

运行：
  python us_radar_picker.py                # 盘前正式跑
  python us_radar_picker.py --dry-run      # 只打印，不写状态不推送
  python us_radar_picker.py --no-push      # 不推送
  python us_radar_picker.py --force        # 忽略“今日已跑”检查

环境变量：
  PUSHPLUS_TOKEN      PushPlus token（与雷达同渠道）
  SERVERCHAN_KEY      方糖 SendKey（可选，两者都配则都推）
  STATE_DIR           状态目录（默认脚本同级 state/）
  WORKERS             并发数（默认 8）
依赖：requests
"""
import argparse
import json
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from statistics import mean

import requests

# ------------------------------------------------------------------ 基础配置
BJT = timezone(timedelta(hours=8))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("STATE_DIR") or os.path.join(SCRIPT_DIR, "state")
WORKERS = int(os.environ.get("WORKERS") or 8)
PUSHPLUS_TOKEN = (os.environ.get("PUSHPLUS_TOKEN") or "").strip()
SERVERCHAN_KEY = (os.environ.get("SERVERCHAN_KEY") or "").strip()
TOP_MAIN = 3

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://quote.eastmoney.com/",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# 东方财富公开接口（行情列表 / 日 K / F10）
# 东财按连接数限流，单域名会被断连，因此多分片域名轮询
EM_KLINE_HOSTS = [
    "push2his.eastmoney.com", "88.push2his.eastmoney.com", "22.push2his.eastmoney.com",
    "33.push2his.eastmoney.com", "48.push2his.eastmoney.com", "72.push2his.eastmoney.com",
    "1.push2his.eastmoney.com", "7.push2his.eastmoney.com",
]
EM_LIST_HOSTS = [
    "push2.eastmoney.com", "82.push2.eastmoney.com", "22.push2.eastmoney.com",
    "33.push2.eastmoney.com", "48.push2.eastmoney.com", "72.push2.eastmoney.com",
    "1.push2.eastmoney.com",
]
EM_F10 = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
EM_UT = "bd1d9ddb04089700cf9c27f6f7426281"
EM_KUT = "fa5fd1943c7b386f172d6893dbfba10b"

# 股票池：创业板 + 科创板 + 北交所
EM_BOARDS = [
    ("创业板", "m:0+t:80"),
    ("科创板", "m:1+t:23"),
    ("北交所", "m:0+t:81+s:2048"),
]

# 备用 K 线源（东财被限流时兜底）
SINA_KLINE = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
              "CN_MarketData.getKLineData")
SINA_KLINE2 = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketData.getKLineData"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/%s?range=5d&interval=1d"
YAHOO2 = "https://query2.finance.yahoo.com/v8/finance/chart/%s?range=5d&interval=1d"
STOOQ = "https://stooq.com/q/d/l/?s=%s.us&i=d"

# 易点模型参数（与 stock_rec/config.json 保持一致）
YD_ENGINE = {
    "topN": 6, "candidatePool": 60,
    "minMarketCapYi": 20, "maxMarketCapYi": 500,
    "minTurnoverPct": 2, "maxTurnoverPct": 35,
    "requirePriorDrawdownPct": 15, "maxFiveDayGainPct": 45,
}
YD_WEIGHTS = {
    "deepDrop": 0.18, "volumeDryUp": 0.13, "ignitionStrength": 0.20,
    "volumeExpansion": 0.16, "ma20Reclaim": 0.16,
    "notExtendedFromMa60": 0.10, "shortMaTurnUp": 0.07, "sectorResonance": 0.10,
}

# 低位上影线参数（与 low_shadow_screener.py 默认值一致）
SHADOW = {
    "upper_amp_min": 0.08, "upper_to_body_min": 2.0,
    "body_abs_max_pct": 0.07, "vol_ratio_min": 1.5,
    "low_pos_max_pct": 0.15, "below_ma20": True, "min_amount": 5e7,
    "shadow_comp_enabled": True, "comp_shadow_min": 0.06,
    "comp_upper_to_body_min": 3.5, "comp_low_pos_max_pct": 0.05,
}

# 美股主题代理（行业 ETF / 龙头股），括号内为 Yahoo 代码
US_THEME_PROXIES = {
    "半导体存储": ["SMH", "SOXX"],
    "消费电子苹果链": ["AAPL", "XLK"],
    "光伏": ["TAN"],
    "清洁能源环保": ["ICLN"],
    "固废处理": ["WM", "RSG"],
    "家电": ["WHR", "XLY"],
    "电源设备": ["XLU"],
    "化工塑料": ["XLB"],
    "有色黄金": ["XME", "GDX"],
    "军工": ["ITA"],
    "医药": ["XLV", "XBI"],
    "金融": ["XLF"],
    "机械工业": ["XLI"],
    "地产建筑": ["XLRE"],
    "能源": ["XLE"],
    "通信传媒": ["XLC"],
    "食品饮料": ["XLP"],
}

# A 股行业关键词 -> 美股主题（顺序即优先级）
THEME_RULES = [
    ("半导体存储", ["半导体", "集成电路", "芯片", "存储", "电子元件", "光学光电子", "电子化学品"]),
    ("消费电子苹果链", ["消费电子", "苹果", "面板", "PCB", "电子零部件"]),
    ("光伏", ["光伏", "太阳能", "逆变器", "硅料", "组件"]),
    ("清洁能源环保", ["环保", "固废", "生物质", "水务", "节能", "环境治理", "垃圾"]),
    ("家电", ["家电", "厨卫", "小家电", "白色家电", "黑色家电"]),
    ("电源设备", ["电源", "电网", "电气设备", "输变电", "储能"]),
    ("化工塑料", ["化工", "塑料", "橡胶", "化学", "化纤", "涂料"]),
    ("有色黄金", ["有色", "黄金", "贵金属", "小金属", "工业金属", "稀土"]),
    ("军工", ["军工", "国防", "航空", "航天", "兵器"]),
    ("医药", ["医药", "生物", "制药", "医疗", "中药", "疫苗"]),
    ("金融", ["银行", "证券", "保险", "金融"]),
    ("机械工业", ["机械", "专用设备", "通用设备", "自动化", "工程机械", "机器人"]),
    ("地产建筑", ["房地产", "建筑", "建材", "装修", "水泥"]),
    ("能源", ["煤炭", "石油", "油气", "能源"]),
    ("通信传媒", ["通信", "传媒", "互联网", "游戏", "广告", "影视"]),
    ("食品饮料", ["食品", "饮料", "白酒", "调味", "乳品"]),
]
FIRST_LEVEL_THEME = {
    "电子": "消费电子苹果链", "电力设备": "电源设备", "家用电器": "家电",
    "环保": "清洁能源环保", "基础化工": "化工塑料", "有色金属": "有色黄金",
    "国防军工": "军工", "医药生物": "医药", "机械设备": "机械工业",
    "计算机": "半导体存储", "通信": "通信传媒", "传媒": "通信传媒",
    "银行": "金融", "非银金融": "金融", "房地产": "地产建筑",
    "建筑材料": "地产建筑", "建筑装饰": "地产建筑", "煤炭": "能源",
    "石油石化": "能源", "钢铁": "化工塑料", "食品饮料": "食品饮料",
    "汽车": "机械工业", "公用事业": "电源设备", "交通运输": "机械工业",
    "商贸零售": "消费电子苹果链", "纺织服饰": "家电", "轻工制造": "家电",
    "社会服务": "消费电子苹果链", "农林牧渔": "食品饮料",
}


# ------------------------------------------------------------------ 通用工具
def clamp(x, a=0.0, b=1.0):
    return max(a, min(b, x))


def round2(x):
    return round(float(x) + 0.0, 2)


def http_json(url, retry=2, timeout=15, params=None, session=None, referer=None):
    s = session or SESSION
    headers = {"Referer": referer} if referer else None
    for attempt in range(retry + 1):
        try:
            r = s.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code != 200:
                raise RuntimeError("HTTP %s" % r.status_code)
            return r.json()
        except Exception:
            if attempt < retry:
                time.sleep(0.4 * (attempt + 1))
    return None


def http_text(url, retry=2, timeout=15, params=None, encoding=None):
    for attempt in range(retry + 1):
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            if r.status_code != 200:
                raise RuntimeError("HTTP %s" % r.status_code)
            if encoding:
                r.encoding = encoding
            return r.text
        except Exception:
            if attempt < retry:
                time.sleep(0.4 * (attempt + 1))
    return None


def szsh_of(code):
    if code.startswith(("8", "4", "92")):
        return "bj" + code
    return ("sh" if code.startswith("6") else "sz") + code


def secu_of(code):
    if code.startswith("6"):
        return code + ".SH"
    if code.startswith(("8", "4", "92")):
        return code + ".BJ"
    return code + ".SZ"


def secid_of(code):
    """东财 secid：沪市 1.，深市/北交所 0."""
    code = str(code).zfill(6)
    return ("1." if code.startswith("6") else "0.") + code


def last_weekday(today):
    d = today - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()


def pct_rank(values):
    n = len(values)
    if n <= 1:
        return [1.0] * n
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg / n
        i = j + 1
    return ranks


def theme_of(ind):
    if not ind:
        return None
    for theme, kws in THEME_RULES:
        if any(k in ind for k in kws):
            return theme
    return FIRST_LEVEL_THEME.get(str(ind).split("-")[0])


def fmt_pct(x):
    return "%+.2f%%" % (x * 100)


# ------------------------------------------------------------------ 美股异动雷达
def yahoo_closes(symbol):
    for url in (YAHOO % symbol, YAHOO2 % symbol):
        j = http_json(url, timeout=8, retry=0)
        try:
            res = j["chart"]["result"][0]
            closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
            if len(closes) >= 2:
                return closes
        except Exception:
            pass
    closes = em_us_closes(symbol)
    if closes:
        return closes
    txt = http_text(STOOQ % symbol.lower(), timeout=8, retry=0)
    if not txt:
        return []
    lines = [ln for ln in txt.strip().splitlines() if ln.strip()]
    if len(lines) < 3:
        return []
    closes = []
    for ln in lines[1:]:
        parts = ln.split(",")
        if len(parts) >= 5:
            try:
                closes.append(float(parts[4]))
            except ValueError:
                pass
    return closes[-5:]


def em_us_closes(symbol):
    """东财美股兜底：105 纳斯达克 / 106 纽交所 / 107 美交所"""
    params = {
        "ut": EM_KUT, "fields1": "f1,f2", "fields2": "f51,f53",
        "klt": 101, "fqt": 1, "beg": 20250901, "end": 20500101, "lmt": 10,
    }
    for prefix in ("105", "106", "107"):
        p = dict(params, secid="%s.%s" % (prefix, symbol.upper()))
        for host in random.sample(EM_KLINE_HOSTS, 2):
            j = http_json("https://%s/api/qt/stock/kline/get" % host,
                          params=p, timeout=10, retry=0)
            rows = ((j or {}).get("data") or {}).get("klines") or []
            closes = []
            for s in rows:
                parts = s.split(",")
                if len(parts) >= 2:
                    try:
                        closes.append(float(parts[1]))
                    except ValueError:
                        pass
            if len(closes) >= 2:
                return closes
    return []


def pct_of(closes):
    if len(closes) < 2:
        return None, None
    d1 = closes[-1] / closes[-2] - 1
    d3 = closes[-1] / closes[-4] - 1 if len(closes) >= 4 else None
    return d1, d3


def build_us_radar():
    syms = sorted({s for lst in US_THEME_PROXIES.values() for s in lst} | {"SPY"})
    pct = {}
    with ThreadPoolExecutor(max_workers=min(8, len(syms))) as ex:
        futs = {ex.submit(yahoo_closes, s): s for s in syms}
        for fu in as_completed(futs):
            pct[futs[fu]] = pct_of(fu.result())
    spy = pct.get("SPY", (None, None))[0]
    bench = spy or 0.0
    themes = {}
    for theme, symlist in US_THEME_PROXIES.items():
        vals, vals3 = [], []
        for sym in symlist:
            d1, d3 = pct.get(sym, (None, None))
            if d1 is not None:
                vals.append(d1)
            if d3 is not None:
                vals3.append(d3)
        if not vals:
            continue
        themes[theme] = {
            "pct": mean(vals),
            "pct3": mean(vals3) if vals3 else None,
            "excess": mean(vals) - bench,
            "symbols": syms,
        }
    return {"spy": spy, "bench": bench, "themes": themes}


# ------------------------------------------------------------------ A 股行情（东方财富公开接口）
def to_float(v, default=0.0):
    try:
        if v in ("-", "", None):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def get_universe():
    """东方财富行情列表：创业板 + 科创板 + 北交所，一次拿齐 OHLC / 成交额 / 市值"""
    fields = ("f2,f3,f5,f6,f8,f12,f13,f14,f15,f16,f17,f18,f20,f21")
    rows = []
    for name, fs in EM_BOARDS:
        pn = 1
        while True:
            params = {
                "pn": pn, "pz": 100, "po": 1, "np": 1, "fid": "f3",
                "fs": fs, "fields": fields, "ut": EM_UT,
                "fltt": 2, "invt": 2,
            }
            j, data = None, {}
            hosts = EM_LIST_HOSTS[:]
            random.shuffle(hosts)
            for host in hosts:
                j = http_json("https://%s/api/qt/clist/get" % host,
                              params=params, timeout=15, retry=0)
                data = (j or {}).get("data") or {}
                if data.get("diff"):
                    break
                time.sleep(0.2)
            diff = data.get("diff") or []
            if isinstance(diff, dict):
                diff = list(diff.values())
            if not diff:
                break
            total = int(to_float(data.get("total")))
            for x in diff:
                code = str(x.get("f12", "")).zfill(6)
                price = to_float(x.get("f2"))
                if not code or price <= 0:
                    continue
                rows.append({
                    "code": code, "name": x.get("f14", ""), "board": name,
                    "szsh": szsh_of(code), "price": price,
                    "pct": to_float(x.get("f3")),
                    "turnover": to_float(x.get("f8")),
                    "market_cap_yi": to_float(x.get("f20")) / 1e8,
                    "open": to_float(x.get("f17")),
                    "high": to_float(x.get("f15")),
                    "low": to_float(x.get("f16")),
                    "prev_close": to_float(x.get("f18")),
                    "volume": to_float(x.get("f5")),
                    "amount": to_float(x.get("f6")),
                })
            if len(diff) < 100:
                break
            if total and pn * 100 >= total:
                break
            pn += 1
            time.sleep(0.2)
    # 去重
    seen, uniq = set(), []
    for r in rows:
        if r["code"] in seen:
            continue
        seen.add(r["code"])
        uniq.append(r)
    return uniq


def em_hist(code, count=140):
    """东财日 K（前复权）；beg 限制回看天数，分片域名轮询规避限流"""
    beg = (datetime.now(BJT).date() - timedelta(days=max(30, count * 3))).strftime("%Y%m%d")
    params = {
        "secid": secid_of(code), "ut": EM_KUT,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": 101, "fqt": 1, "beg": beg, "end": 20500101, "lmt": count,
    }
    hosts = EM_KLINE_HOSTS[:]
    random.shuffle(hosts)
    for host in hosts[:3]:
        j = http_json("https://%s/api/qt/stock/kline/get" % host,
                      params=params, timeout=12, retry=0)
        rows = ((j or {}).get("data") or {}).get("klines") or []
        bars = []
        for s in rows:
            p = s.split(",")
            if len(p) < 6:
                continue
            try:
                bars.append({"d": p[0], "o": float(p[1]), "c": float(p[2]),
                             "h": float(p[3]), "l": float(p[4]), "v": float(p[5])})
            except ValueError:
                continue
        if bars:
            return bars
        time.sleep(0.15)
    return None


def sina_hist(szsh, datalen=60):
    for url in (SINA_KLINE, SINA_KLINE2):
        j = http_json(url, params={"symbol": szsh, "scale": 240, "ma": "no",
                                   "datalen": datalen}, timeout=10, retry=1)
        if not isinstance(j, list) or not j:
            continue
        bars = []
        for b in j:
            try:
                bars.append({
                    "d": b["day"], "o": float(b["open"]), "c": float(b["close"]),
                    "h": float(b["high"]), "l": float(b["low"]),
                    "v": float(b["volume"]),
                })
            except (KeyError, TypeError, ValueError):
                continue
        if len(bars) >= 2:
            return bars
    return None


def get_kline(code, count=140):
    """东财 K 线优先；失败时用新浪兜底"""
    bars = em_hist(code, count)
    if bars:
        return bars
    return sina_hist(szsh_of(code), count)


def index_of_date(bars, day):
    """按日期定位 K 线（取最后一次出现，防重复日期）"""
    for i in range(len(bars) - 1, -1, -1):
        if bars[i]["d"] == day:
            return i
    return None


def _f10_chunk(secucodes):
    inlist = ",".join('"%s"' % c for c in secucodes)
    j = http_json(EM_F10, params={
        "reportName": "RPT_F10_BASIC_ORGINFO",
        "columns": "SECUCODE,SECURITY_NAME_ABBR,EM2016,ACCOUNTFIRM_NAME",
        "filter": "(SECUCODE in (%s))" % inlist,
        "pageNumber": 1, "pageSize": 50, "source": "HSF10", "client": "PC",
    }, timeout=15, retry=1)
    if isinstance(j, list) and j:
        j = j[0]
    rows = []
    try:
        rows = j["result"]["data"] or []
    except Exception:
        rows = []
    out = {}
    for row in rows:
        code = str(row["SECUCODE"]).split(".")[0]
        out[code] = {
            "name": row.get("SECURITY_NAME_ABBR", ""),
            "ind": row.get("EM2016", "") or "",
            "firm": row.get("ACCOUNTFIRM_NAME", "") or "",
        }
    return out


def f10_batch(codes):
    """东财 F10：EM2016 行业 + 审计机构（40 只一批，空响应自动拆成 10 只）"""
    out = {}
    codes = [secu_of(c) for c in codes]
    for i in range(0, len(codes), 40):
        chunk = codes[i:i + 40]
        got = _f10_chunk(chunk)
        if not got and len(chunk) > 10:
            for j in range(0, len(chunk), 10):
                got.update(_f10_chunk(chunk[j:j + 10]))
                time.sleep(0.2)
        out.update(got)
        time.sleep(0.2)
    return out


# ------------------------------------------------------------------ 易点模型
def calc_indicator(bars):
    n = len(bars)
    if n < 91:
        return None
    i = n - 1
    C = [b["c"] for b in bars]
    H = [b["h"] for b in bars]
    L = [b["l"] for b in bars]
    V = [b["v"] for b in bars]

    def ma_at(w, idx):
        return sum(C[idx - w + 1:idx + 1]) / float(w)

    m5, m10, m20, m60 = ma_at(5, i), ma_at(10, i), ma_at(20, i), ma_at(60, i)
    m20p, m5p = ma_at(20, i - 5), ma_at(5, i - 3)
    v20 = mean(V[i - 20:i])
    v10 = mean(V[i - 10:i])
    v30 = mean(V[i - 40:i - 10])
    hi90 = max(H[i - 90:i])
    lo10 = min(L[i - 10:i])
    hi20 = max(H[i - 19:i + 1])
    lo20 = min(L[i - 19:i + 1])
    hi10 = max(H[i - 9:i + 1])

    return {
        "date": bars[i]["d"], "close": C[i], "open": bars[i]["o"],
        "high": H[i], "low": L[i],
        "pct": (C[i] / C[i - 1] - 1) * 100,
        "volume_ratio": V[i] / v20 if v20 > 0 else 0,
        "drawdown": 1 - lo10 / hi90 if hi90 > 0 else 0,
        "volume_dry": v10 / v30 if v30 > 0 else 99,
        "ma5": m5, "ma10": m10, "ma20": m20, "ma60": m60,
        "ma20_slope5": (m20 / m20p - 1) * 100,
        "ma5_turn3": (m5 / m5p - 1) * 100,
        "ret5": (C[i] / C[i - 5] - 1) * 100,
        "ret60": (C[i] / C[i - 60] - 1) * 100,
        "high20": hi20, "low20": lo20, "high10": hi10,
        "close_over_ma20": C[i] / m20 if m20 > 0 else 99,
        "close_over_ma60": C[i] / m60 if m60 > 0 else 99,
        "body_mid": (bars[i]["o"] + C[i]) / 2.0,
    }


def yd_state(f):
    if f["pct"] >= 9.5 and f["volume_ratio"] >= 1.6:
        return "ALREADY"
    if f["pct"] >= 3 and f["volume_ratio"] >= 1.4 and f["close_over_ma20"] >= 0.95:
        return "NEAR"
    if f["volume_dry"] < 0.8 and f["ma5_turn3"] > -0.01:
        return "BASE"
    return "WEAK"


def yd_score(f, sector_res=0.5):
    s1 = clamp((f["drawdown"] - 0.15) / 0.30)
    s2 = clamp((0.85 - f["volume_dry"]) / 0.25)
    s3 = clamp(f["pct"] / 12.0)
    s4 = clamp((f["volume_ratio"] - 1.0) / 1.0)
    if f["close"] > f["ma20"] and f["close_over_ma20"] <= 1.18:
        s5 = 1.0
    elif f["close_over_ma20"] >= 0.95:
        s5 = 0.5
    else:
        s5 = 0.0
    s6 = clamp((1.15 - f["close_over_ma60"]) / 0.30)
    s7 = clamp((f["ma5_turn3"] + 0.02) / 0.06)
    s8 = clamp(sector_res)
    parts = {
        "deepDrop": s1 * YD_WEIGHTS["deepDrop"] * 100,
        "volumeDry": s2 * YD_WEIGHTS["volumeDryUp"] * 100,
        "ignition": s3 * YD_WEIGHTS["ignitionStrength"] * 100,
        "volExpand": s4 * YD_WEIGHTS["volumeExpansion"] * 100,
        "ma20Reclaim": s5 * YD_WEIGHTS["ma20Reclaim"] * 100,
        "nearMA60": s6 * YD_WEIGHTS["notExtendedFromMa60"] * 100,
        "maTurn": s7 * YD_WEIGHTS["shortMaTurnUp"] * 100,
        "sector": s8 * YD_WEIGHTS["sectorResonance"] * 100,
    }
    return round(sum(parts.values()), 1), parts


def yd_plan(f, state):
    if state == "ALREADY":
        low = max(f["body_mid"], f["ma10"])
        mid = round2((low + f["close"]) / 2)
        return {
            "mode": "回踩低吸",
            "buy_low": round2(mid * 0.995), "buy_high": round2(f["close"] * 1.02),
            "trigger": "次日回踩 %s 附近且分时不破，量能不缩过半" % mid,
            "stop": round2(f["low"] * 0.995),
            "target1": round2(f["close"] * 1.15),
            "target2": round2(f["close"] * 1.25),
            "giveup": "次日高开 > +6%% 或跌破 %s 放弃" % round2(f["low"] * 0.995),
        }
    if state == "NEAR":
        trig = max(f["high10"] * 1.005, f["ma20"] * 1.015)
        bh = trig * 1.02
        t1 = max(f["ma60"], bh * 1.06)
        t2 = max(f["high20"], t1 * 1.03)
        return {
            "mode": "突破确认",
            "buy_low": round2(trig), "buy_high": round2(bh),
            "trigger": "放量站上 %s（30分钟量能 > 昨日同期 80%%）" % round2(trig),
            "stop": round2(f["ma20"] * 0.97),
            "target1": round2(t1), "target2": round2(t2),
            "giveup": "收盘跌回 MA20(%s) 下方 放弃" % round2(f["ma20"]),
        }
    trig = f["ma20"] * 1.02
    bh = trig * 1.02
    t1 = max(f["ma60"], bh * 1.06)
    t2 = max(f["high20"], t1 * 1.03)
    return {
        "mode": "观察待突破",
        "buy_low": round2(trig), "buy_high": round2(bh),
        "trigger": "缩量筑底完成，放量突破 MA20(%s)" % round2(f["ma20"]),
        "stop": round2(f["low20"] * 0.98),
        "target1": round2(t1), "target2": round2(t2),
        "giveup": "连续 3 日不突破则移除",
    }


def screen_yidian(universe):
    E = YD_ENGINE
    pool = [u for u in universe
            if not re.search("ST|退", u["name"]) and not u["name"].startswith("N")
            and 0.5 <= u["pct"] <= 22 and 2 <= u["turnover"] <= 35
            and 20 <= u["market_cap_yi"] <= 500 and 2 < u["price"] < 200]
    pool.sort(key=lambda x: x["pct"], reverse=True)
    pool = pool[:int(E["candidatePool"] * 1.5)]
    if not pool:
        return []

    f10 = f10_batch([u["code"] for u in pool])
    cand, excluded = [], 0
    for u in pool:
        a = f10.get(u["code"])
        if not a:
            continue
        if a["firm"] and "立信" in a["firm"]:
            excluded += 1
            continue
        cand.append({**u, "ind": a.get("ind", ""), "firm": a["firm"]})
    if not cand:
        return []

    ind_stat = {}
    for c in cand:
        seg = (c["ind"] or "").split("-")[0]
        if not seg:
            continue
        s = ind_stat.setdefault(seg, {"n": 0, "up": 0, "sum": 0.0})
        s["n"] += 1
        s["up"] += 1 if c["pct"] > 0 else 0
        s["sum"] += c["pct"]

    scored = []

    def work(c):
        bars = get_kline(c["code"], 140)
        if not bars:
            return None
        f = calc_indicator(bars)
        if not f:
            return None
        if f["drawdown"] < E["requirePriorDrawdownPct"] / 100.0:
            return None
        if f["ret5"] > E["maxFiveDayGainPct"]:
            return None
        st = yd_state(f)
        if st == "WEAK":
            return None
        res = 0.5
        seg = (c["ind"] or "").split("-")[0]
        if seg and seg in ind_stat:
            s = ind_stat[seg]
            res = (s["up"] / s["n"]) * 0.6 + clamp((s["sum"] / s["n"]) / 6.0) * 0.4
        total, parts = yd_score(f, res)
        return {**c, "state": st, "score": total, "parts": parts, "f": f}

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = []
        for c in cand:
            time.sleep(random.uniform(0.15, 0.35))
            futs.append(ex.submit(work, c))
        for fu in as_completed(futs):
            r = fu.result()
            if r:
                scored.append(r)

    scored.sort(key=lambda x: x["score"], reverse=True)
    picks = scored[:E["topN"]]
    for p in picks:
        p["plan"] = yd_plan(p["f"], p["state"])
    return picks, len(excluded)


# ------------------------------------------------------------------ 上影线模型
def shadow_evaluate(bars, idx, params):
    if not bars or idx is None or idx < 20 or idx >= len(bars):
        return None
    cur = bars[idx]
    o, h, l, c, v = cur["o"], cur["h"], cur["l"], cur["c"], cur["v"]
    if o <= 0 or h <= 0:
        return None
    body_abs = abs(c - o)
    upper = h - max(o, c)
    upper_amp = upper / o
    utb = upper / body_abs if body_abs > 0 else 99.0

    win = bars[idx - 19:idx + 1]
    low_20 = min(b["l"] for b in win)
    ma20 = mean(b["c"] for b in win)
    prev5 = bars[idx - 5:idx]
    vol_ma5 = mean(b["v"] for b in prev5) if prev5 else v
    vol_ratio = v / vol_ma5 if vol_ma5 > 0 else 0
    pos_pct = (c - low_20) / low_20 if low_20 > 0 else 99
    prev_close = bars[idx - 1]["c"]
    pct_chg = (c / prev_close - 1) * 100 if prev_close > 0 else 0
    amount = v * c

    main_path = upper_amp >= params["upper_amp_min"]
    comp_path = (params["shadow_comp_enabled"]
                 and upper_amp >= params["comp_shadow_min"]
                 and utb >= params["comp_upper_to_body_min"])
    if not (main_path or comp_path):
        return None
    if utb < params["upper_to_body_min"]:
        return None
    if vol_ratio < params["vol_ratio_min"]:
        return None
    is_comp = comp_path and not main_path
    pos_limit = params["comp_low_pos_max_pct"] if is_comp else params["low_pos_max_pct"]
    if pos_pct > pos_limit:
        return None
    if amount < params["min_amount"]:
        return None
    if abs(pct_chg) > params["body_abs_max_pct"] * 100:
        return None
    if params["below_ma20"] and c >= ma20:
        return None

    return {
        "date": cur["d"], "open": round2(o), "high": round2(h),
        "low": round2(l), "close": round2(c), "pct_chg": round2(pct_chg),
        "upper_shadow": round2(upper), "upper_amp": round(upper_amp, 4),
        "upper_to_body": round2(utb), "vol_ratio": round2(vol_ratio),
        "pos_pct": round(pos_pct, 4), "ma20": round2(ma20),
        "low_20": round2(low_20), "amount": round(amount), "comp_path": bool(is_comp),
    }


def shadow_rank(results, n=6):
    if not results:
        return []
    s_upper = pct_rank([r["upper_amp"] for r in results])
    s_vol = pct_rank([r["vol_ratio"] for r in results])
    s_low = pct_rank([-r["pos_pct"] for r in results])
    s_ratio = pct_rank([r["upper_to_body"] for r in results])
    for i, r in enumerate(results):
        r["score"] = round((s_upper[i] * 0.30 + s_vol[i] * 0.25
                            + s_low[i] * 0.25 + s_ratio[i] * 0.20) * 100, 2)
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:n]


def screen_shadow_hist(universe, base_date):
    """兜底：盘前快照被重置时，逐只拉 K 线按目标日判定"""
    params = SHADOW
    results = []

    def work(u):
        time.sleep(random.uniform(0.05, 0.15))
        bars = get_kline(u["code"], 60)
        if not bars:
            return None
        idx = index_of_date(bars, base_date)
        if idx is None:
            return None
        r = shadow_evaluate(bars, idx, params)
        if not r:
            return None
        r.update({"code": u["code"], "name": u["name"], "board": u["board"]})
        return r

    with ThreadPoolExecutor(max_workers=min(8, WORKERS)) as ex:
        futs = [ex.submit(work, u) for u in universe]
        for fu in as_completed(futs):
            r = fu.result()
            if r:
                results.append(r)
    return results


def screen_shadow(universe, base_date):
    params = SHADOW
    valid = [u for u in universe if u.get("open", 0) > 0 and u.get("high", 0) > 0]
    if len(valid) < 300:
        # 盘前快照未保留上一交易日 OHLC，退回逐只 K 线
        return shadow_rank(screen_shadow_hist(universe, base_date), 6)

    cand = []
    for u in valid:
        o, h, c = u["open"], u["high"], u["price"]
        body_abs = abs(c - o)
        upper = h - max(o, c)
        upper_amp = upper / o
        utb = upper / body_abs if body_abs > 0 else 99
        pct = (c / u["prev_close"] - 1) * 100 if u["prev_close"] > 0 else u["pct"]
        main_path = upper_amp >= params["upper_amp_min"]
        comp_path = (params["shadow_comp_enabled"]
                     and upper_amp >= params["comp_shadow_min"]
                     and utb >= params["comp_upper_to_body_min"])
        if not (main_path or comp_path):
            continue
        if utb < params["upper_to_body_min"]:
            continue
        if abs(pct) > params["body_abs_max_pct"] * 100:
            continue
        if u["amount"] < params["min_amount"]:
            continue
        cand.append({**u, "comp_path": bool(comp_path and not main_path)})

    results = []

    def work(u):
        bars = get_kline(u["code"], 60)
        if not bars:
            return None
        idx = index_of_date(bars, base_date)
        if idx is None:
            idx = len(bars) - 1
        r = shadow_evaluate(bars, idx, params)
        if not r:
            return None
        r.update({"code": u["code"], "name": u["name"], "board": u["board"]})
        return r

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = []
        for u in cand:
            time.sleep(random.uniform(0.2, 0.4))
            futs.append(ex.submit(work, u))
        for fu in as_completed(futs):
            r = fu.result()
            if r:
                results.append(r)
    return shadow_rank(results, 6)


# ------------------------------------------------------------------ 融合
def shadow_plan(r):
    close = r["close"]
    return {
        "mode": "次日低吸",
        "buy_low": round2(close * 0.97), "buy_high": round2(close * 1.02),
        "trigger": "次日不低开超过 2%，分时回踩不破昨低",
        "stop": round2(min(r["low"], close * 0.95)),
        "target1": round2(close * 1.12), "target2": round2(close * 1.20),
        "giveup": "低开跌破 %s 放弃" % round2(close * 0.95),
    }


def us_adjust(excess):
    """excess 为小数（0.02 = +2pct）"""
    if excess >= 0.020:
        return 12.0
    if excess >= 0.005:
        return 6.0
    if excess <= -0.020:
        return -15.0
    if excess <= -0.005:
        return -8.0
    return 0.0


def build_candidates(yd_picks, sh_picks, f10):
    cands = []
    for p in yd_picks:
        ind = p.get("ind", "")
        cands.append({
            "code": p["code"], "name": p["name"], "board": p["board"],
            "model": "易点", "state": p["state"], "close": p["f"]["close"],
            "pct": p["f"]["pct"], "vol_ratio": p["f"]["volume_ratio"],
            "score": p["score"], "plan": p["plan"], "ind": ind,
            "theme": theme_of(ind), "comp_path": False,
        })
    for r in sh_picks:
        a = f10.get(r["code"]) or {}
        ind = a.get("ind", "")
        cands.append({
            "code": r["code"], "name": r["name"], "board": r["board"],
            "model": "上影线", "state": "SHADOW", "close": r["close"],
            "pct": r["pct_chg"], "vol_ratio": r["vol_ratio"],
            "score": r["score"], "plan": shadow_plan(r), "ind": ind,
            "theme": theme_of(ind), "comp_path": r.get("comp_path", False),
        })
    return cands


def fuse(cands, radar):
    themes = radar.get("themes", {})
    for c in cands:
        sig = themes.get(c["theme"]) if c["theme"] else None
        c["excess"] = sig["excess"] if sig else 0.0
        c["theme_pct"] = sig["pct"] if sig else 0.0
        c["model_norm"] = min(100.0, float(c["score"]))
        c["final"] = round(c["model_norm"] + us_adjust(c["excess"]), 1)
        c["support"] = ("共振" if sig and sig["excess"] >= 0.005
                        else "逆风" if sig and sig["excess"] <= -0.005 else "中性")
    cands.sort(key=lambda x: x["final"], reverse=True)
    main = [c for c in cands if c["excess"] > -0.010][:TOP_MAIN]
    if len(main) < TOP_MAIN:
        for c in cands:
            if c not in main:
                main.append(c)
            if len(main) >= TOP_MAIN:
                break
    watch = [c for c in cands if c not in main][:5]
    avoid = [c for c in cands if c not in main and c not in watch
             and c["excess"] <= -0.020]
    return main, watch, avoid


# ------------------------------------------------------------------ 复盘/落盘
def review_previous(today):
    if not os.path.isdir(STATE_DIR):
        return []
    files = sorted(f for f in os.listdir(STATE_DIR)
                   if re.match(r"rec_\d{4}-\d{2}-\d{2}\.json$", f))
    files = [f for f in files if f[4:14] < today.isoformat()]
    if not files:
        return []
    latest = files[-1]
    try:
        with open(os.path.join(STATE_DIR, latest), "r", encoding="utf-8") as fh:
            rec = json.load(fh)
    except Exception:
        return []
    lines, rets = [], []
    for p in rec.get("picks", []):
        bars = get_kline(p["code"], 20)
        if not bars:
            continue
        idx = index_of_date(bars, rec["base_date"])
        if idx is None or idx + 1 >= len(bars):
            continue
        nxt = bars[idx + 1]
        ret = (nxt["c"] / p["close"] - 1) * 100
        rets.append(ret)
        lines.append("%s %s %+.2f%%" % (p["name"], p["code"], ret))
    if not rets:
        return []
    win = sum(1 for r in rets if r > 0)
    return ["上次(%s) %d只: %d涨%d跌 均%+.2f%%" %
            (rec["base_date"], len(rets), win, len(rets) - win, mean(rets))] + lines


def save_state(today, base_date, radar, picks):
    os.makedirs(STATE_DIR, exist_ok=True)
    fp = os.path.join(STATE_DIR, "rec_%s.json" % today.isoformat())
    data = {
        "date": today.isoformat(), "base_date": base_date,
        "radar": {"spy": radar.get("spy"), "themes": radar.get("themes", {})},
        "picks": [{
            "code": p["code"], "name": p["name"], "model": p["model"],
            "close": p["close"], "plan": p["plan"], "score": p["score"],
            "excess": p["excess"],
        } for p in picks],
    }
    with open(fp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    # 只保留最近 30 份
    allf = sorted(f for f in os.listdir(STATE_DIR) if f.startswith("rec_"))
    for f in allf[:-30]:
        os.remove(os.path.join(STATE_DIR, f))


# ------------------------------------------------------------------ 推送
def format_message(today, base_date, radar, main, watch, avoid, review):
    themes = radar.get("themes", {})
    ranked = sorted(themes.items(), key=lambda kv: kv[1]["excess"], reverse=True)
    out = []
    out.append("【美股异动×双模型 %s】" % today.isoformat())
    if ranked:
        strong_items = ranked[:3]
        weak_items = ranked[3:][-3:] if len(ranked) > 3 else []
        strong = " / ".join("%s%s" % (k, fmt_pct(v["excess"])) for k, v in strong_items)
        weak = " / ".join("%s%s" % (k, fmt_pct(v["excess"])) for k, v in weak_items)
        spy = radar.get("spy")
        out.append("SPY %s | 强: %s" % (fmt_pct(spy) if spy is not None else "--", strong))
        if weak:
            out.append("弱: %s" % weak)
    else:
        out.append("美股数据获取失败，本次仅按 A 股模型输出")
    out.append("A股基准日 %s | 最优 %d 只" % (base_date, len(main)))
    out.append("")
    for i, c in enumerate(main, 1):
        p = c["plan"]
        out.append("%d.%s %s %s·%s" % (i, c["name"], c["code"], c["model"],
                                       c["theme"] or c["ind"] or "未分类"))
        out.append("   收%.2f 涨%+.2f%% 量比%.2f 分%.1f | 美股%s %s" % (
            c["close"], c["pct"], c["vol_ratio"], c["score"], c["support"],
            fmt_pct(c["excess"])))
        out.append("   买 %s-%s（%s） 止 %s｜目标 %s/%s" % (
            p["buy_low"], p["buy_high"], p["mode"], p["stop"],
            p["target1"], p["target2"]))
    if watch:
        out.append("")
        out.append("观察: " + "、".join(
            "%s(%s)" % (c["name"], c["support"]) for c in watch))
    if avoid:
        out.append("规避: " + "、".join("%s(%s)" % (c["name"], c["theme"]) for c in avoid))
    if review:
        out.append("")
        out.append(review[0])
    out.append("")
    out.append("口径: 美股用 ETF/龙头代理算 SPY 超额; A 股为上一交易日收盘; 不构成投资建议")
    return "\n".join(out)


def push(title, content):
    ok = False
    if PUSHPLUS_TOKEN:
        try:
            r = requests.post("https://www.pushplus.plus/send", json={
                "token": PUSHPLUS_TOKEN, "title": title,
                "content": content, "template": "markdown",
            }, timeout=15)
            j = r.json()
            print("[PushPlus] code=%s msg=%s" % (j.get("code"), j.get("msg")))
            ok = j.get("code") == 200
        except Exception as e:
            print("[PushPlus] 异常: %s" % e)
    if SERVERCHAN_KEY:
        try:
            r = requests.post("https://sctapi.ftqq.com/%s.send" % SERVERCHAN_KEY,
                              data={"title": title, "desp": content}, timeout=15)
            j = r.json()
            print("[方糖] code=%s" % j.get("code"))
            ok = ok or j.get("code") == 0
        except Exception as e:
            print("[方糖] 异常: %s" % e)
    if not PUSHPLUS_TOKEN and not SERVERCHAN_KEY:
        print("[推送] 未配置 PUSHPLUS_TOKEN / SERVERCHAN_KEY，仅打印")
    return ok


# ------------------------------------------------------------------ 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    today = datetime.now(BJT).date()
    print("=" * 64)
    print("美股异动 × 易点 × 低位上影  盘前三合一  %s" % today)
    print("=" * 64)

    if not args.force and not args.dry_run:
        fp = os.path.join(STATE_DIR, "rec_%s.json" % today.isoformat())
        if os.path.exists(fp):
            print("[跳过] 今日已运行过 %s" % fp)
            return

    # 基准交易日：取样本 K 线最后一天（节假日自动回退）
    sample = em_hist("300118", 8) or em_hist("688001", 8)
    base_date = sample[-1]["d"] if sample else last_weekday(today)
    print("[基准] 上一交易日 = %s" % base_date)

    print("[1/4] 抓取隔夜美股行业...")
    radar = build_us_radar()
    if radar["themes"]:
        ranked = sorted(radar["themes"].items(), key=lambda kv: kv[1]["excess"], reverse=True)
        print("      SPY %s | 强: %s | 弱: %s" % (
            fmt_pct(radar["spy"]) if radar["spy"] is not None else "--",
            "、".join("%s%s" % (k, fmt_pct(v["excess"])) for k, v in ranked[:3]),
            "、".join("%s%s" % (k, fmt_pct(v["excess"])) for k, v in ranked[-3:])))
    else:
        print("      美股数据获取失败，继续跑 A 股模型")

    print("[2/4] 获取 A 股股票池...")
    universe = get_universe()
    print("      创业板/科创板/北交所 %d 只" % len(universe))
    if not universe:
        print("股票池为空，退出")
        return

    print("[3/4] 跑易点启动买点模型...")
    yd = screen_yidian(universe)
    if isinstance(yd, tuple):
        yd_picks, yd_excluded = yd
    else:
        yd_picks, yd_excluded = [], 0
    print("      命中 %d 只（剔除立信 %d）" % (len(yd_picks), yd_excluded))

    print("[4/4] 跑低位上影线模型...")
    sh_picks = screen_shadow(universe, base_date)
    print("      命中 %d 只" % len(sh_picks))

    f10 = f10_batch([r["code"] for r in sh_picks]) if sh_picks else {}
    # 与模型口径一致：剔除立信审计（F10 取不到则保留）
    sh_picks = [r for r in sh_picks
                if "立信" not in (f10.get(r["code"]) or {}).get("firm", "")]
    cands = build_candidates(yd_picks, sh_picks, f10)
    main_picks, watch, avoid = fuse(cands, radar)

    review = review_previous(today)
    msg = format_message(today, base_date, radar, main_picks, watch, avoid, review)
    print("\n" + msg)

    if args.dry_run:
        print("\n[dry-run] 不写状态、不推送")
        return
    if main_picks:
        save_state(today, base_date, radar, main_picks)
    if not args.no_push:
        push("美股异动×双模型 %s 最优%d只" % (today, len(main_picks)), msg)
    print("\n完成")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
