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
  4) 一次性批量缓存全市场 K 线到 common/state/klines/<code>.csv
     （东财为主，东财对云 IP 阻断时自动回退新浪/腾讯兜底），
     下游模型读缓存而非各自再拉一遍（这是合并流水线提速的关键）。
  5) load_universe() / load_kline() —— 下游模型读取共享结果的入口。

下游模型只改「数据入口」，模型自身的指标计算、形态判定、门槛、推送一律不动。
"""
import os
import sys
import csv
import json
import re
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
# requests.Session 不是线程安全的：多线程共用单例会让连接池复用混乱，
# 表现为随机的 502 / 连接重置。改为每线程独立 Session（thread-local）。
_SESSION_TLS = threading.local()


def _session():
    s = getattr(_SESSION_TLS, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        _SESSION_TLS.session = s
    return s

EM_KLINE_HOSTS = [
    "push2his.eastmoney.com", "88.push2his.eastmoney.com", "22.push2his.eastmoney.com",
    "33.push2his.eastmoney.com", "48.push2his.eastmoney.com", "72.push2his.eastmoney.com",
    "1.push2his.eastmoney.com", "7.push2his.eastmoney.com",
]
EM_LIST_HOSTS = [
    "push2.eastmoney.com", "82.push2.eastmoney.com", "22.push2.eastmoney.com",
    "1.push2.eastmoney.com", "7.push2.eastmoney.com", "15.push2.eastmoney.com",
    "112.push2.eastmoney.com",
]
EM_UT = "bd1d9ddb04089700cf9c27f6f7426281"   # 列表接口专用 ut（已验证有效）
EM_KUT = "fa5fd1943c7b386f172d6893dbfba10b"  # K线接口 ut

# 腾讯财经兜底源：东财对云 IP 偶发整体阻断/限流时，K 线（历史+实时行情）改走腾讯。
# 已验证腾讯可从云环境直连（HTTP 200，<0.1s），历史 K 线字段顺序与东财一致
# [日期,开,收,高,低,成交量(手)]，成交量单位同为「手」，故下游模型无需任何改动。
TX_KLINE_HOST = "web.ifzq.gtimg.cn"      # 历史 K 线（前复权 qfqday / 不复权 day）
TX_QUOTE_HOST = "qt.gtimg.cn"            # 实时行情（批量刷新列表价格）

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
# 批量拉取并发数：云 IP 下 24 并发会直接撞东财 CDN 的 CC 防护，
# 表现为持续性 502（网关回源失败）。降到 4，并同步放宽 KLINE_BATCH_BUDGET，
# 避免吞吐不足导致缓存覆盖率下降、触发下游模型告警。
KLINE_WORKERS = 4

# 批量抓取单请求超时（秒）：云 IP 偶发「挂起」而非「重置」，必须短，否则超时层层
# 叠加会变成无底洞（一只股票最多 超时×重试×host 数 秒，4000 只轻松跑几小时）。
BULK_TIMEOUT = 8
BULK_RETRY = 1


def _env_int(name, default, lo=30, hi=3600):
    """从环境变量读整型配置；缺失/非法时用默认值，并夹在 [lo, hi] 防误配。"""
    try:
        v = int(os.environ.get(name, default))
        return max(lo, min(hi, v))
    except (TypeError, ValueError):
        return default


# 批量任务硬上限（秒）：超时即放弃剩余（K线缓存本就是优化，模型各自会回退重抓）。
# 2026-11-13：可由 workflow 环境变量覆盖（不改代码即可按需调预算）。
# 并发从 24 降到 4 后，300s 不足以覆盖 4600+ 只，故默认放宽到 900s；
# 仍远小于 180 分钟 job 预算，且网关错误快速失败 + 早熔断会提前终止东财链路。
KLINE_BATCH_BUDGET = _env_int("KLINE_BATCH_BUDGET", 900)
# 列表接口整体抓取的时间上限（秒）：到点即回退快照，避免挂起卡死整条流水线。
# 300s：东财被云 IP 阻断时每页都要烧满超时，600s 会让前置层白等 10 分钟；
# 且已有带富字段的基础快照可回退（见 get_universe），早回退反而更准更快。
LIST_FETCH_DEADLINE = _env_int("LIST_FETCH_DEADLINE", 300)
# K 线源熔断：东财探测能通过但批量被限流/持续 502 时，连续失败达此数即整体
# 切兜底，避免 4000+ 只逐只烧满「3 host × 超时 × 重试」把流水线拖成几小时。
# 2026-11-13：15 -> 5，网关类错误快速失败后尽早熔断，别在已拒绝的链路上加压。
EM_KLINE_CIRCUIT_THRESHOLD = 5

# 最近一次抓取的可观测状态（供 prefilter.py 写 meta.json / 触发降级告警）。
# 2026-11-13 新增：此前 K 线「成功 297 / 失败 4309」只有一行日志，下游模型
# 静默使用残缺缓存无从感知；现在把覆盖率、熔断状态显式暴露出去。
LAST_KLINE_STATS = {}
LAST_KZ_KLINE_STATS = {}
LAST_UNIVERSE_STATUS = {}
# 覆盖率低于此比例即判定「缓存降级」，写 meta 并告警（下游模型会自行回退抓数，
# 结果不必然错，但耗时与数据完整度都受影响，必须让人看得见）。
KLINE_COVERAGE_WARN = 0.9

_lock = threading.Lock()


def kline_stats(which="shared"):
    """返回最近一次 K 线批量抓取的详细统计（copy，避免调用方污染全局）。"""
    return dict(LAST_KLINE_STATS if which == "shared" else LAST_KZ_KLINE_STATS)


def kline_source_status():
    """当前 K 线数据源状态：是否已熔断切兜底、腾讯 WAF 是否拦截。"""
    return {
        "em_available": bool(_EM_AVAILABLE),
        "em_fail_streak": int(_em_fail_streak),
        "tx_waf_blocked": bool(_TX_WAF_BLOCKED),
        "em_circuit_threshold": EM_KLINE_CIRCUIT_THRESHOLD,
    }


def universe_status():
    """最近一次样本池构建的数据来源状态（realtime / snapshot / none）。"""
    return dict(LAST_UNIVERSE_STATUS)


# ------------------------------------------------------------------ 通用工具
# 网关/风控类状态码：重试与逐 host 轮换只会给已被拒绝的链路继续加压，
# 这类错误直接快速失败，由上层熔断切新浪/腾讯兜底。
_GATEWAY_FAIL_CODES = {403, 418, 429, 502, 503, 504}
_HTTP_ERR_SEEN = {}


def http_json(url, params=None, timeout=15, retry=2, referer=None):
    headers = {"Referer": referer} if referer else None
    host = url.split("//")[-1].split("/")[0]
    for attempt in range(retry + 1):
        try:
            r = _session().get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code != 200:
                key = (r.status_code, host)
                n = _HTTP_ERR_SEEN.get(key, 0) + 1
                _HTTP_ERR_SEEN[key] = n
                if n <= 2:      # 限量打印，避免 4600 只 x 多 host 把日志刷爆
                    body = (r.text or "")[:160].replace("\n", " ")
                    print("[common] HTTP %s host=%s secid=%s body=%s"
                          % (r.status_code, host,
                             (params or {}).get("secid", "-"), body))
                if r.status_code in _GATEWAY_FAIL_CODES:
                    return None
                raise RuntimeError("HTTP %s" % r.status_code)
            return r.json()
        except Exception as e:
            if attempt < retry:
                time.sleep(0.4 * (attempt + 1))
            else:
                print("[common] 请求异常 host=%s err=%s" % (host, e))
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
# 全A 剔除 ST/立信后正常应 >3000；低于此值视为接口拉取「明显失败」。
# 注意：仅用于「是否刷新快照」的阈值——降级日即使只拉到部分（>0），也允许继续跑
# （见 get_universe 的 partial 分支），只是不拿部分结果污染快照。
UNIVERSE_SANITY_MIN = 1000


def _fetch_raw_universe(attempts=3, backoff=20, deadline=LIST_FETCH_DEADLINE):
    """从东财 clist 拉全市场原始股票列表（code/name/board），不含任何剔除。

    对「云 IP 偶发阻断/限流/挂起」做多层退避：单 host 短超时重试 + 整轮
    拉取偏少时退避重跑；并设整体时间上限 deadline，到点即回退快照，
    避免「挂起」型故障把整条流水线卡死成无底洞。返回原始 rows；全部失败返回 []。
    """
    fields = "f2,f3,f5,f6,f8,f10,f12,f13,f14,f15,f16,f17,f18,f20,f21"
    start = time.time()
    for attempt in range(attempts + 1):
        rows = []
        for name, fs in EM_BOARDS:
            if time.time() - start > deadline:
                print("[common] 列表抓取已超 %ds 上限，提前结束本轮" % deadline)
                return rows
            pn = 1
            while True:
                if time.time() - start > deadline:
                    print("[common] 列表抓取已超 %ds 上限，提前结束本轮" % deadline)
                    return rows
                params = {
                    "pn": pn, "pz": 100, "po": 1, "np": 1, "fid": "f3",
                    "fs": fs, "fields": fields, "ut": EM_UT, "fltt": 2, "invt": 2,
                }
                data = {}
                hosts = EM_LIST_HOSTS[:4]   # 只试前 4 个 host，减少挂起叠加
                random.shuffle(hosts)
                for host in hosts:
                    j = http_json("https://%s/api/qt/clist/get" % host, params=params,
                                  timeout=BULK_TIMEOUT, retry=BULK_RETRY)
                    data = (j or {}).get("data") or {}
                    if data.get("diff"):
                        break
                    time.sleep(0.2)
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
                    # 必须保留东财原始富字段：get_universe() 用 f2/f3/f5/f6/f8/
                    # f18/f20/f21/f10 构建样本池，只存 code/name/board 会让
                    # price/pct/amount/量比 全部归零，下游模型筛选静默失真。
                    # 同时保证 save_base_universe 写出的快照与种入快照结构一致。
                    rows.append({
                        "code": code, "name": nm, "board": name,
                        "f2": price,
                        "f3": _to_float(x.get("f3")),      # 涨跌幅 %
                        "f5": _to_float(x.get("f5")),      # 成交量（手）
                        "f6": _to_float(x.get("f6")),      # 成交额（元）
                        "f8": _to_float(x.get("f8")),      # 换手率 %
                        "f10": _to_float(x.get("f10")),    # 量比
                        "f18": _to_float(x.get("f18")),    # 昨收
                        "f20": _to_float(x.get("f20")),    # 总市值
                        "f21": _to_float(x.get("f21")),    # 流通市值
                    })
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
    """把成功拉到的原始股票列表落盘为快照（下次接口失效时兜底）。

    防污染：只有带价格富字段的 rows 才能覆盖快照。历史版本曾因
    _fetch_raw_universe 只存 3 个字段，把种入的富字段快照刷成「无价格」版本，
    导致降级日回退后整池 price=0、模型筛选静默失真。
    """
    if not rows:
        return
    probe = rows[:50]
    if not any(_to_float(r.get("f2")) > 0 for r in probe):
        print("[common] 实时列表缺价格字段，跳过刷新基础快照（避免污染富字段快照）")
        return
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


# 快照健康判据（2026-11-13）：条数下限 + 必需字段 + 价格健全率。
# 目的：降级前先确认兜底数据本身可信，避免「空快照 / 旧快照被污染成 price=0」
# 被当成有效样本池，静默跑出失真的选股结果（11-12 曾发生）。
BASE_UNIVERSE_MIN_ROWS = 3000
BASE_UNIVERSE_MIN_PRICED_RATIO = 0.8


def check_base_universe_health(rows=None):
    """校验 base_universe.json 能否作为降级兜底。

    返回 (healthy: bool, reason: str, rows: list)。任何一项不满足都视为不可用。
    """
    if rows is None:
        rows = load_base_universe()
    if not rows:
        return False, "基础股票池快照缺失、为空或无法解析（base_universe.json）", []
    n = len(rows)
    if n < BASE_UNIVERSE_MIN_ROWS:
        return False, "快照仅 %d 只 < 下限 %d 只（疑似残缺）" % (n, BASE_UNIVERSE_MIN_ROWS), rows
    probe = rows[:500]
    if not all(str(r.get("code") or "").strip() and str(r.get("name") or "").strip()
               for r in probe):
        return False, "快照缺少 code/name 必需字段", rows
    priced = sum(1 for r in rows if _to_float(r.get("f2")) > 0)
    ratio = priced / float(n)
    if ratio < BASE_UNIVERSE_MIN_PRICED_RATIO:
        return False, ("快照价格健全率仅 %.1f%%（%d/%d 有价）< %.0f%%"
                       % (ratio * 100, priced, n, BASE_UNIVERSE_MIN_PRICED_RATIO * 100)), rows
    return True, "ok（%d 只，价格健全率 %.1f%%）" % (n, ratio * 100), rows


def get_universe(verbose=True, apply_lixin=True, force_snapshot=False):
    """东方财富全市场列表，应用硬共识剔除，返回统一结构的样本池。

    apply_lixin=False 时只做 ST/次新/无行情 剔除，用于「立信排除集生成」阶段
    拿到全市场基准代码（避免生成排除集时又引用排除集本身）。

    返回字段：code, name, board, price, pct(涨跌幅%), turnover(换手%),
             mktcap(总市值), float_mktcap(流通市值), vr(量比)。
    board 用于下游按板块过滤（如易点仅创/科/北交）。

    韧性：优先实时拉取（多层退避重试）；若实时结果异常偏少（接口偶发阻断），
    回退已提交的 base_universe.json 快照，保证流水线不因单一接口抖动而整体
    崩溃。实时成功时自动刷新快照。

    force_snapshot=True：跳过实时源，直接用「已通过健康校验」的基础快照构建
    样本池。仅供 prefilter 的 fail-degraded 二次兜底使用（实时源与常规回退都
    返回空时）。走的是同一套 ST / 立信 硬剔除逻辑，降级不改变任何准入条件。
    """
    lixin = load_lixin_exclude() if apply_lixin else set()
    st_drop = lixin_drop = 0

    if force_snapshot:
        healthy, reason, snap_rows = check_base_universe_health()
        if not healthy:
            print("[common] 强制快照降级不可用：%s" % reason)
            LAST_UNIVERSE_STATUS.clear()
            LAST_UNIVERSE_STATUS.update({"source": "none", "rows": 0, "error": reason})
            return []
        raw = snap_rows
        used_fallback = True
        snap = snap_rows
        print("[common] 强制使用基础股票池快照 %d 只（实时源异常，fail-degraded）" % len(raw))
        # best-effort 补当日价格；失败则沿用快照自带价格，不阻断
        prices = _refresh_prices_tx([r.get("code", "") for r in raw])
        if prices:
            for r in raw:
                pr = prices.get(str(r.get("code", "")).zfill(6))
                if pr:
                    r["f2"] = pr[0]
                    r["f3"] = pr[1]
            print("[common] 腾讯实时行情补价成功 %d 只" % len(prices))
    else:
        raw = _fetch_raw_universe()
        used_fallback = False
        snap = load_base_universe()
    # 只有「实时结果不比已提交的完整快照少」才允许刷新快照。
    # UNIVERSE_SANITY_MIN(1000) 只代表接口没完全挂：若降级日实时拉到 1500 只
    # 就覆盖 4607 只快照，会造成样本池静默缩水（历史缺陷）。降级日一律回退
    # 更全的快照，绝不拿部分结果刷新快照。
    if force_snapshot:
        pass  # 上面已完成 raw 赋值；降级数据绝不回写快照，避免污染富字段快照
    elif len(raw) >= UNIVERSE_SANITY_MIN and len(raw) >= int(len(snap) * 0.9):
        save_base_universe(raw)
    elif raw:
        if len(snap) > len(raw):
            print("[common] 实时列表偏少（%d 只 < 快照 %d 只，疑似接口降级），回退基础快照"
                  % (len(raw), len(snap)))
            raw = snap
            used_fallback = True
        else:
            print("[common] 实时列表偏少（%d 只），快照不更全，仍继续（不刷新快照）"
                  % len(raw))
    else:
        # 实时一只没拉到 → 回退快照；无任何数据则样本池为空（交由 watchdog 重试）
        if snap:
            print("[common] 实时列表异常（0 只），回退基础股票池快照 %d 只"
                  % len(snap))
            raw = snap
            used_fallback = True
            # 用腾讯实时行情补当日价格（best-effort，失败则用快照自带价格）
            prices = _refresh_prices_tx([r.get("code", "") for r in snap])
            if prices:
                for r in raw:
                    pr = prices.get(str(r.get("code", "")).zfill(6))
                    if pr:
                        r["f2"] = pr[0]; r["f3"] = pr[1]
                print("[common] 腾讯实时行情补价成功 %d 只" % len(prices))
        else:
            print("[common] 实时列表异常且无快照，样本池为空。")
            if verbose:
                print("[common] 全市场样本池：0 只（ST/次新剔除 0，立信剔除 0）")
            LAST_UNIVERSE_STATUS.clear()
            LAST_UNIVERSE_STATUS.update({
                "source": "none", "rows": 0,
                "error": "realtime_empty_and_snapshot_unusable",
            })
            return []

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
    # 价格健全性兜底：若样本池绝大多数价格为 0（实时列表丢字段 / 旧快照被污染 /
    # 腾讯补价失败），用带富字段的基础快照按代码补齐，避免下游模型拿着全 0 的
    # 价格、成交额、量比静默跑出失真结果。
    if rows:
        priced = sum(1 for r in rows if r["price"] > 0)
        if priced * 2 < len(rows):
            smap = {str(r.get("code", "")).zfill(6): r for r in load_base_universe()}
            _fields = (("price", "f2"), ("pct", "f3"), ("volume", "f5"),
                       ("amount", "f6"), ("turnover", "f8"), ("vr", "f10"),
                       ("prev_close", "f18"), ("mktcap", "f20"),
                       ("float_mktcap", "f21"))
            filled = 0
            for r in rows:
                if r["price"] > 0:
                    continue
                s = smap.get(r["code"])
                if not s or _to_float(s.get("f2")) <= 0:
                    continue
                for dst, src in _fields:
                    r[dst] = _to_float(s.get(src))
                filled += 1
            print("[common] 样本池价格异常（仅 %d/%d 有价），用基础快照补齐 %d 只"
                  % (priced, len(rows), filled))
    if verbose:
        print("[common] 全市场样本池：%d 只（ST/次新剔除 %d，立信剔除 %d）"
              % (len(rows), st_drop, lixin_drop))
    LAST_UNIVERSE_STATUS.clear()
    LAST_UNIVERSE_STATUS.update({
        "source": "snapshot" if used_fallback else "realtime",
        "rows": len(rows),
        "st_drop": st_drop,
        "lixin_drop": lixin_drop,
    })
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
def tx_secid_of(code):
    """腾讯行情代码前缀：沪/科创=sh，深/创业=sz，北交=bj。"""
    code = str(code).zfill(6)
    if code.startswith(("6", "688", "689")):
        return "sh" + code
    if code.startswith(("8", "4", "92")):
        return "bj" + code
    return "sz" + code


# 东财可用性探针（进程内只探一次）：东财对云 IP 偶发整体阻断，一旦探测失败则后续
# K 线直接走腾讯兜底，避免在 4000+ 只上每只都烧东财超时（无底洞卡死）。
_EM_PROBED = False
_EM_AVAILABLE = True
_em_fail_streak = 0      # 东财连续失败计数（批量熔断用，受 _lock 保护）
_TX_WAF_BLOCKED = False  # 腾讯 WAF 拦截熔断（云 IP 常见 501）


def _em_kline_available():
    global _EM_PROBED, _EM_AVAILABLE
    if _EM_PROBED:
        return _EM_AVAILABLE
    _EM_PROBED = True
    for host in (EM_KLINE_HOSTS[0], EM_KLINE_HOSTS[1]):
        j = http_json("https://%s/api/qt/stock/kline/get" % host,
                      params={"secid": secid_of("600519"), "ut": EM_KUT,
                              "fields1": "f1", "fields2": "f51",
                              "klt": 101, "fqt": 1, "end": "20500101", "lmt": 1},
                      timeout=5, retry=0)
        if ((j or {}).get("data") or {}).get("klines"):
            _EM_AVAILABLE = True
            return True
    _EM_AVAILABLE = False
    print("[common] 东财 K 线探测失败，后续 K 线自动回退新浪/腾讯兜底")
    return False


def _fetch_one_kline_em(code, count=KLINE_CACHE_COUNT, fqt=1):
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
    for host in hosts[:3]:
        j = http_json("https://%s/api/qt/stock/kline/get" % host, params=params,
                      timeout=BULK_TIMEOUT, retry=BULK_RETRY)
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


def _fetch_one_kline_tx(code, count=KLINE_CACHE_COUNT, fqt=1):
    """腾讯财经历史 K 线兜底（东财挂起/阻断时启用）。

    字段顺序与东财一致：[日期,开,收,高,低,成交量(手)]，成交量单位同为「手」，
    故下游模型无需任何改动。fqt=1→前复权(qfqday)，fqt=0→不复权(day)。
    """
    global _TX_WAF_BLOCKED
    if _TX_WAF_BLOCKED:
        return None
    kind = "qfqday" if fqt else "day"
    sid = tx_secid_of(code)
    # 自己发请求以识别 WAF 拦截：腾讯对云 IP 会返回 501 跳转页（非 JSON），
    # http_json 只会当普通失败并重试，导致每只白等 超时×重试。首次识别即熔断。
    try:
        r = _session().get("https://%s/appstock/app/fqkline/get" % TX_KLINE_HOST,
                        params={"param": "%s,%s,,,%d," % (sid, kind, count)},
                        timeout=4)
        if r.status_code in (403, 501):
            _TX_WAF_BLOCKED = True
            print("[common] 腾讯 K 线被 WAF 拦截（HTTP %s），后续跳过该兜底源"
                  % r.status_code)
            return None
        if r.status_code != 200:
            return None
        j = r.json()
    except Exception:
        return None
    node = ((j or {}).get("data") or {}).get(sid) or {}
    rows = (node.get(kind) or node.get("day") or node.get("qfqday")) or []
    bars = []
    for p in rows:
        if not isinstance(p, (list, tuple)) or len(p) < 6:
            continue
        try:
            bars.append({
                "date": str(p[0]),
                "open": float(p[1]), "close": float(p[2]),
                "high": float(p[3]), "low": float(p[4]),
                "volume": float(p[5]),
                "turnover": 0.0,
            })
        except (ValueError, TypeError):
            continue
    return bars if bars else None


def _fetch_one_kline_sina(code, count=KLINE_CACHE_COUNT, fqt=1):
    """新浪历史日线兜底（东财不可用时启用，已在该 runner 环境验证可全市场扫描）。

    不复权日线（scale=240）；字段 day/open/high/low/close/volume，成交量单位为
    「股」，÷100 转「手」以与东财口径一致（凯众量能判定用相对倍数，常数不影响结果）。
    fqt 参数仅东财前/不复权有意义，新浪固定不复权，故忽略 fqt。
    """
    sym = tx_secid_of(code)      # sh/sz/bj 前缀，与新浪代码一致
    # 走 HTTPS：部分云环境会拦明文 HTTP。
    url = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/"
           "CN_MarketDataService.getKLineData?symbol=" + sym +
           "&scale=240&ma=no&datalen=" + str(count))
    txt = None
    for _attempt in range(2):
        try:
            r = _session().get(url, headers={"User-Agent": HEADERS["User-Agent"],
                                          "Referer": "https://finance.sina.com.cn"},
                            timeout=BULK_TIMEOUT)
            if r.status_code == 200 and r.text:
                txt = r.text
                break
        except Exception:
            pass
        time.sleep(0.25)
    if not txt:
        return None
    # 【重要】新浪正常响应开头固定带一段注释：
    #   /*<script>location.href='//sina.com';</script>*/
    # 历史版本用 `if "location.href" in txt: return None` 判反爬，
    # 结果把 **每一次正常响应都误杀**，导致东财熔断后兜底 100% 失败
    # （2026-11-12 实测 K 线缓存仅 297/4606 成功）。
    # 正确判据是「能否提取到有效 JSON 数组」，而不是任何字符串黑名单。
    m = re.search(r"\((\[.*\])\)", txt, re.S)
    if not m:
        return None
    bars = []
    try:
        for d in json.loads(m.group(1)):
            try:
                bars.append({
                    "date": str(d["day"]),
                    "open": float(d["open"]), "close": float(d["close"]),
                    "high": float(d["high"]), "low": float(d["low"]),
                    "volume": float(d["volume"]) / 100.0,   # 股→手
                    "turnover": 0.0,
                })
            except (ValueError, TypeError, KeyError):
                continue
    except Exception:
        return None
    return bars if bars else None


def _fetch_one_kline(code, count=KLINE_CACHE_COUNT, fqt=1):
    """单只 K 线：优先东财；东财不可用时自动回退新浪→腾讯兜底（字段口径一致）。

    兜底顺序 新浪→腾讯：新浪历史日线已在该 runner 环境验证可全市场扫描；腾讯历史
    K 线字段与东财一致，作次选冗余。两者均返回不复权日线，与东财 fqt=1 前复权在
    「无近期除权」时等价；除权股在降级日略有偏差，属可接受的兜底近似（远优于无
    数据导致整条流水线崩溃）。
    """
    global _em_fail_streak, _EM_AVAILABLE
    if _em_kline_available():
        bars = _fetch_one_kline_em(code, count=count, fqt=fqt)
        if bars:
            with _lock:
                _em_fail_streak = 0
            return bars
        # 探测能过但批量被限流是东财对云 IP 的典型症状：此时逐只重试只会把
        # 4000+ 只 × 超时叠加成几小时。连续失败到阈值即整体熔断，后续直接兜底。
        with _lock:
            _em_fail_streak += 1
            if _em_fail_streak >= EM_KLINE_CIRCUIT_THRESHOLD and _EM_AVAILABLE:
                _EM_AVAILABLE = False
                print("[common] 东财 K 线连续失败 %d 次，熔断切新浪/腾讯兜底"
                      % _em_fail_streak)
    bars = _fetch_one_kline_sina(code, count=count)
    if bars:
        return bars
    return _fetch_one_kline_tx(code, count=count)


def _refresh_prices_tx(codes, batch=80):
    """腾讯实时行情批量刷新价格（东财列表接口失效时，给快照样本池补当日价格）。

    返回 {code: (price, pct)}。best-effort：单批失败不中断，整体失败返回空，
    交由快照自带（上次成功时的）价格兜底，流水线不会因此崩溃。
    """
    out = {}
    for i in range(0, len(codes), batch):
        chunk = codes[i:i + batch]
        q = ",".join(tx_secid_of(c) for c in chunk)
        try:
            r = _session().get("https://%s/q=%s" % (TX_QUOTE_HOST, q),
                            headers={"User-Agent": HEADERS["User-Agent"],
                                     "Referer": "https://gu.qq.com/"},
                            timeout=8)
            txt = r.text
        except Exception:
            continue
        for line in txt.split(";"):
            line = line.strip()
            if not line.startswith("v_"):
                continue
            _, _, val = line.partition("=")
            val = val.strip().strip('"')
            if not val:
                continue
            parts = val.split("~")
            if len(parts) < 5:
                continue
            try:
                price = float(parts[3]); prev = float(parts[4])
            except ValueError:
                continue
            code = str(parts[2]).zfill(6)
            pct = (price - prev) / prev * 100 if prev else 0.0
            out[code] = (price, pct)
    return out


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
                       skip_existing=True, verbose=True, budget=KLINE_BATCH_BUDGET):
    """批量把全市场 K 线缓存到本地（共享给下游模型）。已存在且足够的跳过。

    返回 (成功数, 失败数)。本步骤是「提速优化」而非必需：设硬性时间预算 budget，
    超时即放弃剩余（模型各自会回退重抓），避免东财挂起时把整条流水线卡死。
    """
    targets = []
    for u in universe:
        code = u["code"]
        path = _kline_csv_path(code)
        if skip_existing and os.path.exists(path) and os.path.getsize(path) > 0:
            continue
        targets.append(code)

    if verbose:
        print("[common] K线缓存：需新拉 %d 只（已缓存跳过 %d 只），预算 %ds"
              % (len(targets), len(universe) - len(targets), budget))

    ok = fail = skip = 0
    done = 0
    total = len(targets)
    deadline = time.time() + budget

    def work(code):
        if time.time() > deadline:
            return None          # 超预算，跳过（不抓）
        bars = _fetch_one_kline(code, count=count)
        if bars:
            _write_kline_csv(code, bars)
            return True
        return False

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(work, c): c for c in targets}
        for fut in as_completed(futs):
            try:
                res = fut.result()
            except Exception:
                res = False
            if res is True:
                ok += 1
            elif res is False:
                fail += 1
            else:
                skip += 1       # 超预算跳过
            done += 1
            if verbose and done % 200 == 0:
                print("[common] K线缓存进度 %d/%d  成功 %d 失败 %d 跳过 %d"
                      % (done, total, ok, fail, skip))
    if verbose:
        print("[common] K线缓存完成：成功 %d，失败 %d，跳过 %d（预算 %ds）"
              % (ok, fail, skip, budget))
    coverage = (ok / float(total)) if total else 1.0
    LAST_KLINE_STATS.clear()
    LAST_KLINE_STATS.update({
        "kind": "shared", "ok": ok, "fail": fail, "skip": skip,
        "targets": total, "budget": budget,
        "coverage": round(coverage, 4),
        "degraded": bool(total) and coverage < KLINE_COVERAGE_WARN,
        "source": kline_source_status(),
    })
    if total and coverage < KLINE_COVERAGE_WARN:
        print("[common][ALERT] 共享K线缓存未全覆盖：成功 %d / 需拉 %d（覆盖率 %.1f%%），"
              "失败 %d、超预算跳过 %d；下游模型将回退自行抓取，耗时与数据完整度受影响。"
              % (ok, total, coverage * 100, fail, skip))
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
                          workers=KLINE_WORKERS, quote_date=None, verbose=True,
                          budget=KLINE_BATCH_BUDGET):
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

    返回 (成功数, 未预填数=抓取失败+超预算跳过)。这些个股由凯众自行回退
    新浪抓取，不影响正确性；预算用于保证前置层不会拖垮整条流水线。
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
    ok = fail = skipped = 0
    deadline = time.time() + budget

    def work(code):
        # 硬时间预算：与 fetch_klines_batch 对齐。此前本函数没有预算，
        # 东财挂起时 4000+ 只逐只烧超时会让前置层卡死数小时，
        # 进而拖垮整条统一流水线（run_all 根本轮不到执行）。
        if time.time() > deadline:
            return None
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
        futs = {ex.submit(work, c): c for c in targets}
        for fut in as_completed(futs):
            try:
                r = fut.result()
            except Exception:
                r = False
            if r is True:
                ok += 1
            elif r is None:
                skipped += 1
            else:
                fail += 1
    if verbose:
        print("[common] 凯众不复权K线完成：成功 %d，失败 %d，超预算跳过 %d"
              "（失败/跳过者凯众自行回退）" % (ok, fail, skipped))
    total = len(targets)
    coverage = (ok / float(total)) if total else 1.0
    LAST_KZ_KLINE_STATS.clear()
    LAST_KZ_KLINE_STATS.update({
        "kind": "kaizhong", "ok": ok, "fail": fail, "skip": skipped,
        "targets": total, "budget": budget,
        "coverage": round(coverage, 4),
        "degraded": bool(total) and coverage < KLINE_COVERAGE_WARN,
        "source": kline_source_status(),
    })
    if total and coverage < KLINE_COVERAGE_WARN:
        print("[common][ALERT] 凯众不复权K线未全覆盖：成功 %d / 需预填 %d（覆盖率 %.1f%%），"
              "凯众将自行回退新浪抓取。" % (ok, total, coverage * 100))
    return ok, fail + skipped


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
