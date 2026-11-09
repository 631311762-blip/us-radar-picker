# -*- coding: utf-8 -*-
"""领湃工具箱：行情获取、PushPlus 推送、立信排除集、SW_B 广度闸门。
重建版：按原 prompt 口径推断实现，阈值/数据源需在真实标的上核对。
"""
import os
import sys
import json
import time
import random
import datetime
import requests

BJT = datetime.timezone(datetime.timedelta(hours=8))
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "")
STATE_DIR = os.environ.get("STATE_DIR", "state")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "*/*",
})

EM_KLINE_HOSTS = [
    "push2his.eastmoney.com", "88.push2his.eastmoney.com", "22.push2his.eastmoney.com",
    "33.push2his.eastmoney.com", "48.push2his.eastmoney.com", "72.push2his.eastmoney.com",
    "1.push2his.eastmoney.com", "7.push2his.eastmoney.com",
]
EM_LIST_HOSTS = [
    "push2.eastmoney.com", "82.push2.eastmoney.com", "22.push2.eastmoney.com",
    "33.push2his.eastmoney.com", "48.push2.eastmoney.com", "72.push2.eastmoney.com",
    "1.push2.eastmoney.com",
]
EM_F10 = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
EM_UT = "bd1d9ddb04089700cf9c27f6f7426281"
EM_KUT = "fa5fd1943c7b386f172d6893dbfba10b"

# 全 A：沪深主板 + 创业板 + 科创板 + 北交所
EM_BOARDS = [
    ("沪市主板", "m:1+t:2"),
    ("深市主板", "m:0+t:6"),
    ("创业板", "m:0+t:80"),
    ("科创板", "m:1+t:23"),
    ("北交所", "m:0+t:81+s:2048"),
]

LIXIN_FILE = "领湃_立信排除.json"
REC_FILE = "领湃_推荐记录.json"
SWB_FILE = "领湃_sw_b_breadth.csv"


# ------------------------------------------------------------------ 通用工具
def to_float(v, default=0.0):
    try:
        if v in ("-", "", None):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def http_json(url, retry=2, timeout=15, params=None, referer=None):
    headers = {"Referer": referer} if referer else None
    for attempt in range(retry + 1):
        try:
            r = SESSION.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code != 200:
                raise RuntimeError("HTTP %s" % r.status_code)
            return r.json()
        except Exception:
            if attempt < retry:
                time.sleep(0.4 * (attempt + 1))
    return None


def secu_of(code):
    if code.startswith("6"):
        return code + ".SH"
    if code.startswith(("8", "4", "92")):
        return code + ".BJ"
    return code + ".SZ"


def secid_of(code):
    code = str(code).zfill(6)
    return ("1." if code.startswith("6") else "0.") + code


def state_path(name):
    return os.path.join(STATE_DIR, name)


def today_str():
    return datetime.datetime.now(BJT).strftime("%Y-%m-%d")


def ensure_state():
    os.makedirs(STATE_DIR, exist_ok=True)


# ------------------------------------------------------------------ A 股行情（东方财富，前复权）
def get_universe():
    """返回全 A 股票基础列表（code/name/board/price/pct/turnover/market_cap_yi/prev_close）。"""
    fields = "f2,f3,f5,f6,f8,f12,f13,f14,f15,f16,f17,f18,f20,f21"
    rows = []
    for name, fs in EM_BOARDS:
        pn = 1
        while True:
            params = {
                "pn": pn, "pz": 100, "po": 1, "np": 1, "fid": "f3",
                "fs": fs, "fields": fields, "ut": EM_UT, "fltt": 2, "invt": 2,
            }
            data = {}
            hosts = EM_LIST_HOSTS[:]
            random.shuffle(hosts)
            for host in hosts:
                j = http_json("https://%s/api/qt/clist/get" % host, params=params, timeout=15, retry=0)
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
                    "price": price, "pct": to_float(x.get("f3")),
                    "turnover": to_float(x.get("f8")),
                    "market_cap_yi": to_float(x.get("f20")) / 1e8,
                    "prev_close": to_float(x.get("f18")),
                })
            if len(diff) < 100 or (total and pn * 100 >= total):
                break
            pn += 1
            time.sleep(0.2)
    seen, uniq = set(), []
    for r in rows:
        if r["code"] in seen:
            continue
        seen.add(r["code"])
        uniq.append(r)
    return uniq


