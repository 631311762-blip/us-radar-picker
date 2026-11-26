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
    try:                       # 临近财报/预告：微信推送加醒目标识（离线读本地日历）
        import sys as _sys
        _sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))
        import report_risk as _rr
        title, content = _rr.decorate(title, content, template)
    except Exception:
        pass
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


# ------------------------------------------------------------------ 推送去重
# 2026-11-16：用户反馈「同一天收到两条领湃样本推送，样本一致但表述不同」。
# 根因是一次三步流程里 每日脚本 + 跟踪修正脚本 各推一条。现结构性收敛为
# 「每日仅一条选股清单」，并在此加按天去重保险，防止主流水线/兜底/手动补跑
# 同日重复推送。标记落在 lingpei/lingpei/state（与台账同目录，随 state 回写
# 入库），跨 runner 生效。
PUSH_DEDUP_FILE = "领湃_推送去重.json"
PUSH_MUTE_FILE = "领湃_推送静默.json"


def _push_dedup_path():
    # 用 __file__ 固定绝对路径，不依赖 CWD / STATE_DIR，避免各流程落点错位
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "lingpei", "state", PUSH_DEDUP_FILE)


def _push_mute_path():
    # 与去重标记同目录，随 state 回写 GitHub 跨 runner 生效
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "lingpei", "state", PUSH_MUTE_FILE)


