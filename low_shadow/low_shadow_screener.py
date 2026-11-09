#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
低位上影线样本 · 收盘后模型  low_shadow_screener.py
=====================================================

运行方式
--------
    python3 low_shadow_screener.py --mode review        # 收盘后复盘（主模式）
    python3 low_shadow_screener.py --mode afterclose    # 兼容别名，等同于 review
    python3 low_shadow_screener.py --mode backtest      # 回溯上一日候选、更新胜率与参数
    python3 low_shadow_screener.py --mode review --demo # 离线演示（内置确定性假数据）

模型组成（全部内置，无需外部配置即可跑通）
------------------------------------------------
    1. 形态筛           : 识别「低位 + 上影线」样本
    2. 低位判定         : 收盘价位于中长期区间低位 / 跌破 MA
    3. 评分             : 上影线、低位、放量、弱势趋势多因子加权打分
    4. 立信审计一票否决 : 审计机构为「立信」体系者直接剔除
    5. 环境门槛         : 通过环境过滤的标的数 >= ENV_THRESHOLD(默认60) 才推个股，否则空仓
    6. 云端归档         : 每次结果写本地归档；若配置 CLOUD_WEBHOOK 则推送云端
    7. 次日回溯         : 记录候选次日真实表现，计算近期胜率
    8. 参数修正         : 依据近期胜率自适应微调阈值（收紧 / 放宽 / 维持）
    9. 异常处理         : 全链路 try/except，单点失败不中断整体运行

数据源说明（与仓库其他模型统一）
-------------------------------
    使用东方财富行情接口（requests 直连，已在善水/领湃验证可全市场扫描）。
    内置「演示数据」仅供离线调试，默认关闭；仅当显式传入 --demo 时才启用，
    生产环境若取数失败会如实报错并空仓，绝不伪造候选。

立信剔除说明
------------
    真实行情无法便捷拿到审计机构名称，故立信一票否决改为复用仓库内
    领湃模型维护的排除集（领湃_立信排除.json）。领湃在 CI 中把它写到
    lingpei/lingpei/state/ 下；load_lixin_exclude() 会按多候选路径兼容
    lingpei/state/ 与 lingpei/lingpei/state/ 两种布局。该文件由
    lingpei/_lixin_update.py 增量更新，若确实尚未生成，当日立信剔除为 0
    （仅告警，不阻断）——此时应检查路径，而非误判为「今日无立信股」。