def em_hist(code, count=180):
    """东方财富前复权日线，返回 [{d,o,c,h,l,v}]。"""
    beg = (datetime.datetime.now(BJT).date() - datetime.timedelta(days=max(60, count * 3))).strftime("%Y%m%d")
    params = {
        "secid": secid_of(code), "ut": EM_KUT,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": 101, "fqt": 1, "beg": beg, "end": 20500101, "lmt": count,
    }
    hosts = EM_KLINE_HOSTS[:]
    random.shuffle(hosts)
    for host in hosts[:3]:
        j = http_json("https://%s/api/qt/stock/kline/get" % host, params=params, timeout=12, retry=0)
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


# ------------------------------------------------------------------ 推送
def push(title, content, template="markdown"):
    if not PUSHPLUS_TOKEN:
        print("[推送] 未配置 PUSHPLUS_TOKEN，仅打印")
        print(content)
        return False
    try:
        r = requests.post("https://www.pushplus.plus/send", json={
            "token": PUSHPLUS_TOKEN, "title": title,
            "content": content, "template": template,
        }, timeout=15)
        j = r.json()
        print("[PushPlus] code=%s msg=%s" % (j.get("code"), j.get("msg")))
        return j.get("code") == 200
    except Exception as e:
        print("[PushPlus] 异常: %s" % e)
        return False


# ------------------------------------------------------------------ 立信排除集
def load_lixin_exclude():
    ensure_state()
    p = state_path(LIXIN_FILE)
    if not os.path.exists(p):
        return set()
    try:
        d = json.load(open(p, encoding="utf-8"))
        return set(d.get("codes", []))
    except Exception:
        return set()


def save_lixin_exclude(s):
    ensure_state()
    json.dump({"codes": sorted(s)}, open(state_path(LIXIN_FILE), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


# ------------------------------------------------------------------ 推荐记录
def load_records():
    ensure_state()
    p = state_path(REC_FILE)
    if not os.path.exists(p):
        return []
    try:
        return json.load(open(p, encoding="utf-8")) or []
    except Exception:
        return []


def save_records(recs):
    ensure_state()
    json.dump(recs, open(state_path(REC_FILE), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


# ------------------------------------------------------------------ SW_B 广度闸门
def _breadth_now():
    u = get_universe()
    if not u:
        return None
    up = sum(1 for x in u if x["pct"] > 0)
    return up / len(u)


def sw_b_breadth_ok(window=20):
    """今日上涨广度 > 近 window 日均值，则闸门通过。返回 (ok, 今日, 均值)。"""
    b = _breadth_now()
    if b is None:
        return False, None, None
    ensure_state()
    rows = []
    if os.path.exists(state_path(SWB_FILE)):
        rows = [l.strip() for l in open(state_path(SWB_FILE), encoding="utf-8") if l.strip()]
    today = today_str()
    rows = [r for r in rows if not r.startswith(today)]
    rows.append("%s,%.4f" % (today, b))
    rows = rows[-90:]
    open(state_path(SWB_FILE), "w", encoding="utf-8").write("\n".join(rows) + "\n")
    vals = [float(r.split(",")[1]) for r in rows]
    if len(vals) < window:
        # 历史样本不足，无法判断广度趋势，放行不拦截（避免新环境首次运行必被拦截）
        ma = sum(vals) / len(vals) if vals else b
        return True, b, ma
    recent = vals[-window:]
    ma = sum(recent) / len(recent)
    return (b > ma), b, ma
