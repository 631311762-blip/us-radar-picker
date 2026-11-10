# -*- coding: utf-8 -*-
"""
凯众股份(603037)相似形态选股 —— 公共库
数据源：新浪财经(日线/快照) + 东方财富数据中心(F10 审计机构/题材/行业)
策略：SCHEME='breakout' —— 只做"放量突破"买点，回踩区间仅作形态失效参考线
"""
import os
import json
import time
import math
import requests
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(BASE)   # 仓库根目录
# 数据目录可用 KZ_DATA_DIR 覆盖（GitHub Actions 中指向 kaizhong/state 以便回写）。
# 解析顺序：绝对路径 → 相对 cwd 存在 → 相对仓库根存在 → 相对脚本目录存在。
# 之所以不只按 cwd 解析：当 workflow 设了 working-directory: kaizhong、
# 同时又传相对路径 kaizhong/state 时，会被拼成 kaizhong/kaizhong/state，
# 静默读到一个空目录（历史台账「凭空消失」）。这里做多级回退，消除该歧义。
def _resolve_data_dir():
    raw = os.environ.get("KZ_DATA_DIR")
    if not raw:
        return os.path.join(BASE, "data")
    if os.path.isabs(raw):
        return os.path.abspath(raw)
    for base in (os.getcwd(), _REPO, BASE):
        cand = os.path.abspath(os.path.join(base, raw))
        if os.path.isdir(cand):
            return cand
    return os.path.abspath(os.path.join(_REPO, raw))


DATA = _resolve_data_dir()
KLINE_DIR = os.path.join(DATA, "kline")
REPORT_DIR = os.path.join(DATA, "reports")
F10_CACHE = os.path.join(DATA, "f10.json")
HISTORY = os.path.join(DATA, "history.json")

for p in (DATA, KLINE_DIR, REPORT_DIR):
    os.makedirs(p, exist_ok=True)

# ---------------- 策略常量（勿随意修改） ----------------
SCHEME = 'breakout'          # 只做放量突破买点
BUY_P = {
    'start_chg': 5.0,        # 启动K线最小涨幅(%)
    'start_vol_x': 2.0,      # 启动K线量能 ≥ 前20日均量倍数
    'cons_min': 3,           # 整理期最少交易日
    'cons_max': 20,          # 整理期最多交易日
    'vol_shrink': 0.95,      # 整理期均量 ≤ 启动日均量 × 该系数
    'proximity': 0.94,       # 现价 ≥ 平台上沿 × 该系数（贴近压力位）
    'breakout_buf': 1.005,   # 触发价 = 平台上沿 × 该系数
    'vol_ratio': 1.8,        # 突破所需成交量 ≥ 近5日均量 × 该系数
    'stop_loss': 0.05,       # 单笔风险上限 5%
    'max_wait_days': 5,      # 未触发单子 5 个交易日自动作废
    'pre_range': 1.35,       # 启动前30日高低幅度上限（底部横盘蓄势）
    'max_extend': 1.60,      # 现价 / 近60日最低 上限（避免过度透支）
}
MIN_AMOUNT = 5e7             # 近20日日均成交额下限
MIN_PRICE = 3.0              # 股价下限
LOOKBACK = 180               # 缓存日线根数

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"}
SINA_REF = {"User-Agent": UA["User-Agent"], "Referer": "https://finance.sina.com.cn"}


try:
    import urllib3
    urllib3.disable_warnings()
except Exception:
    pass

_CA = "/etc/ssl/certs/ca-certificates.crt"
_VERIFY = _CA if os.path.exists(_CA) else True


def _get(url, headers=None, timeout=20, retries=3):
    global _VERIFY
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=headers or UA, timeout=timeout, verify=_VERIFY)
            if r.status_code == 200 and r.text.strip():
                return r
            last = f"HTTP {r.status_code}"
        except Exception as e:
            last = str(e)
            if "CERTIFICATE" in str(e).upper() and _VERIFY is not False:
                _VERIFY = False          # 沙箱代理自签证书场景下降级
        time.sleep(0.6 * (i + 1))
    raise RuntimeError(f"请求失败: {url[:80]} ({last})")