"""

import argparse
import json
import logging
import math
import os
import random
import sys
import time
from datetime import datetime, timedelta

import requests

try:  # 抑制自签名/兼容告警噪音
    from urllib3.exceptions import InsecureRequestWarning
    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
except Exception:  # noqa: BLE001
    pass

# ----------------------------------------------------------------------------
# 日志
# ----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("low_shadow")

# ----------------------------------------------------------------------------
# 路径与常量
# ----------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ARCHIVE_DIR = os.path.join(BASE_DIR, "state")          # 归档/回溯/参数，由 workflow 回写
PENDING_FILE = os.path.join(ARCHIVE_DIR, "pending.json")
HISTORY_FILE = os.path.join(ARCHIVE_DIR, "history.jsonl")
PARAMS_FILE = os.path.join(BASE_DIR, "params.json")

# 立信审计体系（名称路径，作为二次保险；主剔除走领湃排除集文件）
AUDIT_BLACKLIST = {
    "立信会计师事务所",
    "立信会计师事务所(特殊普通合伙)",
    "立信中联会计师事务所",
    "立信中联会计师事务所(特殊普通合伙)",
}

# 示例种子股票（仅 --demo 使用）
SEED_STOCKS = [
    {"symbol": "300030", "name": "阳普医疗"},
    {"symbol": "688095", "name": "福昕软件"},
]

# ----------------------------------------------------------------------------
# 东方财富行情层（与领湃/善水一致，requests 直连，已验证可全市场扫描）
# 注意：列表接口 EM_UT 令牌敏感，错误令牌会返回空 data；此处使用领湃验证过的有效值。
# ----------------------------------------------------------------------------
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0"})

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
EM_UT = "bd1d9ddb04089700cf9c27f6f7426281"   # 列表接口专用 ut（错误令牌会返回空数据）
EM_KUT = "fa5fd1943c7b386f172d6893dbfba10b"   # K线接口 ut
EM_BOARDS = [
    ("沪市主板", "m:1+t:2"),
    ("深市主板", "m:0+t:6"),
    ("创业板", "m:0+t:80"),
    ("科创板", "m:1+t:23"),
    ("北交所", "m:0+t:81+s:2048"),
]


def _to_float(v, default=0.0):
    try:
        if v in ("-", "", None):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _http_json(url, params=None, timeout=15, retry=2):
    for attempt in range(retry + 1):
        try:
            r = SESSION.get(url, params=params, timeout=timeout, verify=False)
            if r.status_code != 200:
                raise RuntimeError("HTTP %s" % r.status_code)
            return r.json()
        except Exception:  # noqa: BLE001
            if attempt < retry:
                time.sleep(0.4 * (attempt + 1))
    return None


def szsh_of(code):
    """东财 secid 前缀：沪市 1，深市/创业板/北交所 0。"""
    code = str(code).zfill(6)
    if code.startswith("6"):
        return "1"
    return "0"


def secid_of(code):
    code = str(code).zfill(6)
    return ("1." if code.startswith("6") else "0.") + code


def get_universe():
    """东方财富行情列表：全市场 A 股（沪深主板/创业板/科创板/北交所），一次拿齐代码/名称/涨跌幅。"""
    if DEMO:
        return _demo_universe()
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
                j = _http_json("https://%s/api/qt/clist/get" % host, params=params, timeout=15, retry=0)
                data = (j or {}).get("data") or {}
                if data.get("diff"):
                    break
                time.sleep(0.2)
            diff = data.get("diff") or []
            if isinstance(diff, dict):
                diff = list(diff.values())
            if not diff:
                break
            total = int(_to_float(data.get("total")))
            for x in diff:
                code = str(x.get("f12", "")).zfill(6)
                price = _to_float(x.get("f2"))
                if not code or price <= 0:
                    continue
                rows.append({"symbol": code, "name": x.get("f14", ""),
                             "pct": _to_float(x.get("f3"))})
            if len(diff) < 100 or (total and pn * 100 >= total):
                break
            pn += 1
            time.sleep(0.2)
    seen, uniq = set(), []
    for r in rows:
        if r["symbol"] in seen:
            continue
        seen.add(r["symbol"])
        uniq.append(r)
    log.info("东财获取全市场股票 %d 只", len(uniq))
    return uniq


def em_hist(code, count=150):
    """东财日 K（前复权）。分片域名轮询 + 重试规避限流。返回 [{date,open,high,low,close,vol}]。"""
    beg = (datetime.now() - timedelta(days=count * 3)).strftime("%Y%m%d")
    params = {
        "secid": secid_of(code), "ut": EM_KUT,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": 101, "fqt": 1, "beg": beg, "end": "20500101", "lmt": count,
    }
    hosts = EM_KLINE_HOSTS[:]
    random.shuffle(hosts)
    for host in hosts:
        j = _http_json("https://%s/api/qt/stock/kline/get" % host, params=params, timeout=12, retry=1)
        rows = ((j or {}).get("data") or {}).get("klines") or []
        bars = []
        for s in rows:
            p = s.split(",")
            if len(p) < 6:
                continue
            try:
                bars.append({
                    "date": p[0], "open": float(p[1]), "close": float(p[2]),
                    "high": float(p[3]), "low": float(p[4]), "vol": float(p[5]),
                })
            except ValueError:
                continue
        if bars:
            return bars
    return []


def get_kline(symbol, lookback):
    """返回该股票最近 lookback+30 根日K线。生产环境取数失败返回空列表（不伪造）。"""
    if DEMO:
        return _demo_kline(symbol, lookback)
    bars = em_hist(symbol, count=lookback + 30)
    if not bars:
        log.debug("K线缺失：%s", symbol)
    return bars


def get_auditor(symbol):
    """真实行情无法便捷获取审计机构名称；立信剔除统一走领湃排除集（见 is_lixin_excluded）。"""
    return "未知"


# ----------------------------------------------------------------------------
# 立信排除集（复用领湃维护的 领湃_立信排除.json）
# 领湃在 CI 中以 working-directory=lingpei、STATE_DIR=lingpei/state 运行，
# 排除集实际落在 仓库根/lingpei/lingpei/state/；历史/其它布局可能落在
# 仓库根/lingpei/state/。下面按多候选路径依次查找，兼容两种布局，避免
# 「找不到文件 → 立信剔除静默为 0」。
# ----------------------------------------------------------------------------
def load_lixin_exclude():
    """返回 (排除代码集合, 是否成功加载)。失败返回 (空集合, False)。"""
    candidates = [
        os.path.abspath(os.path.join(BASE_DIR, "..", "lingpei", "state", "领湃_立信排除.json")),
        os.path.abspath(os.path.join(BASE_DIR, "..", "lingpei", "lingpei", "state", "领湃_立信排除.json")),
    ]
    for p in candidates:
        if not os.path.exists(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            codes = set(d.get("codes", []))
            log.info("立信排除集已加载：%d 只（来源 %s）", len(codes), p)
            return codes, True
        except Exception as e:  # noqa: BLE001
            log.warning("立信排除集读取失败 %s：%s", p, e)
    return set(), False


# ----------------------------------------------------------------------------
# 参数（可被「参数修正」模块持久化覆盖）
# ----------------------------------------------------------------------------
DEFAULT_PARAMS = {
    "lookback": 120,          # 区间回看窗口（交易日）
    "upper_shadow_min": 0.50, # 上影线占整根K线最小比例
    "low_pos_pct": 0.40,      # 收盘价位于区间底部的最大比例（越小越严格）
    "min_score": 60,          # 入选最低评分
    "env_threshold": 60,      # 环境门槛：通过环境过滤标的数 >= 此值才推个股
    "top_n": 6,               # 简报展示前 N 只
    "vol_mult": 1.20,         # 放量倍数阈值
}


def load_params():
    """加载持久化参数；不存在则用默认。"""
    try:
        if os.path.exists(PARAMS_FILE):
            with open(PARAMS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            p = {**DEFAULT_PARAMS, **saved}
            log.info("已加载持久化参数：%s", p)
            return p
    except Exception as e:  # noqa: BLE001
        log.warning("参数加载失败，使用默认：%s", e)
    return dict(DEFAULT_PARAMS)


def save_params(params):
    try:
        with open(PARAMS_FILE, "w", encoding="utf-8") as f:
            json.dump(params, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        log.warning("参数保存失败：%s", e)


# ----------------------------------------------------------------------------
# 演示数据（仅在 --demo 启用，确定性，保证可复现）
# ----------------------------------------------------------------------------
_DEMO_AUDITORS = {}


def _demo_universe():
    rng = random.Random(20261109)
    universe = [dict(s) for s in SEED_STOCKS]
    audit_pool = ["立信会计师事务所", "天健会计师事务所", "大华会计师事务所",
                  "容诚会计师事务所", "中审众环会计师事务所", "未知"]
    _DEMO_AUDITORS["300030"] = "大华会计师事务所"
    _DEMO_AUDITORS["688095"] = "天健会计师事务所"
    for i in range(1, 121):
        sym = "DEMO%04d" % i
        universe.append({"symbol": sym, "name": "演示股%03d" % i})
        _DEMO_AUDITORS[sym] = rng.choice(audit_pool)
    return universe


def _demo_kline(symbol, lookback):
    rng = random.Random(hash(symbol) & 0xFFFFFFFF)
    n = lookback + 10
    price = rng.uniform(8, 60)
    recs = []
    base = datetime.now() - timedelta(days=n * 1.6)
    inject = (hash(symbol) % 100) < 12
    for i in range(n):
        drift = rng.uniform(-0.02, 0.02)
        op = price
        cl = max(1.0, price * (1 + drift))
        hi = max(op, cl) * (1 + rng.uniform(0, 0.02))
        lo = min(op, cl) * (1 - rng.uniform(0, 0.02))
        if inject and i == n - 1:
            hi = max(op, cl) * (1 + rng.uniform(0.06, 0.12))
            cl = min(op, cl) * (1 - rng.uniform(0.01, 0.03))
            lo = cl * (1 - rng.uniform(0.01))
        vol = rng.uniform(0.5, 2.0) * 1_000_000
        recs.append({
            "date": (base + timedelta(days=int(i * 1.55))).strftime("%Y-%m-%d"),
            "open": round(op, 2), "high": round(hi, 2), "low": round(lo, 2),
            "close": round(cl, 2), "vol": round(vol, 0),
        })
        price = cl
    return recs


# ----------------------------------------------------------------------------
# 特征与形态识别
# ----------------------------------------------------------------------------
def extract_last_features(recs, params):
    """从K线提取末根与区间特征。失败时返回 None。"""
    if not recs or len(recs) < 2:
        return None
    try:
        recs = sorted(recs, key=lambda x: x["date"])
        last = recs[-1]
        look = recs[-params["lookback"]:]
        lo_min = min(r["low"] for r in look)
        hi_max = max(r["high"] for r in look)
        rng = hi_max - lo_min
        pos = (last["close"] - lo_min) / rng if rng > 0 else 0.5  # 区间位置 0~1

        body_high = max(last["open"], last["close"])
        body_low = min(last["open"], last["close"])
        bar_rng = last["high"] - last["low"]
        if bar_rng <= 0:
            return None
        upper_shadow = (last["high"] - body_high) / bar_rng   # 上影线占比
        lower_shadow = (body_low - last["low"]) / bar_rng
        close_in_lower_half = last["close"] <= (body_high + body_low) / 2

        closes = [r["close"] for r in recs]
        ma60 = sum(closes[-60:]) / min(60, len(closes)) if len(closes) >= 20 else last["close"]
        below_ma = last["close"] < ma60

        avg_vol = sum(r["vol"] for r in recs[-20:]) / min(20, len(recs))
        vol_up = last["vol"] > avg_vol * params["vol_mult"]

        return {
            "date": last["date"], "close": last["close"],
            "upper_shadow": upper_shadow, "lower_shadow": lower_shadow,
            "close_in_lower_half": close_in_lower_half,
            "pos_in_range": pos, "below_ma": below_ma, "vol_up": vol_up,
            "ma60": round(ma60, 2),
        }
    except Exception as e:  # noqa: BLE001
        log.debug("特征提取失败：%s", e)
        return None


def is_low_shadow(feat, params):
    """形态筛：上影线足够长且收在下半部，且处于低位。"""
    if feat is None:
        return False
    shadow_ok = feat["upper_shadow"] >= params["upper_shadow_min"] \
        and feat["close_in_lower_half"]
    low_ok = feat["pos_in_range"] <= params["low_pos_pct"] or feat["below_ma"]
    return shadow_ok and low_ok


def score_stock(feat, params):
    """多因子评分（0~100）。"""
    if feat is None:
        return 0.0
    s = 0.0
    s += min(feat["upper_shadow"] / max(params["upper_shadow_min"], 1e-6), 1.0) * 30
    s += (1 - min(feat["pos_in_range"], 1.0)) * 30
    if feat["vol_up"]:
        s += 15
    if feat["below_ma"]:
        s += 15
    if feat["lower_shadow"] < 0.1:  # 下影线短，主力试盘未承接
        s += 10
    return round(min(100.0, s), 1)


# ----------------------------------------------------------------------------
# 环境门槛
# ----------------------------------------------------------------------------
def env_pass(recs, params):
    """环境过滤：价格未处于区间高位（非追高环境）且非地量。"""
    feat = extract_last_features(recs, params)
    if feat is None:
        return False
    if feat["pos_in_range"] >= 0.85:   # 区间极度高位，环境过热
        return False
    if feat["vol_up"] is False and feat["pos_in_range"] > 0.6:
        return False
    return True


# ----------------------------------------------------------------------------
# 推送（PushPlus，与仓库其他模型统一）
# ----------------------------------------------------------------------------
def push(title, content):
    token = (os.environ.get("PUSHPLUS_TOKEN") or "").strip()
    if not token:
        print("[推送] 未配置 PUSHPLUS_TOKEN，仅打印")
        print(content)
        return False
    try:
        r = requests.post("https://www.pushplus.plus/send", json={
            "token": token, "title": title,
            "content": content, "template": "markdown",
        }, timeout=15)
        j = r.json()
        print("[PushPlus] code=%s msg=%s" % (j.get("code"), j.get("msg")))
        return j.get("code") == 200
    except Exception as e:  # noqa: BLE001
        print("[PushPlus] 异常: %s" % e)
        return False


# ----------------------------------------------------------------------------
# 归档 / 回溯 / 参数修正
# ----------------------------------------------------------------------------
def ensure_archive():
    os.makedirs(ARCHIVE_DIR, exist_ok=True)


def cloud_archive(payload):
    """本地归档 + 可选云端推送（配置 CLOUD_WEBHOOK 环境变量时推送）。"""
    ensure_archive()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    local = os.path.join(ARCHIVE_DIR, "run_%s.json" % ts)
    try:
        with open(local, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        log.info("本地归档：%s", local)
    except Exception as e:  # noqa: BLE001
        log.warning("本地归档失败：%s", e)

    wh = os.environ.get("CLOUD_WEBHOOK")
    if wh:
        try:
            req = requests.Request("POST", wh, data=json.dumps(payload).encode("utf-8"),
                                   headers={"Content-Type": "application/json"})
            with requests.Session().send(req.prepare(), timeout=10) as resp:
                log.info("云端归档推送状态：%s", resp.status_code)
        except Exception as e:  # noqa: BLE001
            log.warning("云端归档推送失败（不影响本地）：%s", e)
    else:
        log.info("未配置 CLOUD_WEBHOOK，跳过云端推送（仅本地归档）")
    return local


def save_pending(candidates, run_date):
    ensure_archive()
    try:
        with open(PENDING_FILE, "w", encoding="utf-8") as f:
            json.dump({"date": run_date, "candidates": candidates}, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        log.warning("pending 写入失败：%s", e)


def run_backtest(params):
    """回溯上一日候选次日表现，更新历史并计算胜率，触发参数修正。"""
    ensure_archive()
    if not os.path.exists(PENDING_FILE):
        log.info("无待回溯候选，跳过")
        return None
    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            pend = json.load(f)
    except Exception as e:  # noqa: BLE001
        log.warning("读取 pending 失败：%s", e)
        return None

    run_date = pend.get("date")
    new_hist = []
    for c in pend.get("candidates", []):
        sym = c["symbol"]
        recs = get_kline(sym, params["lookback"])
        if not recs or len(recs) < 3:
            continue
        recs = sorted(recs, key=lambda x: x["date"])
        idx = next((i for i, r in enumerate(recs) if r["date"] >= run_date), None)
        if idx is None or idx + 1 >= len(recs):
            continue
        p0 = recs[idx]["close"]
        p1 = recs[idx + 1]["close"]
        ret = (p1 - p0) / p0 if p0 else 0.0
        win = ret > 0
        rec = {"symbol": sym, "name": c.get("name"), "date": run_date,
               "next_ret": round(ret, 4), "win": win}
        new_hist.append(rec)
        try:
            with open(HISTORY_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass

    win_rate = None
    action = "无历史数据"
    try:
        rows = []
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        recent = rows[-20:]
        if recent:
            wins = sum(1 for r in recent if r.get("win"))
            win_rate = round(wins / len(recent), 3)
            action = correct_params(params, win_rate)
    except Exception as e:  # noqa: BLE001
        log.warning("历史读取失败：%s", e)

    if os.path.exists(PENDING_FILE):
        try:
            os.remove(PENDING_FILE)
        except Exception:  # noqa: BLE001
            pass
    log.info("回溯完成：样本 %d，近期胜率 %s，参数动作：%s",
             len(new_hist), win_rate, action)
    return {"backtest_samples": len(new_hist), "win_rate": win_rate, "action": action}


def correct_params(params, win_rate):
    """依据近期胜率修正阈值。"""
    if win_rate is None:
        return "维持当前参数"
    if win_rate < 0.45:
        params["upper_shadow_min"] = min(0.70, params["upper_shadow_min"] + 0.05)
        params["low_pos_pct"] = max(0.20, params["low_pos_pct"] - 0.03)
        params["min_score"] = min(80, params["min_score"] + 2)
        action = "收紧阈值（胜率偏低）"
    elif win_rate > 0.60:
        params["upper_shadow_min"] = max(0.40, params["upper_shadow_min"] - 0.03)
        params["low_pos_pct"] = min(0.50, params["low_pos_pct"] + 0.02)
        params["min_score"] = max(50, params["min_score"] - 2)
        action = "放宽阈值（胜率偏高）"
    else:
        action = "维持当前参数"
    save_params(params)
    return action


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def run_review(params, top_n=None):
    top_n = top_n or params["top_n"]
    run_date = datetime.now().strftime("%Y-%m-%d")
    log.info("=== 低位上影线样本 · 收盘后复盘 ===  运行日 %s", run_date)

    universe = get_universe()
    if not universe:
        msg = "低位上影线模型 %s 数据获取失败（行情接口无返回），今日不出股。" % run_date
        log.error(msg)
        push("低位上影线模型 %s 数据异常" % run_date, msg)
        return None

    lixin_set, lixin_ok = load_lixin_exclude()
    if not lixin_ok:
        log.warning("立信排除集尚未生成（请先运行领湃），本日立信剔除为 0")

    total = len(universe)
    env_count = 0
    audited_out = 0
    candidates = []

    for stk in universe:
        try:
            sym = stk["symbol"]
            recs = get_kline(sym, params["lookback"])
            # 环境过滤计数（宽松口径）
            if env_pass(recs, params):
                env_count += 1
            # 立信审计一票否决（复用领湃排除集 + 名称黑名单兜底）
            auditor = get_auditor(sym)
            if sym in lixin_set or auditor in AUDIT_BLACKLIST:
                audited_out += 1
                continue
            # 形态筛
            feat = extract_last_features(recs, params)
            if not is_low_shadow(feat, params):
                continue
            sc = score_stock(feat, params)
            if sc < params["min_score"]:
                continue
            candidates.append({
                "symbol": sym, "name": stk.get("name", ""),
                "score": sc, "close": feat["close"],
                "upper_shadow": round(feat["upper_shadow"], 3),
                "pos_in_range": round(feat["pos_in_range"], 3),
                "auditor": ("立信剔除" if (sym in lixin_set) else auditor),
            })
        except Exception as e:  # noqa: BLE001
            log.debug("处理 %s 异常，跳过：%s", stk.get("symbol"), e)
            continue

    candidates.sort(key=lambda x: x["score"], reverse=True)
    qualified = candidates

    if env_count < params["env_threshold"]:
        log.warning("环境过滤通过 %d 只 < 门槛 %d → 空仓，不推个股",
                    env_count, params["env_threshold"])
        qualified = []
        position = "空仓（环境未达门槛）"
    else:
        position = "正常推个股"

    backtest = run_backtest(params)

    payload = {
        "run_date": run_date,
        "mode": "review",
        "total_scanned": total,
        "env_pass_count": env_count,
        "env_threshold": params["env_threshold"],
        "audit_veto_count": audited_out,
        "qualified_count": len(qualified),
        "position": position,
        "params": params,
        "top_samples": qualified[:top_n],
        "backtest": backtest,
    }
    archive_path = cloud_archive(payload)
    save_pending(qualified[:top_n], run_date)

    # 共同前置层：写统一推荐台账（出口 gate + 回溯/参数修正）
    _ad = globals().get("_COMMON_ADAPTER")
    if _ad:
        try:
            _ad.record("low_shadow", qualified[:top_n])
        except Exception:
            pass

    # ---- 中文简报（同时打印与推送） ----
    L = []
    L.append("## 低位上影线样本 · 收盘后模型简报")
    L.append("- 运行日：**%s**" % run_date)
    L.append("- 扫描标的总数：%d" % total)
    L.append("- 环境过滤通过：%d 只（门槛 ≥ %d）" % (env_count, params["env_threshold"]))
    L.append("- 立信审计剔除：%d 只（一票否决）" % audited_out)
    L.append("- 合格股数（审计后）：%d 只" % len(qualified))
    L.append("- 仓位状态：**%s**" % position)
    L.append("---")
    if qualified:
        L.append("前 %d 只样本：" % min(top_n, len(qualified)))
        for i, c in enumerate(qualified[:top_n], 1):
            L.append("> %d. `%s` %s | 评分 %s | 收盘 %s | 上影 %s | 区间位 %s"
                     % (i, c["symbol"], c["name"], c["score"], c["close"],
                        c["upper_shadow"], c["pos_in_range"]))
    else:
        L.append("空仓提示：当前环境/样本未达推股条件，保持空仓观望。")
    L.append("---")
    if backtest:
        L.append("- 近期胜率：%s（回溯样本 %d）" % (backtest.get("win_rate"), backtest.get("backtest_samples")))
        L.append("- 参数动作：%s" % backtest.get("action"))
    else:
        L.append("- 近期胜率：暂无（首批运行）")
        L.append("- 参数动作：维持当前参数")
    L.append("- 归档路径：%s" % archive_path)
    brief = "\n".join(L)

    print("\n" + "=" * 56)
    print("        低位上影线样本 · 收盘后模型简报")
    print("=" * 56)
    print(brief)
    print("=" * 56 + "\n")

    # 推送微信（仅在有候选或空仓决策时推送，数据异常已在上面单独推送）
    push("低位上影线模型 %s" % run_date, brief)

    return payload


def main():
    global DEMO
    DEMO = False
    # 共同前置层接入（强制）：数据入口自动切到共享样本池/K线缓存。
    _ad = None
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "..", "common"))
        import adapter as _ad
        _ad.install("low_shadow", globals())
    except Exception:
        _ad = None
    globals()["_COMMON_ADAPTER"] = _ad
    parser = argparse.ArgumentParser(description="低位上影线样本 收盘后模型")
    parser.add_argument("--mode", default="review",
                        choices=["review", "afterclose", "backtest"],
                        help="review / afterclose(别名) / backtest")
    parser.add_argument("--top", type=int, default=None, help="简报展示前 N 只")
    parser.add_argument("--demo", action="store_true",
                        help="启用内置演示数据（离线调试，生产勿用）")
    args = parser.parse_args()
    DEMO = args.demo

    params = load_params()
    try:
        if args.mode in ("review", "afterclose"):
            run_review(params, top_n=args.top)
        elif args.mode == "backtest":
            run_backtest(params)
            print("回溯与参数修正完成。")
    except Exception as e:  # noqa: BLE001
        log.error("运行异常：%s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
