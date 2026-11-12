#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
common_lib.py —— 多模型共享的「共同前置层」
========================================================================
职责（仅做各模型都重复、且不应各自实现的部分）：
  1) 全市场股票列表（东方财富，覆盖 沪/深主板 + 创业板 + 科创板 + 北交所）。
  2) 硬共识合规剔除：ST / *ST / 退市 / 退 / PT / 次新(N) / 无有效行情(price<=0)。
     注意：价格下限、流动性下限、立信审计等「模型专属门槛」不在此层，
     由各模型自己保留，避免改变任何模型的选股结果。
  3) 交易日 / 行情更新判定（取若干样本 K 线最新日，与北京时间当日比较）。
  4) 一次性批量缓存全市场 K 线到 common/state/klines/<code>.csv，
     下游模型读缓存而非各自再拉一遍（这是合并流水线提速的关键）。
  5) load_universe() / load_kline() —— 下游模型读取共享结果的入口。

下游模型只改「数据入口」，模型自身的指标计算、形态判定、门槛、推送一律不动。
"""
import os
import sys
import csv
import json
import time
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests

# ------------------------------------------------------------------ 路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(BASE_DIR, "state")
KLINE_DIR = os.path.join(STATE_DIR, "klines")
UNIVERSE_JSON = os.path.join(STATE_DIR, "universe.json")
META_JSON = os.path.join(STATE_DIR, "meta.json")
LIXIN_JSON = os.path.join(STATE_DIR, "lixin_exclude.json")
os.makedirs(KLINE_DIR, exist_ok=True)


def load_lixin_exclude():
    """读取共同层维护的「立信审计排除集」代码集合（由 lixin.py 生成）。"""
    if not os.path.exists(LIXIN_JSON):
        return set()
    try:
        with open(LIXIN_JSON, "r", encoding="utf-8") as fh:
            return set(json.load(fh).get("codes", []))
    except Exception:
        return set()

# ------------------------------------------------------------------ 常量（与已验证的东财口径一致）
BJT = timezone(timedelta(hours=8))

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://quote.eastmoney.com/",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

EM_KLINE_HOSTS = [
    "push2his.eastmoney.com", "88.push2his.eastmoney.com", "22.push2his.eastmoney.com",
    "33.push2his.eastmoney.com", "48.push2his.eastmoney.com", "72.push2his.eastmoney.com",
    "1.push2his.eastmoney.com", "7.push2his.eastmoney.com",
]
EM_LIST_HOSTS = [
    "push2.eastmoney.com", "82.push2.eastmoney.com", "22.push2.eastmoney.com",
    "33.push2his.eastmoney.com", "48.push2his.eastmoney.com", "72.push2his.eastmoney.com",
    "1.push2.eastmoney.com",
]
EM_UT = "bd1d9ddb04089700cf9c27f6f7426281"   # 列表接口专用 ut（已验证有效）
EM_KUT = "fa5fd1943c7b386f172d6893dbfba10b"  # K线接口 ut

# 全 A：沪深主板 + 创业板 + 科创板 + 北交所
EM_BOARDS = [
    ("沪市主板", "m:1+t:2"),
    ("深市主板", "m:0+t:6"),
    ("创业板", "m:0+t:80"),
    ("科创板", "m:1+t:23"),
    ("北交所", "m:0+t:81+s:2048"),
]

# 全市场 K 线缓存统一抓取的根数（覆盖各模型最大需求：领湃 180 / low_shadow 150）
KLINE_CACHE_COUNT = 220
# 批量拉取并发数
KLINE_WORKERS = 24

_lock = threading.Lock()


# ------------------------------------------------------------------ 通用工具
def http_json(url, params=None, timeout=15, retry=2, referer=None):
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


def secid_of(code):
    code = str(code).zfill(6)
    return ("1." if code.startswith("6") else "0.") + code


def board_of(code):
    """按代码前缀判定板块（与东财板块划分一致）。"""
    code = str(code).zfill(6)
    if code.startswith("6"):
        return "沪市主板"
    if code.startswith(("8", "4", "92")):
        return "北交所"
    if code.startswith(("688", "689")):
        return "科创板"
    if code.startswith(("300", "301", "302")):
        return "创业板"
    return "深市主板"


def _is_hard_excluded(name):
    """硬共识剔除：ST/*ST/退市/退/PT/次新(N)。名字层面判定，不触及财务/审计。"""
    if not name:
        return True
    n = name.replace(" ", "").upper()
    if "ST" in n or "退" in n or "PT" in n:
        return True
    if n.startswith("N") or n.startswith("*"):
        return True
    return False


# ------------------------------------------------------------------ 全市场列表 + 硬剔除
# 基础股票池快照（提交进仓库，跨 runner 持久化）：东财 clist 列表接口对云 IP
# 偶发阻断/限流，实时拉取失败时回退此快照，避免整条流水线崩溃。
BASE_UNIVERSE_JSON = os.path.join(STATE_DIR, "base_universe.json")
# 全A 剔除 ST/立信后正常应 >3000；低于此值视为接口拉取失败，启用兜底/重试。
UNIVERSE_SANITY_MIN = 1500


def _fetch_raw_universe(attempts=3, backoff=20):
    """从东财 clist 拉全市场原始股票列表（code/name/board），不含任何剔除。

    对「云 IP 偶发阻断/限流」做多层退避：单 host 失败重试(retry=2) + 整轮
    拉取偏少时退避重跑。返回原始 rows；全部失败返回 []。
    """
    fields = "f2,f3,f5,f6,f8,f10,f12,f13,f14,f15,f16,f17,f18,f20,f21"
    for attempt in range(attempts + 1):
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
                    j = http_json("https://%s/api/qt/clist/get" % host, params=params,
                                  timeout=15, retry=2)
                    data = (j or {}).get("data") or {}
                    if data.get("diff"):
                        break
                    time.sleep(0.3)
                diff = data.get("diff") or []
                if isinstance(diff, dict):
                    diff = list(diff.values())
                if not diff:
                    break
                total = int(float(data.get("total") or 0))
                for x in diff:
                    code = str(x.get("f12", "")).zfill(6)
                    price = _to_float(x.get("f2"))
                    nm = x.get("f14", "") or ""
                    if not code or price <= 0:
                        continue
                    rows.append({"code": code, "name": nm, "board": name})
                if len(diff) < 100 or (total and pn * 100 >= total):
                    break
                pn += 1
                time.sleep(0.2)
        if len(rows) >= UNIVERSE_SANITY_MIN:
            return rows
        if attempt < attempts:
            print("[common] 全市场列表仅拉到 %d 只（疑似接口限流/阻断），%ds 后重试（%d/%d）"
                  % (len(rows), backoff, attempt + 1, attempts))
            time.sleep(backoff)
    return rows


def save_base_universe(rows):
    """把成功拉到的原始股票列表落盘为快照（下次接口失效时兜底）。"""
    try:
        with open(BASE_UNIVERSE_JSON, "w", encoding="utf-8") as fh:
            json.dump({"generated_at": datetime.now(BJT).isoformat(timespec="seconds"),
                       "count": len(rows), "rows": rows}, fh, ensure_ascii=False)
    except Exception:
        pass


def load_base_universe():
    if not os.path.exists(BASE_UNIVERSE_JSON):
        return []
    try:
        with open(BASE_UNIVERSE_JSON, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return [r for r in (d.get("rows") or []) if r.get("code")]
    except Exception:
        return []


def get_universe(verbose=True, apply_lixin=True):
    """东方财富全市场列表，应用硬共识剔除，返回统一结构的样本池。

    apply_lixin=False 时只做 ST/次新/无行情 剔除，用于「立信排除集生成」阶段
    拿到全市场基准代码（避免生成排除集时又引用排除集本身）。

    返回字段：code, name, board, price, pct(涨跌幅%), turnover(换手%),
             mktcap(总市值), float_mktcap(流通市值), vr(量比)。
    board 用于下游按板块过滤（如易点仅创/科/北交）。

    韧性：优先实时拉取（多层退避重试）；若实时结果异常偏少（接口偶发阻断），
    回退已提交的 base_universe.json 快照，保证流水线不因单一接口抖动而整体
    崩溃。实时成功时自动刷新快照。
    """
    lixin = load_lixin_exclude() if apply_lixin else set()
    st_drop = lixin_drop = 0

    raw = _fetch_raw_universe()
    used_fallback = False
    if len(raw) < UNIVERSE_SANITY_MIN:
        snap = load_base_universe()
        if snap:
            print("[common] 实时列表异常（%d 只），回退基础股票池快照 %d 只"
                  % (len(raw), len(snap)))
            raw = snap
            used_fallback = True
        else:
            print("[common] 实时列表异常且无快照，样本池为空。")
            if verbose:
                print("[common] 全市场样本池：0 只（ST/次新剔除 0，立信剔除 0）")
            return []
    # 实时成功（非兜底）且结果完整 → 刷新快照，供后续接口失效时兜底
    if not used_fallback and len(raw) >= UNIVERSE_SANITY_MIN:
        save_base_universe(raw)

    rows, seen = [], set()
    for r in raw:
        code = str(r.get("code", "")).zfill(6)
        nm = r.get("name", "") or ""
        if code in seen:
            continue
        seen.add(code)
        if _is_hard_excluded(nm):   # 条件③：ST/退市/次新 硬剔除
            st_drop += 1
            continue
        if code in lixin:            # 条件②：立信审计排除集 物理剔除
            lixin_drop += 1
            continue
        rows.append({
            "code": code, "name": nm, "board": r.get("board") or board_of(code),
            "price": _to_float(r.get("f2")),
            "pct": _to_float(r.get("f3")),
            "turnover": _to_float(r.get("f8")),
            "volume": _to_float(r.get("f5")),     # 成交量（手）
            "amount": _to_float(r.get("f6")),     # 成交额（元）
            "prev_close": _to_float(r.get("f18")),
            "mktcap": _to_float(r.get("f20")),
            "float_mktcap": _to_float(r.get("f21")),
            "vr": _to_float(r.get("f10")),
        })
    if verbose:
        print("[common] 全市场样本池：%d 只（ST/次新剔除 %d，立信剔除 %d）"
              % (len(rows), st_drop, lixin_drop))
    return rows


def _to_float(v, default=0.0):
    try:
        if v in ("-", "", None):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ 交易日 / 行情判定
def latest_quote_date(sample_codes=("600519", "000001", "300750", "688981")):
    """取若干样本股票 K 线的最新交易日（北京时间）。"""
    dates = []
    for code in sample_codes:
        for attempt in range(3):
            try:
                bars = _fetch_one_kline(code, count=8)
                if bars:
                    dates.append(bars[-1]["date"])
                    break
            except Exception:
                time.sleep(0.3)
    return max(dates) if dates else None


def check_trading_day(verbose=True):
    """行情最新日 == 北京时间当日 → 交易日；否则非交易日/行情未更新。"""
    qd = latest_quote_date()
    today = datetime.now(BJT).strftime("%Y-%m-%d")
    if verbose:
        print("[common] 行情最新日=%s  北京时间当日=%s" % (qd, today))
    if not qd:
        return False, qd, today
    return (qd == today), qd, today


# ------------------------------------------------------------------ K 线抓取与缓存
def _fetch_one_kline(code, count=KLINE_CACHE_COUNT, fqt=1):
    """从东财抓取单只 K 线，返回规范 bars 或 None。

    fqt=1 前复权（共享主缓存，供 善水/低位上影/易点/领湃）；
    fqt=0 不复权（供凯众：与新浪日线逐日一致，成交量单位为「手」）。
    """
    beg = (datetime.now(BJT).date() - timedelta(days=count * 3)).strftime("%Y%m%d")
    params = {
        "secid": secid_of(code), "ut": EM_KUT,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": 101, "fqt": fqt, "beg": beg, "end": "20500101", "lmt": count,
    }
    hosts = EM_KLINE_HOSTS[:]
    random.shuffle(hosts)
    for host in hosts[:4]:
        j = http_json("https://%s/api/qt/stock/kline/get" % host, params=params,
                      timeout=12, retry=1)
        rows = ((j or {}).get("data") or {}).get("klines") or []
        bars = []
        for s in rows:
            p = s.split(",")
            if len(p) < 11:
                continue
            try:
                bars.append({
                    "date": p[0],
                    "open": float(p[1]), "close": float(p[2]),
                    "high": float(p[3]), "low": float(p[4]),
                    "volume": float(p[5]),
                    "turnover": _to_float(p[10]),
                })
            except ValueError:
                continue
        if bars:
            return bars
        time.sleep(0.12)
    return None


def _kline_csv_path(code):
    return os.path.join(KLINE_DIR, "%s.csv" % str(code).zfill(6))


def _write_kline_csv(code, bars):
    path = _kline_csv_path(code)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "open", "high", "low", "close", "volume", "turnover"])
        for b in bars:
            w.writerow([b["date"], b["open"], b["high"], b["low"],
                        b["close"], b["volume"], b["turnover"]])


def _read_kline_csv(code, count=None):
    path = _kline_csv_path(code)
    if not os.path.exists(path):
        return None
    bars = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                bars.append({
                    "date": row["date"], "open": float(row["open"]),
                    "high": float(row["high"]), "low": float(row["low"]),
                    "close": float(row["close"]), "volume": float(row["volume"]),
                    "turnover": _to_float(row.get("turnover")),
                })
    except Exception:
        return None
    if not bars:
        return None
    bars.sort(key=lambda x: x["date"])
    if count:
        bars = bars[-count:]
    return bars


def fetch_klines_batch(universe, count=KLINE_CACHE_COUNT, workers=KLINE_WORKERS,
                       skip_existing=True, verbose=True):
    """批量把全市场 K 线缓存到本地（共享给下游模型）。已存在且足够的跳过。

    返回 (成功数, 失败数)。
    """
    targets = []
    for u in universe:
        code = u["code"]
        path = _kline_csv_path(code)
        if skip_existing and os.path.exists(path) and os.path.getsize(path) > 0:
            continue
        targets.append(code)

    if verbose:
        print("[common] K线缓存：需新拉 %d 只（已缓存跳过 %d 只）"
              % (len(targets), len(universe) - len(targets)))

    ok = fail = 0
    done = 0
    total = len(targets)

    def work(code):
        bars = _fetch_one_kline(code, count=count)
        if bars:
            _write_kline_csv(code, bars)
            return True
        return False

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(work, c): c for c in targets}
        for fut in as_completed(futs):
            try:
                if fut.result():
                    with _lock:
                        ok += 1
                else:
                    with _lock:
                        fail += 1
            except Exception:
                with _lock:
                    fail += 1
            done += 1
            if verbose and done % 200 == 0:
                print("[common] K线缓存进度 %d/%d  成功 %d 失败 %d"
                      % (done, total, ok, fail))
    if verbose:
        print("[common] K线缓存完成：成功 %d，失败 %d" % (ok, fail))
    return ok, fail


# ------------------------------------------------------------------ 凯众专用：不复权 K 线共享缓存
KAIZHONG_MIN_PRICE = 3.0      # 与 kaizhong/lib.py MIN_PRICE 一致
KAIZHONG_MIN_AMOUNT = 5e7     # 与 kaizhong/lib.py MIN_AMOUNT 一致
KAIZHONG_LOOKBACK = 180       # 与 kaizhong/lib.py LOOKBACK 一致


def kaizhong_kline_dir():
    """凯众 K 线缓存目录：与 kaizhong/lib.py 的 KZ_DATA_DIR 口径严格一致。"""
    repo = os.path.dirname(BASE_DIR)
    env = os.environ.get("KZ_DATA_DIR") or os.path.join("kaizhong", "state")
    base = env if os.path.isabs(env) else os.path.join(repo, env)
    return os.path.join(base, "kline")


def _csv_last_date(path):
    """读 CSV 最后一根 K 线的日期（不加载整表）。失败返回 None。"""
    try:
        last = None
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("date"):
                    last = line.split(",")[0]
        return last
    except Exception:
        return None


def cache_kaizhong_klines(universe, count=KAIZHONG_LOOKBACK,
                          workers=KLINE_WORKERS, quote_date=None, verbose=True):
    """为凯众预填「不复权」K 线 CSV，使其 update_cache 零网络抓取。

    实测依据（2026-11-10，180 根逐日比对 6 只样本）：
      · 东财 fqt=0（不复权）与新浪日线价格 0 差异；fqt=1（前复权）在除权日
        前后有价差，故凯众必须用 fqt=0，不能直连共享主缓存。
      · 成交量：新浪(股)/东财(手) 恒为 100 倍，此处 ×100 还原；凯众量能判定
        用的是相对倍数，乘常数不影响任何结果。
      · 范围：仅凯众原本会扫描的股票（非北交所 + 价格/成交额/成交量门槛），
        且样本池已物理剔除 ST/立信，符合全局强制条件。
      · 格式：date,open,high,low,close,volume，与新浪 fetch_kline 完全一致。

    关键：按「末日 == 最新交易日」判定是否跳过，而非「文件存在即跳过」——
    GitHub Actions 全新 runner 会 checkout 到仓库里的旧缓存，必须重写为当日，
    否则凯众 update_cache 判定末日过期仍会全量重抓（这正是原先慢的根源）。

    返回 (成功数, 失败数)。失败个股由凯众自行回退新浪抓取，不影响正确性。
    """
    if quote_date is None:
        _, quote_date, _ = check_trading_day()
    d = kaizhong_kline_dir()
    os.makedirs(d, exist_ok=True)
    targets = []
    fresh = 0
    for x in universe or []:
        code = str(x.get("code") or "").zfill(6)
        # 范围 = 样本池内全部非北交所：不加凯众自己的价格/成交额门槛。
        # 因为凯众 scan_and_matrix 遍历「目录下所有 CSV」而非仅 items，若在此
        # 预筛会缩小扫描集合、改变选股结果。样本池已物理剔除 ST/立信/无行情，
        # 属全局强制条件；凯众自身的门槛仍由它自己在 scan 阶段判定，逻辑不变。
        if not code or code == "000000" or x.get("board") == "北交所":
            continue
        p = os.path.join(d, code + ".csv")
        if os.path.exists(p) and os.path.getsize(p) > 0 and \
                _csv_last_date(p) == quote_date:
            fresh += 1
            continue
        targets.append(code)

    if verbose:
        print("[common] 凯众不复权K线：需预填 %d 只（已是当日 %d 只）→ %s"
              % (len(targets), fresh, d))
    ok = fail = 0

    def work(code):
        bars = _fetch_one_kline(code, count=count, fqt=0)
        if not bars or len(bars) < 6:
            return False
        p = os.path.join(d, code + ".csv")
        with open(p, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["date", "open", "high", "low", "close", "volume"])
            for b in bars:
                w.writerow([b["date"], b["open"], b["high"], b["low"],
                            b["close"], b["volume"] * 100.0])
        return True

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(work, targets):
            if r:
                ok += 1
            else:
                fail += 1
    if verbose:
        print("[common] 凯众不复权K线完成：成功 %d，失败 %d（失败者凯众自行回退）" % (ok, fail))
    return ok, fail


# ------------------------------------------------------------------ 下游读取入口
def load_universe():
    """读取预筛产出的样本池（universe.json）。"""
    if not os.path.exists(UNIVERSE_JSON):
        return None
    try:
        with open(UNIVERSE_JSON, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def load_kline(code, count=None):
    """读取共享 K 线缓存（规范格式）；缺失返回 None（下游可回退自有接口）。"""
    return _read_kline_csv(code, count=count)


def save_universe(universe):
    with open(UNIVERSE_JSON, "w", encoding="utf-8") as fh:
        json.dump(universe, fh, ensure_ascii=False, indent=0)


def save_meta(meta):
    with open(META_JSON, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)


def load_meta():
    if not os.path.exists(META_JSON):
        return None
    try:
        with open(META_JSON, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None