# ---------------- 全市场快照 ----------------
def fetch_spot_all():
    """新浪全市场 A 股快照"""
    rows, page = [], 1
    while True:
        url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               f"Market_Center.getHQNodeData?page={page}&num=100&sort=symbol&asc=1&node=hs_a")
        try:
            txt = _get(url, SINA_REF).text
            data = json.loads(txt) if txt.strip().startswith("[") else []
        except Exception:
            data = []
        if not data:
            break
        rows.extend(data)
        if len(data) < 100:
            break
        page += 1
        if page > 80:
            break
    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError("快照抓取为空")
    for c in ("trade", "volume", "amount", "changepercent", "turnoverratio", "mktcap", "nmc", "per", "pb"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


# ---------------- 日线抓取与缓存 ----------------
def _kline_file(code):
    return os.path.join(KLINE_DIR, f"{code}.csv")


def fetch_kline(symbol):
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen={LOOKBACK}")
    r = _get(url, SINA_REF, timeout=15)
    txt = r.text.strip()
    if not txt.startswith("["):
        return None
    data = json.loads(txt)
    if not data:
        return None
    df = pd.DataFrame(data)
    df = df.rename(columns={"day": "date"})
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[["date", "open", "high", "low", "close", "volume"]].dropna()
    return df


def load_kline(code, symbol):
    f = _kline_file(code)
    if os.path.exists(f):
        try:
            df = pd.read_csv(f)
            if len(df) > 5:
                return df
        except Exception:
            pass
    return None


def save_kline(code, df):
    df.to_csv(_kline_file(code), index=False)


def update_cache(codes_symbols, workers=16, force=False, last_trade_date=None):
    """增量更新日线缓存：缓存末日 != 最新交易日 的股票才重新抓取"""
    todo = []
    for code, symbol in codes_symbols:
        df = load_kline(code, symbol)
        if force or df is None or (last_trade_date and str(df["date"].iloc[-1]) != last_trade_date):
            todo.append((code, symbol))
    if not todo:
        return 0, 0

    def work(item):
        code, symbol = item
        try:
            df = fetch_kline(symbol)
            if df is not None and len(df) > 20:
                save_kline(code, df)
                return code, True
        except Exception:
            pass
        return code, False

    ok, fail = 0, 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(work, it) for it in todo]
        for fu in as_completed(futs):
            _, s = fu.result()
            ok += s
            fail += (not s)
    return ok, fail


def market_last_date(df_snap_peek=None):
    """探测当前最新交易日（用凯众股份做探针）；传入 before 则取该日期之前最近一个交易日"""
    df = fetch_kline("sh603037")
    d = df["date"].astype(str)
    if df_snap_peek:
        d = d[d < str(df_snap_peek)]
        if len(d) == 0:
            return None
    return str(d.iloc[-1])