def _write_push_dedup(key, p, rec, note=""):
    """写推送去重标记（静默时也写，供兜底/看门狗判定「今日已处理」避免反复补派）。"""
    rec = {key: datetime.datetime.now(BJT).isoformat(timespec="seconds") + note}
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        json.dump(rec, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception as e:
        print("[推送去重] 标记写入失败: %s" % e)


def push_muted():
    """读取微信推送静默开关。返回 True 表示暂停发微信（选股/台账照常）。
    开关文件 state/领湃_推送静默.json：{"muted": true, ...}；恢复推送改 false 或删文件。
    """
    try:
        d = json.load(open(_push_mute_path(), encoding="utf-8"))
        return bool(d.get("muted"))
    except Exception:
        return False


def push_once(title, content, template="markdown", kind="signal"):
    """同一自然日、同一类别只推送一次。

    kind:
      "signal" —— 正式入选清单（每日脚本）
      "notice" —— 闸门未过 / 无信号等提示
      "settle" —— 3/5/10 结算简报（默认不推，开关打开时独立成类）
    分类别去重，避免「先发无信号、后补出清单」被误拦。
    推送成功才写标记（失败允许补推）；LINGPEI_PUSH_FORCE=1 可强制绕过。
    2026-11-26：用户要求暂停微信推送——静默模式下写「已处理」标记但不发微信，
    兜底/看门狗读到今日标记即 skip，不会反复补派；选股/台账/结算完全不受影响。
    """
    day = today_str()
    key = "%s|%s" % (day, kind)
    force = os.environ.get("LINGPEI_PUSH_FORCE", "") == "1"
    p = _push_dedup_path()
    rec = {}
    try:
        if os.path.exists(p):
            rec = json.load(open(p, encoding="utf-8")) or {}
        if rec.get(key) and not force:
            print("[推送去重] %s 今日已推送，跳过重复推送" % key)
            return False
    except Exception:
        rec = {}
    # 静默模式：写「已处理」标记（防兜底补派），不实际发微信
    if push_muted() and not force:
        print("[推送静默] 用户已暂停微信推送，本次不发微信（已写去重标记供兜底识别）")
        _write_push_dedup(key, p, rec, "|muted")
        return False
    ok = push(title, content, template)
    if ok:
        # 只保留当天标记，文件不随日期膨胀
        _write_push_dedup(key, p, rec)
    return ok


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


# ------------------------------------------------------------------ V3.3 3/5/10 日跟踪台账
TRACK_FILE = "领湃_V33跟踪台账.json"
TRACK_HORIZONS = (3, 5, 10)
# 台账固定落在 lingpei/lingpei/state（与推荐记录同目录），不依赖 CWD / STATE_DIR，
# 避免「每日写入」与「结算读取」因运行目录不同而错位。
TRACK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lingpei", "state")


def load_track():
    """读取 V3.3 跟踪台账；不存在时返回空骨架。"""
    p = os.path.join(TRACK_DIR, TRACK_FILE)
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8")) or {}
        except Exception:
            return {}
    return {"model": "B_V3.3", "horizons": list(TRACK_HORIZONS), "samples": []}


def save_track(track):
    os.makedirs(TRACK_DIR, exist_ok=True)
    json.dump(track, open(os.path.join(TRACK_DIR, TRACK_FILE), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


def track_append(date, hits):
    """把某日 V3.3 全量命中写入 3/5/10 跟踪台账（按 code+date 去重）。

    hits 每项含 detect_coil_v2 返回字段 + code/name/entry_close。
    返回 (新增数, 台账总样本数)。
    """
    track = load_track()
    track.setdefault("model", "B_V3.3")
    track.setdefault("horizons", list(TRACK_HORIZONS))
    smp = track.setdefault("samples", [])
    exist = {(x.get("code"), x.get("date")) for x in smp}
    added = 0
    for h in hits:
        key = (h.get("code"), date)
        if key in exist:
            continue
        dev = h.get("dev_signed") or 0.0
        pos = "深破MA60" if dev <= -0.03 else ("贴线/略破" if dev <= 0 else "MA60上方")
        row = {
            "code": h.get("code"), "name": h.get("name", ""), "date": date, "batch": date,
            "score": h.get("score"), "dev_signed": dev, "dev60": h.get("dev60"),
            "drop_from_peak": h.get("drop_from_peak"), "gap_days": h.get("gap_days"),
            "trial_date": h.get("trial_date"), "fall_pp": h.get("fall_pp"),
            "sig_vr40": h.get("sig_vr40"), "shrink_prev3": h.get("shrink_prev3"),
            "trial_vr": h.get("trial_vr"), "k_amp": h.get("k_amp"),
            "k_lower": h.get("k_lower"), "k_c_from_low": h.get("k_c_from_low"),
            "k_low_vs_prev": h.get("k_low_vs_prev"), "near_ma": h.get("near_ma"),
            "entry_close": h.get("entry_close"), "position": pos,
        }
        for k in TRACK_HORIZONS:
            row["t%d" % k] = {"date": None, "close": None, "ret": None, "done": False}
        smp.append(row)
        exist.add(key)
        added += 1
    smp.sort(key=lambda x: (x.get("date", ""), -(x.get("score") or 0)))
    if added:
        save_track(track)
    return added, len(smp)


# ------------------------------------------------------------------ SW_B 广度闸门
def _breadth_now():
    u = get_universe()
    if not u:
        return None, 0, 0
    n = len(u)
    nz = sum(1 for x in u if x["pct"] != 0)
    up = sum(1 for x in u if x["pct"] > 0)
    return (up / n if n else 0.0), n, nz


def sw_b_breadth_ok(window=20):
    """今日上涨广度 > 近 window 日均值，则闸门通过。返回 (ok, 今日, 均值)。
    数据源异常防护：股票池样本过少或涨跌数据大量缺失时放行不拦截，避免东财接口异常误杀。
    历史样本不足时也放行，避免新环境首次运行必被拦截。"""
    b, n, nz = _breadth_now()
    if b is None:
        return False, None, None
    if n < 200 or nz < n * 0.5:
        print("[SW_B] 数据源异常（样本%d / 有效涨跌%d），放行不拦截" % (n, nz))
        return True, b, None
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
        ma = sum(vals) / len(vals) if vals else b
        return True, b, ma
    recent = vals[-window:]
    ma = sum(recent) / len(recent)
    return (b > ma), b, ma


# ------------------------------------------------------------------ 业绩预告风险备注（2026-11-23 用户要求）
# 背景：用户观察到「提示日若次日公告业绩预告、业绩不佳则暴跌」，要求在每日清单对临近业绩预告的股票
# 特别备注、便于谨慎对待。数据源：东方财富业绩预告报表 RPT_PUBLIC_OP_PREDICT。
# 注意：历史回放显示大亏样本多由突发利空驱动、未必有预告；本功能作为「风险提示」而非「过滤条件」，
#       不改变选股与评分，仅在推送清单加备注列。
FORECAST_FILE = "领湃_业绩预告缓存.json"
_FORECAST_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
# 负面预告类型关键词：出现这些即视为业绩利空、需要谨慎
_BAD_FORECAST_KW = ("预亏", "首亏", "续亏", "预减", "略减", "减亏", "不确定")


def refresh_forecast(year=None, force=False):
    """全量拉取当年业绩预告并缓存到 state。返回 (记录数, 是否刷新成功)。
    当日已刷新则跳过（除非 force）；网络失败时保留旧缓存不影响主流程。"""
    ensure_state()
    year = year or datetime.datetime.now(BJT).year
    cache = state_path(FORECAST_FILE)
    today = today_str()
    if not force and os.path.exists(cache):
        try:
            old = json.load(open(cache, encoding="utf-8"))
            if old.get("refreshed") == today and old.get("year") == year:
                return len(old.get("rows", [])), False
        except Exception:
            pass
    import urllib.parse
    rows, page, pages, size = [], 1, 1, 500
    flt = "(NOTICE_DATE>='%d-01-01')(NOTICE_DATE<='%d-12-31')" % (year, year)
    while page <= pages:
        q = urllib.parse.urlencode({
            "sortColumns": "NOTICE_DATE", "sortTypes": -1,
            "pageSize": size, "pageNumber": page,
            "reportName": "RPT_PUBLIC_OP_PREDICT", "columns": "ALL",
            "source": "WEB", "client": "WEB", "filter": flt,
        })
        d = http_json(_FORECAST_URL + "?" + q, retry=2, timeout=20)
        if not d or not d.get("result"):
            if page == 1:
                return 0, False
            break
        data = d["result"].get("data") or []
        for r in data:
            rows.append({
                "code": r.get("SECURITY_CODE"),
                "notice": (r.get("NOTICE_DATE") or "")[:10],
                "report": (r.get("REPORTDATE") or "")[:10],
                "type": r.get("FORECASTTYPE"),
                "increase": r.get("INCREASEJZ"),
                "content": r.get("FORECASTCONTENT"),
            })
        pages = d["result"].get("pages", 1)
        page += 1
        time.sleep(0.1)
    json.dump({"refreshed": today, "year": year, "rows": rows},
              open(cache, "w", encoding="utf-8"), ensure_ascii=False)
    return len(rows), True


def load_forecast():
    """读取业绩预告缓存，返回按 code 分组的 dict；无缓存返回空 dict。"""
    cache = state_path(FORECAST_FILE)
    if not os.path.exists(cache):
        return {}
    try:
        d = json.load(open(cache, encoding="utf-8"))
    except Exception:
        return {}
    by_code = {}
    for r in d.get("rows", []):
        c = r.get("code")
        if c:
            by_code.setdefault(c, []).append(r)
    return by_code


def _day_diff(d1, d2):
    """d1 - d2 的自然日差；d 格式 YYYY-MM-DD。解析失败返回 999。"""
    try:
        a = datetime.datetime.strptime(d1, "%Y-%m-%d").date()
        b = datetime.datetime.strptime(d2, "%Y-%m-%d").date()
        return (a - b).days
    except Exception:
        return 999


def forecast_risk(code, signal_date, by_code=None, ahead=3):
    """判断某股在提示日 [T, T+ahead] 窗口是否有业绩预告，给出风险备注。
    返回 dict: {level, text, notice, type, increase}。
      level='bad'   负面预告（预亏/预减/首亏…）→ 红色谨慎
      level='good'  正面预告（预增/略增…）    → 绿色
      level='none'  窗口内无预告 → 空文本
    说明：预告公告日多为盘后公布，此处按「公告日在提示日当天至其后 ahead 日」匹配，
    便于在提示日即提示「近日将披露业绩预告」的风险。"""
    by_code = by_code if by_code is not None else load_forecast()
    items = by_code.get(str(code).zfill(6), [])
    hit = None
    for r in items:
        dd = _day_diff(r.get("notice", ""), signal_date)
        if 0 <= dd <= ahead:
            if hit is None or r["notice"] < hit["notice"]:
                hit = r
    if hit is None:
        return {"level": "none", "text": "", "notice": "", "type": "", "increase": None}
    typ = hit.get("type") or ""
    inc = hit.get("increase")
    is_bad = any(k in typ for k in _BAD_FORECAST_KW)
    level = "bad" if is_bad else "good"
    inc_s = ("%+d%%" % round(inc)) if isinstance(inc, (int, float)) else "--"
    if is_bad:
        text = '业绩预告[%s] 净利%s (公告日%s) 谨慎' % (typ, inc_s, hit["notice"][5:])
    else:
        text = '业绩预告[%s] 净利%s (公告日%s)' % (typ, inc_s, hit["notice"][5:])
    return {"level": level, "text": text, "notice": hit["notice"],
            "type": typ, "increase": inc}


# ------------------------------------------------------------------ 正式财报披露风险（2026-11-23 用户补充）
# 用户指出：即便没有「业绩预告」，正式财报（一季报/中报/三季报/年报）披露同样会引发暴跌。
# 核对 TOP5 大亏：艾比森 T+1 披露一季报、金凯生科 T+1 披露年报、彩客科技 T 日披露三季报。
# 全样本：T~T+1 有财报的大亏率 15.4% vs 无财报 1.0%（15倍）。数据源 RPT_LICO_FN_CPD。
REPORT_FILE = "领湃_财报披露缓存.json"
_REPORT_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def refresh_reports(year=None, force=False):
    """全量拉取当年披露的正式财报(披露日+净利同比)并缓存。返回 (条数, 是否刷新成功)。"""
    ensure_state()
    year = year or datetime.datetime.now(BJT).year
    cache = state_path(REPORT_FILE)
    today = today_str()
    if not force and os.path.exists(cache):
        try:
            old = json.load(open(cache, encoding="utf-8"))
            if old.get("refreshed") == today and old.get("year") == year:
                return len(old.get("rows", [])), False
        except Exception:
            pass
    import urllib.parse
    rows, page, pages, size = [], 1, 1, 500
    flt = "(NOTICE_DATE>='%d-01-01')(NOTICE_DATE<='%d-12-31')" % (year, year)
    while page <= pages:
        q = urllib.parse.urlencode({
            "sortColumns": "NOTICE_DATE", "sortTypes": -1,
            "pageSize": size, "pageNumber": page,
            "reportName": "RPT_LICO_FN_CPD", "columns": "ALL",
            "source": "WEB", "client": "WEB", "filter": flt,
        })
        d = http_json(_REPORT_URL + "?" + q, retry=2, timeout=20)
        if not d or not d.get("result"):
            if page == 1:
                return 0, False
            break
        for r in d["result"].get("data") or []:
            rows.append({
                "code": r.get("SECURITY_CODE"),
                "notice": (r.get("NOTICE_DATE") or "")[:10],
                "report": (r.get("REPORTDATE") or "")[:10],
                "period": r.get("DATEMMDD"),
                "sjl": r.get("SJLTZ"),   # 净利润同比%
                "ystz": r.get("YSTZ"),   # 营收同比%
            })
        pages = d["result"].get("pages", 1)
        page += 1
        time.sleep(0.08)
    json.dump({"refreshed": today, "year": year, "rows": rows},
              open(cache, "w", encoding="utf-8"), ensure_ascii=False)
    return len(rows), True


def load_reports():
    cache = state_path(REPORT_FILE)
    if not os.path.exists(cache):
        return {}
    try:
        d = json.load(open(cache, encoding="utf-8"))
    except Exception:
        return {}
    by_code = {}
    for r in d.get("rows", []):
        c = r.get("code")
        if c:
            by_code.setdefault(c, []).append(r)
    return by_code


def report_risk(code, signal_date, by_code=None, ahead=3):
    """判断某股在提示日 [T, T+ahead] 是否有正式财报披露，返回风险备注。
    level: 'bad' 净利同比<0（尤其<-50%）红色；'good' 净利同比>=30% 绿色；'flat' 小幅/未知；'none' 无。"""
    by_code = by_code if by_code is not None else load_reports()
    items = by_code.get(str(code).zfill(6), [])
    hit = None
    for r in items:
        dd = _day_diff(r.get("notice", ""), signal_date)
        if 0 <= dd <= ahead:
            if hit is None or (r["notice"] < hit["notice"]):
                hit = r
    if hit is None:
        return {"level": "none", "text": "", "notice": "", "sjl": None, "period": ""}
    sjl = hit.get("sjl")
    period = hit.get("period") or "财报"
    try:
        sjlf = float(sjl)
    except (TypeError, ValueError):
        sjlf = None
    tag = "T日" if hit["notice"] == signal_date else ("T+%d" % _day_diff(hit["notice"], signal_date))
    if sjlf is not None and sjlf < -50:
        level = "bad"
        text = "正式财报[%s] 净利同比%+.0f%% (%s披露) 谨慎" % (period, sjlf, tag)
    elif sjlf is not None and sjlf < 0:
        level = "bad"
        text = "正式财报[%s] 净利同比%+.0f%% (%s披露) 谨慎" % (period, sjlf, tag)
    elif sjlf is not None and sjlf >= 30:
        level = "good"
        text = "正式财报[%s] 净利同比%+.0f%% (%s披露)" % (period, sjlf, tag)
    else:
        level = "flat"
        sjs = ("%+.0f%%" % sjlf) if sjlf is not None else "--"
        text = "正式财报[%s] 净利同比%s (%s披露)" % (period, sjs, tag)
    return {"level": level, "text": text, "notice": hit["notice"], "sjl": sjl, "period": period}


def event_risk(code, signal_date, fc_by_code=None, rp_by_code=None, ahead=3):
    """统一事件风险：正式财报优先，其次业绩预告，都没有则 none。
    返回 {'level','text','src'}；src='report'|'forecast'|''。"""
    rp = report_risk(code, signal_date, rp_by_code, ahead=ahead)
    if rp["level"] != "none":
        return {"level": rp["level"], "text": rp["text"], "src": "report"}
    fc = forecast_risk(code, signal_date, fc_by_code, ahead=ahead)
    if fc["level"] != "none":
        return {"level": fc["level"], "text": fc["text"], "src": "forecast"}
    return {"level": "none", "text": "", "src": ""}


# ------------------------------------------------------------------ K线兜底（2026-11-24 修复 11-23 停摆）
# 事故复盘：11-23 东方财富 K 线接口在 GitHub runner 上整体 Connection aborted，
#   em_hist 只走东财 → 基准探测返回 None → 每日脚本早退、当天零推送；而统一流水线同日多次触发互相取消，
#   23 点兜底又只认「独立流程已派发」不认「实际推送成功」，最终无人补跑。
#   修复：统一入口 hist_with_fallback = 东财 → 新浪 → 腾讯，任何一层成功即返回。
def sina_hist(code, count=220):
    """新浪日线兜底，返回 [{d,o,c,h,l,v}]（成交量股数转手，与东财「手」口径一致）。"""
    import re
    secid = ("sh" + str(code)) if str(code).startswith("6") else (
        ("bj" + str(code)) if str(code).startswith(("4", "8", "9")) else ("sz" + str(code)))
    url = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/CN_MarketDataService.getKLineData"
           "?symbol=" + secid + "&scale=240&ma=no&datalen=" + str(count))
    try:
        r = SESSION.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"})
        m = re.search(r"\((\[.*\])\)", r.text, re.S)
        if m:
            bars = [{"d": str(d["day"]), "o": float(d["open"]), "c": float(d["close"]),
                     "h": float(d["high"]), "l": float(d["low"]),
                     "v": float(d["volume"]) / 100.0} for d in json.loads(m.group(1))]
            return bars or None
    except Exception:
        pass
    return None


def tx_hist(code, count=220):
    """腾讯日线兜底，返回 [{d,o,c,h,l,v}]（成交量手）。"""
    sym = (("sh" if str(code).startswith("6") else
            ("bj" if str(code).startswith(("4", "8", "9")) else "sz")) + str(code))
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           "?param=%s,day,,,%d,qfq" % (sym, count))
    try:
        j = http_json(url, retry=1, timeout=12)
        data = ((j or {}).get("data") or {}).get(sym) or {}
        rows = data.get("qfqday") or data.get("day") or []
        bars = []
        for p in rows:
            if len(p) < 6:
                continue
            try:
                bars.append({"d": str(p[0]), "o": float(p[1]), "c": float(p[2]),
                             "h": float(p[3]), "l": float(p[4]), "v": float(p[5])})
            except (ValueError, TypeError):
                continue
        return bars or None
    except Exception:
        return None


_EM_BROKEN = {"v": False}   # 东财熔断：一旦探测失败，本次进程内后续扫描直接走新浪，避免每只重试3host超时


def mark_em_broken():
    _EM_BROKEN["v"] = True


def hist_with_fallback(code, count=180):
    """K线统一入口：东财 → 新浪 → 腾讯，任一成功即返回 bars，全失败返回 None。
    东财熔断后置位（mark_em_broken）时跳过东财，直接新浪→腾讯，防止全市场扫描每只卡 36s。"""
    if not _EM_BROKEN["v"]:
        bars = em_hist(code, count)
        if bars:
            return bars
    bars = sina_hist(code, max(count, 180))
    if bars:
        return bars
    bars = tx_hist(code, max(count, 180))
    if bars:
        return bars
    return None