# ---------------- F10：审计机构 / 题材 / 行业 ----------------
def load_f10(refresh_days=7):
    if os.path.exists(F10_CACHE):
        age = (time.time() - os.path.getmtime(F10_CACHE)) / 86400.0
        if age < refresh_days:
            try:
                return json.load(open(F10_CACHE, encoding="utf-8"))
            except Exception:
                pass
    out = {}
    page = 1
    while page <= 60:
        url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_F10_ORG_BASICINFO"
               "&columns=SECURITY_CODE,SECURITY_NAME_ABBR,ACCOUNT_FIRM,BLGAINIAN,EM2016,TRADE_MARKET"
               f"&pageSize=500&pageNumber={page}&sortColumns=SECURITY_CODE&sortTypes=1")
        try:
            js = _get(url, timeout=25).json()
            res = (js or {}).get("result") or {}
            rows = res.get("data") or []
        except Exception:
            rows = []
        if not rows:
            break
        for r in rows:
            out[str(r.get("SECURITY_CODE"))] = {
                "name": r.get("SECURITY_NAME_ABBR"),
                "firm": r.get("ACCOUNT_FIRM") or "",
                "concepts": r.get("BLGAINIAN") or "",
                "industry": r.get("EM2016") or "",
                "market": r.get("TRADE_MARKET") or "",
            }
        if page >= int(res.get("pages") or 1):
            break
        page += 1
    if out:
        json.dump(out, open(F10_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    return out


def blocked_by_rule(name, code, f10_row):
    """合规剔除：ST/退市风险 与 指定审计机构（静默处理，不记录原因）"""
    n = (name or "").upper().replace(" ", "")
    if ("ST" in n) or ("退" in n) or ("退市" in n):
        return True
    firm = (f10_row or {}).get("firm", "") or ""
    if "立信" in firm:
        return True
    return False


# ---------------- 形态识别：凯众式底部蓄势 + 放量突破 ----------------
def analyze(code, name, df, f10_row):
    if df is None or len(df) < 70:
        return None
    df = df.tail(120).reset_index(drop=True)
    c = df["close"].values
    v = df["volume"].values
    h = df["high"].values
    l = df["low"].values
    o = df["open"].values
    dates = df["date"].astype(str).values
    n = len(df)

    last_close = c[-1]
    if last_close < MIN_PRICE:
        return None
    # 流动性
    amount20 = float((c[-20:] * v[-20:]).mean())
    if amount20 < MIN_AMOUNT:
        return None
    ma5 = float(c[-5:].mean())
    ma20 = float(c[-20:].mean())
    ma60 = float(c[-60:].mean())

    best = None
    # 启动K线：近 60 日内，涨幅达标 + 放量达标
    for i in range(n - 60, n - 2):
        if i < 21:
            continue
        chg = (c[i] / c[i - 1] - 1) * 100
        if chg < BUY_P["start_chg"]:
            continue
        base_v = float(v[i - 20:i].mean())
        if base_v <= 0:
            continue
        vr = v[i] / base_v
        if vr < BUY_P["start_vol_x"]:
            continue
        cons = n - 1 - i            # 启动后的整理天数
        if cons < BUY_P["cons_min"] or cons > BUY_P["cons_max"]:
            continue
        seg = slice(i + 1, n)
        p_top = float(h[i + 1:].max())          # 平台上沿（压力位）
        cons_v = float(v[i + 1:].mean())
        if cons_v > v[i] * BUY_P["vol_shrink"]:
            continue
        # 形态失效线：启动K线实体中位（回踩参考，非买点）
        invalid_line = float((o[i] + c[i]) / 2)
        if float(l[i + 1:].min()) < invalid_line:
            continue
        if last_close < p_top * BUY_P["proximity"]:
            continue
        if last_close > p_top * 1.12:           # 已大幅脱离平台，不追
            continue
        if last_close < ma20:
            continue
        # 凯众式底部蓄势：启动前 30 日未大幅拉升，且现价未过度透支
        pre = slice(max(0, i - 30), i)
        if float(h[pre].max()) / max(float(l[pre].min()), 0.01) > BUY_P['pre_range']:
            continue
        if last_close / max(float(l[-60:].min()), 0.01) > BUY_P['max_extend']:
            continue
        # 假跌破统计：整理期内某日最低跌破此前箱体低点，且 2 日内收盘收回
        fakebreaks = 0
        cons_lows = l[i + 1:]
        for p in range(3, len(cons_lows)):
            bb = float(cons_lows[:p].min())
            if float(cons_lows[p]) < bb:
                rec = c[i + 1 + p + 1: i + 1 + p + 3]
                if len(rec) > 0 and float(max(rec.max(), c[i + 1 + p])) >= bb:
                    fakebreaks += 1
        # 评分（≥2次假跌破且快速收回 = 箱体下沿多次验证承接，历史胜率 57%、均收益+9.5%，加分排序）
        compact = (float(h[i + 1:].max()) - float(l[i + 1:].min())) / p_top
        prox = last_close / p_top
        shrink = cons_v / v[i]
        power = chg * vr
        trend = ma20 / ma60 - 1
        score = (0.22 * (1 - min(shrink, 1.0))
                 + 0.20 * (1 - min(compact / 0.35, 1.0))
                 + 0.20 * min(prox, 1.0)
                 + 0.20 * min(power / 40.0, 1.0)
                 + 0.18 * max(min(trend / 0.15, 1.0), 0.0)
                 + (0.08 if fakebreaks >= 2 else 0.0))
        cand = dict(
            code=code, name=name, score=score,
            date=str(dates[-1]),
            start_date=str(dates[i]), start_chg=round(float(chg), 2), start_vol_x=round(float(vr), 2),
            days=cons, platform_top=round(p_top, 2),
            trigger=round(p_top * BUY_P["breakout_buf"], 2),
            req_vol=int(max(v[i] * 0.8, float(v[-5:].mean()) * BUY_P["vol_ratio"])),
            req_vol_wan=round(max(v[i] * 0.8, float(v[-5:].mean()) * BUY_P["vol_ratio"]) / 1e6, 2),  # 万手
            stop=round(p_top * BUY_P["breakout_buf"] * (1 - BUY_P["stop_loss"]), 2),
            invalid_line=round(invalid_line, 2),
            last_close=round(float(last_close), 2),
            ma20=round(ma20, 2), ma60=round(ma60, 2),
            amount20_wan=int(amount20 / 1e4),
            turn_shrink=round(float(shrink), 2),
            compact=round(float(compact), 3),
            fakebreaks=fakebreaks,
            industry=(f10_row or {}).get("industry", ""),
            concepts=(f10_row or {}).get("concepts", ""),
        )
        if best is None or cand["score"] > best["score"]:
            best = cand
    return best


def screen(codes_symbols, names, f10, workers=16):
    out = []

    def work(item):
        code, symbol = item
        df = load_kline(code, symbol)
        if df is None or len(df) < 70:
            return None
        row = f10.get(code, {})
        nm = names.get(code) or row.get("name") or code
        if blocked_by_rule(nm, code, row):
            return None
        try:
            return analyze(code, nm, df, row)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for fu in as_completed([ex.submit(work, it) for it in codes_symbols]):
            r = fu.result()
            if r:
                out.append(r)
    out.sort(key=lambda x: -x["score"])
    return out


# ---------------- 历史买点回放 ----------------
def load_history():
    if os.path.exists(HISTORY):
        try:
            return json.load(open(HISTORY, encoding="utf-8"))
        except Exception:
            return []
    return []


def save_history(hist):
    json.dump(hist, open(HISTORY, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def trade_days_between(dates_all, d0, d1):
    try:
        return int(sum(1 for d in dates_all if d0 < str(d) <= d1))
    except Exception:
        return 0


def replay(hist, market_dates, as_of=None):
    """回放历史推荐：触发/等待/作废，并做成败判定。
    as_of 指定回溯基准日（默认不限制）。回溯分析时只使用 as_of 当日及之前的K线，
    对「前一交易日收盘时点」的全部在跟踪样本做快照式评估，避免用到未来数据。"""
    res = []
    as_of = str(as_of) if as_of else None
    for rec in hist:
        if rec.get("status") in ("good", "mid", "bad", "invalid", "expired"):
            continue
        df = load_kline(rec["code"], rec.get("symbol"))
        if df is None:
            continue
        df = df.copy()
        df["date"] = df["date"].astype(str)
        if as_of:
            df = df[df["date"] <= as_of]
            if len(df) == 0:
                continue
        after = df[df["date"] > rec["date"]]
        status, buy_px, buy_date, note = "waiting", None, None, ""
        # 已触发的单子：用触发日之后的K线重新判定止损/收益，避免"当日收绿误判止损"
        if rec.get("status") == "triggered" and rec.get("buy_date"):
            buy_px = rec["buy_price"]
            buy_date = rec["buy_date"]
            status = "triggered"
        else:
            for _, r in after.iterrows():
                if float(r["low"]) <= rec["invalid_line"] and buy_px is None:
                    status = "invalid"
                    note = f'{r["date"]} 跌破形态失效线 {rec["invalid_line"]}'
                    break
                if float(r["close"]) >= rec["trigger"] and float(r["volume"]) >= rec["req_vol"]:
                    buy_px = max(rec["trigger"], float(r["open"]))
                    buy_date = str(r["date"])
                    status = "triggered"
                    break
        if status == "waiting":
            waited = trade_days_between(market_dates, rec["date"], str(df["date"].iloc[-1]))
            if waited >= BUY_P["max_wait_days"]:
                status, note = "expired", f"已等待 {waited} 个交易日未触发"
        elif status == "triggered":
            # 用触发日（含）之后的K线；止损从下一根开始算，触发当日不算止损
            seg = df[df["date"] >= buy_date].iloc[1:] if len(df[df["date"] >= buy_date]) > 1 else df[df["date"] >= buy_date].iloc[0:0]
            cur = float(df["close"].iloc[-1])
            ret = cur / buy_px - 1
            hit_stop = float(seg["low"].min()) <= rec["stop"] if len(seg) else False
            if hit_stop:
                status, note, ret = "bad", f'{buy_date} 触发买入后回落止损', -BUY_P["stop_loss"]
            else:
                status = "good" if ret >= 0.08 else ("mid" if ret >= 0 else "bad")
                note = f'{buy_date} 放量突破触发，买入价 {buy_px:.2f}'
            rec.update(buy_price=round(buy_px, 2), buy_date=buy_date,
                      ret=round(ret * 100, 2), cur=round(cur, 2))
        rec["status"] = status
        rec["note"] = note
        rec["last_check"] = str(df["date"].iloc[-1])
        res.append(rec)
    save_history(hist)
    return res


def detect_today_triggers(hist, last_date):
    """扫描台账中全部 waiting 样本，检查其当日K线是否满足「收盘 ≥ 触发价 且 成交量 ≥ 所需量」。
    返回触发样本列表（带当日信息），用于触发日推送。"""
    fired = []
    for rec in hist:
        if rec.get("status") != "waiting":
            continue
        df = load_kline(rec["code"], rec.get("symbol"))
        if df is None:
            continue
        row = df[df["date"].astype(str) == str(last_date)]
        if row.empty:
            continue
        r = row.iloc[0]
        close, vol, low = float(r["close"]), float(r["volume"]), float(r["low"])
        if low <= rec["invalid_line"]:      # 当日曾跌破失效线则形态失效，不算买点
            continue
        if close >= rec["trigger"] and vol >= rec["req_vol"]:
            rec["today_close"] = close
            rec["today_vol_wan"] = round(vol / 1e6, 2)
            rec["today_chg"] = round((close / float(df["close"].iloc[-2]) - 1) * 100, 2)
            fired.append(rec)
    fired.sort(key=lambda x: -float(x.get("score", 0)))
    return fired


# ---------------- 方糖（Server酱）推送 ----------------
def push_ftqq(title, md):
    key = (os.environ.get("FTQQ_KEY") or os.environ.get("SENDKEY")
           or os.environ.get("SERVERCHAN_KEY") or "").strip()
    cfg = os.path.join(BASE, "config.json")
    if not key and os.path.exists(cfg):
        try:
            key = (json.load(open(cfg, encoding="utf-8")).get("FTQQ_KEY") or "").strip()
        except Exception:
            key = ""
    if not key:
        print("[push] 未配置方糖 SENDKEY（环境变量 FTQQ_KEY 或 config.json），跳过推送")
        return False
    url = f"https://sctapi.ftqq.com/{key}.send"
    try:
        r = requests.post(url, data={"title": title, "desp": md}, timeout=25, verify=_VERIFY)
        ok = r.status_code == 200
        print(f"[push] 方糖推送 {'成功' if ok else '失败'} HTTP {r.status_code}")
        return ok
    except Exception as e:
        print(f"[push] 方糖推送异常: {e}")
        return False
