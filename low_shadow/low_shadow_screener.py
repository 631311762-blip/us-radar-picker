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
import csv
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
MARKET_ENV_FILE = os.path.join(ARCHIVE_DIR, "market_env.jsonl")  # L1 每日环境快照

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
                # p[6] = f57 成交额(元)，与原型盘前主路径 f6 同口径；
                # 缺失时回退 成交量*收盘价（仅 demo / 异常兜底）。
                amount = _to_float(p[6]) if len(p) > 6 else 0.0
                if amount <= 0:
                    amount = float(p[5]) * float(p[2])
                bars.append({
                    "date": p[0], "open": float(p[1]), "close": float(p[2]),
                    "high": float(p[3]), "low": float(p[4]), "vol": float(p[5]),
                    "amount": amount,
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
# 原型 us_radar_picker.py::SHADOW 的 8 道硬条件（逐字对齐，勿随意放宽）
DEFAULT_PARAMS = {
    # --- 形态硬条件（原型 SHADOW）---
    "upper_amp_min": 0.045,         # 上影/开盘 >= 4.5%（放宽：让凌志软件0.0488类低位票纳入）
    "upper_to_body_min": 2.0,       # 上影/实体 >= 2 倍
    "body_abs_max_pct": 0.07,       # 当日涨跌幅绝对值 <= 7%
    "vol_ratio_min": 1.5,           # 量比(对前5日均量) >= 1.5
    "low_pos_max_pct": 0.15,        # 距20日低点涨幅 <= 15%
    "below_ma20": True,             # 收盘必须低于 MA20（硬性）
    "min_amount": 5e7,              # 当日成交额 >= 5000万（f57 真实元）
    "shadow_comp_enabled": True,    # 启用「次选路径」
    "comp_shadow_min": 0.06,        # 次选：上影/开盘 >= 6%
    "comp_upper_to_body_min": 3.5,  # 次选：上影/实体 >= 3.5
    "comp_low_pos_max_pct": 0.05,   # 次选：距20日低点 <= 5%
    # --- 框架参数 ---
    "lookback": 120,          # K线回看窗口（>=21 根即可判定末根）
    "top_n": 6,               # 简报展示前 N 只（原型 topN）
    "candidate_pool": 0,      # 0=不截断，每只满足模型的股票都进清单（用户要求剔除60上限）
    "env_threshold": 60,      # 合格候选数 >= 此值视为「信号异常放量」告警（非空仓开关）
    "vol_mult": 1.20,         # 保留兼容（旧 env 统计用）
    # --- H5 精选层（2026-11-14 用户拍板落地）---
    # 10 月至今 27 交易日全市场回测：基线 322 只 → H5 123 只，
    # 3 日命中 54.1% / +1.82%，5 日命中 57.5% / +2.61%，且保留凌志软件类低上影票。
    # 原型 8 道硬条件保持不变，H5 作为其后的独立精选层。
    "h5_enabled": True,
    "h5_board_prefix": ["300", "301", "688", "689"],  # 只留创业板/科创板（隐含排除北交所与主板）
    "h5_max_drop_pct": -3.0,   # 当日跌幅 <= -3% 剔除（破位非试盘，回测 28 只 5 日 -1.83%）
    "h5_vol_ratio_max": 3.0,   # 量比 > 3 剔除（过度放量，回测 20 只 5 日 -1.68%）
    "h5_amount_max": 1e9,      # 成交额 > 10 亿剔除（大额票是主要亏损源）
    # --- 自动调参保护（防单样本噪声驱动参数漂移）---
    "param_lock": True,        # 人工设定参数（含 H5）不允许被 correct_params 覆盖
    "min_tune_samples": 20,    # 已完成回溯样本 < 20 时禁止调参
    # --- L1 大盘环境标注（2026-11-14 落地，只加字段/文本，不参与筛选）---
    "env_annotation_enabled": True,
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
            lo = cl * (1 - rng.uniform(0.0, 0.01))
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
    """从K线提取末根特征（对齐原型 shadow_evaluate 的 8 道硬条件所需字段）。

    成交额 amount 取东财 K 线 f57（真实元，与原型盘前主路径 f6 同口径）；
    缺失时回退 成交量*收盘价。至少需要 21 根日K（20 根窗口 + 当日）。
    """
    if not recs or len(recs) < 21:
        return None
    try:
        recs = sorted(recs, key=lambda x: x["date"])
        last = recs[-1]
        o, h, l, c, v = (last["open"], last["high"], last["low"],
                          last["close"], last["vol"])
        if o <= 0 or h <= 0:
            return None
        body_abs = abs(c - o)
        upper = h - max(o, c)
        upper_amp = upper / o                                   # 上影/开盘
        upper_to_body = upper / body_abs if body_abs > 0 else 99.0

        win = recs[-20:]                                        # 含当日共 20 根
        low_20 = min(r["low"] for r in win)
        ma20 = sum(r["close"] for r in win) / len(win)
        prev5 = recs[-6:-1]                                     # 当日前 5 根
        vol_ma5 = sum(r["vol"] for r in prev5) / len(prev5) if prev5 else v
        vol_ratio = v / vol_ma5 if vol_ma5 > 0 else 0.0
        pos_pct = (c - low_20) / low_20 if low_20 > 0 else 99.0
        prev_close = recs[-2]["close"]
        pct_chg = (c / prev_close - 1) * 100 if prev_close > 0 else 0.0
        # 成交额(元)：东财直连路径由 em_hist 解析 f57 得到，与原型盘前主路径 f6 同口径。
        # 共同前置层共享 K 线缓存路径不返回 amount，其 vol 单位为「手」，
        # 故 ×100 换算成股数再 ×收盘价近似成交额，避免 v*c 偏小 100 倍误杀全部信号。
        amount = last.get("amount") or 0.0
        if amount <= 0:
            amount = v * 100.0 * c

        main_path = upper_amp >= params["upper_amp_min"]
        comp_path = (params["shadow_comp_enabled"]
                     and upper_amp >= params["comp_shadow_min"]
                     and upper_to_body >= params["comp_upper_to_body_min"])

        # 兼容旧字段（简报/回溯）
        bar_rng = h - l
        upper_shadow = upper / bar_rng if bar_rng > 0 else 0.0
        closes = [r["close"] for r in recs]
        ma60 = sum(closes[-60:]) / min(60, len(closes))

        return {
            "date": last["date"], "close": c,
            "upper_amp": upper_amp, "upper_to_body": upper_to_body,
            "vol_ratio": vol_ratio, "pos_pct": pos_pct,
            "pct_chg": pct_chg, "ma20": ma20, "low_20": low_20,
            "amount": amount,
            "main_path": main_path, "comp_path": comp_path,
            "is_comp": bool(comp_path and not main_path),
            # 兼容字段
            "upper_shadow": upper_shadow, "pos_in_range": min(pos_pct, 1.0),
            "below_ma": c < ma60, "ma60": round(ma60, 2),
        }
    except Exception as e:  # noqa: BLE001
        log.debug("特征提取失败：%s", e)
        return None


def _check_shadow(feat, params):
    """原型 8 道硬条件。返回 (是否通过, 淘汰原因)。"""
    if feat is None:
        return False, "K线不足"
    if not (feat["main_path"] or feat["comp_path"]):
        return False, "上影幅度"
    if feat["upper_to_body"] < params["upper_to_body_min"]:
        return False, "上影实体比"
    if feat["vol_ratio"] < params["vol_ratio_min"]:
        return False, "量比"
    pos_limit = (params["comp_low_pos_max_pct"] if feat["is_comp"]
                 else params["low_pos_max_pct"])
    if feat["pos_pct"] > pos_limit:
        return False, "低位"
    if feat["amount"] < params["min_amount"]:
        return False, "成交额"
    if abs(feat["pct_chg"]) > params["body_abs_max_pct"] * 100:
        return False, "涨跌幅"
    if params["below_ma20"] and feat["close"] >= feat["ma20"]:
        return False, "MA20"
    return True, "通过"


def is_low_shadow(feat, params):
    """形态筛：对齐原型 us_radar_picker.py::shadow_evaluate 的 8 道硬条件。"""
    return _check_shadow(feat, params)[0]


def shadow_diagnose(feat, params):
    """返回未通过的首个硬条件名，用于筛选漏斗复盘。"""
    return _check_shadow(feat, params)[1]


def h5_check(symbol, feat, params):
    """H5 精选层：在原型 8 道硬条件之后叠加的过滤规则。

    10 月至今回测（322 → 123 只，5 日均收益 -0.04% → +2.61%）落地的生产口径：
      只留创业板/科创板 + 排除当日跌>3% + 排除量比>3 + 排除成交额>10亿。
    返回 (是否通过, 淘汰原因)；h5_enabled=False 时恒通过（便于 A/B 对照）。
    """
    if not params.get("h5_enabled"):
        return True, "通过"
    if feat is None:
        return False, "K线不足"
    prefixes = tuple(params.get("h5_board_prefix") or ())
    if prefixes and not str(symbol).startswith(prefixes):
        return False, "板块(仅创/科)"
    if feat["pct_chg"] <= params.get("h5_max_drop_pct", -3.0):
        return False, "当日跌幅>3%"
    if feat["vol_ratio"] > params.get("h5_vol_ratio_max", 3.0):
        return False, "量比>3"
    if feat["amount"] > params.get("h5_amount_max", 1e9):
        return False, "成交额>10亿"
    return True, "通过"


def pct_rank(values):
    """分位排名（逐字对齐原型 us_radar_picker.py::pct_rank）。"""
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


def rank_candidates(cands):
    """原型 shadow_rank：候选池内分位评分（0~100）后排序，无绝对分数线。"""
    if not cands:
        return []
    s_upper = pct_rank([c["upper_amp"] for c in cands])
    s_vol = pct_rank([c["vol_ratio"] for c in cands])
    s_low = pct_rank([-c["pos_pct"] for c in cands])
    s_ratio = pct_rank([c["upper_to_body"] for c in cands])
    for i, c in enumerate(cands):
        c["score"] = round((s_upper[i] * 0.30 + s_vol[i] * 0.25
                            + s_low[i] * 0.25 + s_ratio[i] * 0.20) * 100, 2)
    cands.sort(key=lambda x: x["score"], reverse=True)
    return cands


# ----------------------------------------------------------------------------
# 环境参考（旧版 env_pass 计数恒真，已不作为空仓开关；保留仅供归档统计）
# ----------------------------------------------------------------------------
def env_pass(recs, params):
    feat = extract_last_features(recs, params)
    if feat is None:
        return False
    if feat["pos_in_range"] >= 0.85:
        return False
    return True


# ----------------------------------------------------------------------------
# 推送（PushPlus，与仓库其他模型统一）
# ----------------------------------------------------------------------------
# ----------------------------------------------------------------------------
# L1 大盘环境标注与推荐意见（2026-11-14 落地）
# 只写归档字段与简报文本，不参与任何筛选 / 评分 / 参数决策，零风险。
# 依据：state/env_factor_result.json（2026-10-08~11-13，27 交易日全市场回测）
#   · 全市场等权当日涨跌 ↔ 信号数 Pearson -0.708 / Spearman -0.782
#   · 强 / 中性 / 弱 三档 H5 5 日收益 +2.05% / -0.66% / +3.64%
#   · 「大盘转强才推」闸门实测 5 日 -3.21%，已证伪 → 本模块不做闸门
# ----------------------------------------------------------------------------
# 各环境档历史表现（H5 123 只样本，由 env_factor.py 回测产出）
ENV_EVIDENCE = {
    "强":   {"days": 12, "n": 24, "r3_hit": 60.9, "r3": 1.67,
             "r5_hit": 60.0, "r5": 2.05, "r10": -2.96},
    "中性": {"days": 4,  "n": 18, "r3_hit": 22.2, "r3": -0.48,
             "r5_hit": 38.9, "r5": -0.66, "r10": -7.27},
    "弱":   {"days": 11, "n": 81, "r3_hit": 60.3, "r3": 2.48,
             "r5_hit": 61.8, "r5": 3.64, "r10": 2.74},
}
CROWD_THRESHOLD = 16   # 当日 8 道硬条件通过数 >= 16 视为「信号拥挤」


def _load_env_history():
    """读取每日环境快照（最多保留最近 60 条）。失败返回空列表，不影响主流程。"""
    try:
        if not os.path.exists(MARKET_ENV_FILE):
            return []
        rows = []
        with open(MARKET_ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows[-60:]
    except Exception as e:  # noqa: BLE001
        log.debug("环境快照读取失败：%s", e)
        return []


def _append_env_history(rec):
    """追加当日环境快照（同日去重）。失败静默降级，绝不影响出股。"""
    try:
        ensure_archive()
        rows = [r for r in _load_env_history() if r.get("date") != rec.get("date")]
        rows.append(rec)
        with open(MARKET_ENV_FILE, "w", encoding="utf-8") as f:
            for r in rows[-60:]:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        log.debug("环境快照写入失败：%s", e)


def compute_market_env(universe, run_date):
    """从全市场样本池直接计算大盘环境（零额外接口、零前视）。

    universe 每行需含 pct（百分数数值，如 -2.07 表示 -2.07%）；
    common/adapter.universe_for() 与东财 fltt=2 列表均满足该口径。
    """
    try:
        pcts = [float(s["pct"]) for s in universe
                if isinstance(s.get("pct"), (int, float))]
    except Exception:  # noqa: BLE001
        pcts = []
    if not pcts:
        return {"available": False, "reason": "样本池无有效涨跌幅字段"}

    eq = sum(pcts) / len(pcts)
    adv = sum(1 for x in pcts if x > 0)
    dec = sum(1 for x in pcts if x < 0)
    flat = len(pcts) - adv - dec
    adv_ratio = 100.0 * adv / len(pcts)
    # 环境档与回测口径严格一致：>0 强；[-1, 0] 中性；< -1 弱
    regime = "强" if eq > 0 else ("弱" if eq < -1.0 else "中性")
    breadth = ("普涨" if adv_ratio >= 75 else "偏强" if adv_ratio >= 55
               else "分歧" if adv_ratio >= 40 else "偏弱" if adv_ratio >= 20 else "普跌")

    hist = [r for r in _load_env_history() if r.get("date") != run_date]
    eq_r5 = None
    if len(hist) >= 5:
        z = 1.0
        for r in hist[-5:]:
            v = r.get("eq_chg_pct")
            if isinstance(v, (int, float)):
                z *= (1 + v / 100.0)
        eq_r5 = round((z - 1) * 100, 2)
    past_n = [r.get("n_base") for r in hist[-5:]
              if isinstance(r.get("n_base"), (int, float))]
    avg_n5 = round(sum(past_n) / len(past_n), 1) if past_n else None

    return {
        "available": True, "date": run_date, "universe_n": len(pcts),
        "eq_chg_pct": round(eq, 3), "adv_count": adv, "dec_count": dec,
        "flat_count": flat, "adv_ratio_pct": round(adv_ratio, 2),
        "breadth": breadth, "regime": regime,
        "eq_r5_pct": eq_r5, "avg_n_base_5d": avg_n5,
        "history_days": len(hist),
    }


def env_advice(env, n_qualified, n_base):
    """结合大盘环境档与信号拥挤度给出推荐意见（纯文本，不参与筛选）。"""
    if not env.get("available"):
        return {"stance": "维持常规", "holding": "按模型默认执行",
                "risk": "大盘环境数据不可用（%s），本日不启用环境加权" % env.get("reason", "未知"),
                "evidence": None, "regime": None, "crowded": False,
                "lines": ["- 大盘环境：数据不可用（%s），按常规执行"
                          % env.get("reason", "未知")]}

    rg = env["regime"]
    ev = ENV_EVIDENCE[rg]
    crowded = n_base >= CROWD_THRESHOLD
    avg5 = env.get("avg_n_base_5d")
    crowd_note = ("，近 5 日均 %s 只" % avg5) if avg5 else ""
    eq_r5 = env.get("eq_r5_pct")

    if rg == "强":
        stance = "正常参与（信号稀缺属正常，精选为主，不必凑数）"
        holding = "T+3~5 止盈，勿持有超过 5 日"
        risk = "强市 10 日收益 −2.96%，反弹不持久；大盘转强时本模型信号天然稀少，残留票需防「没跟上反弹」"
    elif rg == "中性":
        stance = "减半仓 / 观望（本模型最差环境）"
        holding = "T+3 内严格止损，不建议持有到 5 日以上"
        risk = "中性区间全周期亏损：3 日命中仅 22.2%、5 日 −0.66%、10 日 −7.27%"
    else:
        if crowded:
            stance = "可正常至积极参与（超跌反转窗口）"
            holding = "T+5~10，可适当放宽持有；分批建仓、不追高"
            risk = "弱市拥挤日 5 日命中 61.8%、均收益 +3.64%，10 日仍 +2.74%；极拥挤档仅 1 天，忌单日满仓"
        else:
            stance = "轻仓试探，等信号拥挤确认"
            holding = "T+3~5，快进快出"
            risk = "弱势但信号尚未爆发，尚非反转确认；控制仓位，等基线信号数 ≥%d 再加大" % CROWD_THRESHOLD

    extra = []
    if env["breadth"] in ("普跌", "偏弱") and rg == "弱":
        extra.append("全市场%s（上涨占比 %.1f%%）—— 历史看普跌反而利好本模型的超跌反转逻辑"
                     % (env["breadth"], env["adv_ratio_pct"]))
    elif env["breadth"] in ("普涨", "偏强") and rg == "强":
        extra.append("全市场%s（上涨占比 %.1f%%）—— 注意本模型信号可能极少"
                     % (env["breadth"], env["adv_ratio_pct"]))
    if eq_r5 is not None:
        if eq_r5 <= -3:
            extra.append("近 5 日全市场等权 %.2f%%，深跌背景，反转弹性较大" % eq_r5)
        elif eq_r5 >= 2:
            extra.append("近 5 日全市场等权 +%.2f%%，反弹已持续，谨慎追高" % eq_r5)

    ev_txt = ("依据（10-08~11-13 回测，%s档 %d 天 / H5 %d 只）：3 日命中 %.1f%% / %+.2f%%，"
              "5 日命中 %.1f%% / %+.2f%%，10 日 %+.2f%%"
              % (rg, ev["days"], ev["n"], ev["r3_hit"], ev["r3"],
                 ev["r5_hit"], ev["r5"], ev["r10"]))
    lines = [
        "- 大盘环境：**%s**（全市场等权 %+.2f%%，上涨占比 %.1f%%，%s）" % (
            rg, env["eq_chg_pct"], env["adv_ratio_pct"], env["breadth"]),
        "- 信号拥挤度：8 道硬条件通过 **%d 只** / H5 精选 **%d 只**%s%s" % (
            n_base, n_qualified, crowd_note, "，**信号拥挤**" if crowded else ""),
    ]
    if eq_r5 is not None:
        lines.append("- 趋势背景：全市场等权近 5 日 %+.2f%%" % eq_r5)
    lines += [
        "- **仓位建议：%s**" % stance,
        "- **持有 / 止盈：%s**" % holding,
        "- %s" % ev_txt,
        "- 风险提示：%s" % risk,
    ]
    for x in extra:
        lines.append("- 补充：%s" % x)
    return {"stance": stance, "holding": holding, "risk": risk,
            "evidence": ev, "regime": rg, "crowded": crowded,
            "lines": lines}


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


def save_full_list(qualified, meta, run_date):
    """全市场合格清单完整落盘（JSON + CSV），不截断，供复盘/核对。"""
    ensure_archive()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    jpath = os.path.join(ARCHIVE_DIR, "qualified_%s.json" % ts)
    cpath = os.path.join(ARCHIVE_DIR, "qualified_%s.csv" % ts)
    cols = ["symbol", "name", "score", "date", "close", "upper_amp",
            "upper_to_body", "vol_ratio", "pos_pct", "amount", "pct_chg",
            "ma20", "low_20", "comp_path"]
    try:
        with open(jpath, "w", encoding="utf-8") as f:
            json.dump({"run_date": run_date,
                       "total_scanned": meta.get("total_scanned"),
                       "qualified_count": len(qualified),
                       "market_env": meta.get("market_env"),
                       "advice": meta.get("advice"),
                       "candidates": qualified}, f, ensure_ascii=False, indent=2)
        with open(cpath, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for c in qualified:
                w.writerow([c.get(k, "") for k in cols])
        log.info("全量合格清单：%s / %s（%d 只）", jpath, cpath, len(qualified))
        return jpath, cpath
    except Exception as e:  # noqa: BLE001
        log.warning("全量清单写入失败：%s", e)
        return None, None


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
    n_recent = 0
    try:
        rows = []
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        recent = rows[-20:]
        n_recent = len(recent)
        if recent:
            wins = sum(1 for r in recent if r.get("win"))
            win_rate = round(wins / len(recent), 3)
            action = correct_params(params, win_rate, n_recent)
    except Exception as e:  # noqa: BLE001
        log.warning("历史读取失败：%s", e)

    if os.path.exists(PENDING_FILE):
        try:
            os.remove(PENDING_FILE)
        except Exception:  # noqa: BLE001
            pass
    log.info("回溯完成：样本 %d，近期胜率 %s，参数动作：%s",
             len(new_hist), win_rate, action)
    return {"backtest_samples": len(new_hist), "n_recent": n_recent,
            "win_rate": win_rate, "action": action}


def correct_params(params, win_rate, n_recent=0):
    """依据近期胜率修正阈值；带人工锁定与最小样本门槛。

    历史缺陷：仅 1 个回溯样本（胜率 0.0）就一次收紧三个参数，把人工设定的
    0.045 改写成 0.055，并导致归档参数与本次筛选口径不一致。
    修正：param_lock=True 时人工参数不被覆盖；样本不足 min_tune_samples 时
    不调参；调参时一次只动一个参数并打印 diff，便于归因。
    """
    if params.get("param_lock"):
        return "参数已锁定（人工设定，自动调参跳过）"
    need = params.get("min_tune_samples", 20)
    if win_rate is None or n_recent < need:
        return "维持当前参数（回溯样本 %d < %d，样本不足）" % (n_recent, need)
    before = dict(params)
    if win_rate < 0.45:
        k = "upper_amp_min"
        params[k] = min(0.15, round(params[k] + 0.01, 3))
        action = "收紧阈值（胜率偏低）：%s %.3f→%.3f" % (k, before[k], params[k])
    elif win_rate > 0.60:
        k = "upper_amp_min"
        params[k] = max(0.045, round(params[k] - 0.01, 3))
        action = "放宽阈值（胜率偏高）：%s %.3f→%.3f" % (k, before[k], params[k])
    else:
        return "维持当前参数"
    diff = {k: [before[k], params[k]] for k in before if before.get(k) != params.get(k)}
    log.info("参数修正 diff：%s", diff)
    save_params(params)
    return action


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def run_review(params, top_n=None):
    top_n = top_n or params["top_n"]
    run_date = datetime.now().strftime("%Y-%m-%d")
    # 归档必须记录「本次筛选实际使用」的参数：run_backtest() 会在筛选之后
    # 就地修改 params，若直接引用会写出与清单口径不符的参数（历史缺陷）。
    params_snapshot = dict(params)
    log.info("=== 低位上影线样本 · 收盘后复盘 ===  运行日 %s  H5=%s",
             run_date, params.get("h5_enabled"))

    universe = get_universe()
    if not universe:
        msg = "低位上影线模型 %s 数据获取失败（行情接口无返回），今日不出股。" % run_date
        log.error(msg)
        push("低位上影线模型 %s 数据异常" % run_date, msg)
        return None

    lixin_set, lixin_ok = load_lixin_exclude()
    if not lixin_ok:
        log.warning("立信排除集尚未生成（请先运行领湃），本日立信剔除为 0")

    env_enabled = bool(params.get("env_annotation_enabled"))
    env_base = (compute_market_env(universe, run_date) if env_enabled
                else {"available": False, "reason": "env_annotation_enabled=False"})
    total = len(universe)                 # 全市场样本总数（非抽样）
    data_ok = 0
    audited_out = 0
    funnel = {}
    h5_funnel = {}
    candidates = []

    for stk in universe:
        try:
            sym = stk["symbol"]
            recs = get_kline(sym, params["lookback"])
            if not recs or len(recs) < 21:
                continue
            data_ok += 1
            # 立信审计一票否决（复用领湃排除集 + 名称黑名单兜底）
            auditor = get_auditor(sym)
            if sym in lixin_set or auditor in AUDIT_BLACKLIST:
                audited_out += 1
                continue
            feat = extract_last_features(recs, params)
            if not is_low_shadow(feat, params):
                reason = shadow_diagnose(feat, params)
                funnel[reason] = funnel.get(reason, 0) + 1
                continue
            # H5 精选层：在 8 道硬条件通过者之上再过滤，独立漏斗统计
            ok_h5, h5_reason = h5_check(sym, feat, params)
            if not ok_h5:
                h5_funnel[h5_reason] = h5_funnel.get(h5_reason, 0) + 1
                continue
            candidates.append({
                "symbol": sym, "name": stk.get("name", ""),
                "date": feat["date"], "close": feat["close"],
                "upper_amp": round(feat["upper_amp"], 4),
                "upper_to_body": round(feat["upper_to_body"], 2),
                "vol_ratio": round(feat["vol_ratio"], 3),
                "pos_pct": round(feat["pos_pct"], 4),
                "amount": round(feat["amount"], 0),
                "pct_chg": round(feat["pct_chg"], 2),
                "ma20": round(feat["ma20"], 2),
                "low_20": round(feat["low_20"], 2),
                "comp_path": feat["is_comp"],
                "auditor": auditor,
            })
        except Exception as e:  # noqa: BLE001
            log.debug("处理 %s 异常，跳过：%s", stk.get("symbol"), e)
            continue

    # 全市场合格清单：8 道硬条件 + H5 精选层全部通过者，不截断
    log.info("H5 精选层漏斗：%s", h5_funnel)
    qualified = rank_candidates(candidates)
    n_qualified = len(qualified)
    # 候选池：candidate_pool<=0 表示不截断（每只满足模型的股票都进清单）；>0 才取前N
    pool = qualified if not params["candidate_pool"] else qualified[:params["candidate_pool"]]

    if n_qualified == 0:
        position = "空仓（全市场无符合 8 道硬条件 + H5 精选的信号）"
    elif n_qualified >= params["env_threshold"]:
        position = "正常推个股（信号数达门槛，疑似口径/行情异常，请复核）"
    else:
        position = "正常推个股"
    log.info("8 道硬条件筛选漏斗：%s", funnel)

    # L1：把当日信号拥挤度并入环境，生成推荐意见（不参与筛选）
    n_base = n_qualified + sum(h5_funnel.values())
    if env_enabled:
        market_env = dict(env_base)
        market_env.update({
            "n_qualified": n_qualified, "n_base": n_base,
            "crowded": n_base >= CROWD_THRESHOLD,
            "crowd_ratio": (round(n_base / env_base["avg_n_base_5d"], 2)
                            if env_base.get("avg_n_base_5d") else None),
        })
        advice = env_advice(market_env, n_qualified, n_base)
        _append_env_history({
            "date": run_date, "eq_chg_pct": env_base.get("eq_chg_pct"),
            "adv_ratio_pct": env_base.get("adv_ratio_pct"),
            "regime": env_base.get("regime"),
            "n_base": n_base, "n_qualified": n_qualified,
        })
    else:
        market_env = {"available": False, "reason": "env_annotation_enabled=False"}
        advice = env_advice(market_env, n_qualified, n_base)
    log.info("L1 大盘环境：%s / %s / 仓位=%s", market_env.get("regime"),
             market_env.get("breadth"), advice["stance"])

    backtest = run_backtest(params)

    payload = {
        "run_date": run_date,
        "mode": "review",
        "total_scanned": total,           # 全市场 A 股全量，非抽样
        "data_ok_count": data_ok,
        "audit_veto_count": audited_out,
        "qualified_count": n_qualified,   # 全市场符合条件总数
        "candidate_pool": params["candidate_pool"],
        "position": position,
        "funnel": funnel,                 # 8 道硬条件淘汰数
        "h5_funnel": h5_funnel,           # H5 精选层淘汰数
        "h5_enabled": params_snapshot.get("h5_enabled"),
        "params": params_snapshot,        # 本次筛选实际使用的参数
        "params_after_tune": dict(params),  # 回溯调参后的状态（供审计，不影响本次结果）
        "top_samples": pool[:top_n],
        "backtest": backtest,
        "market_env": market_env,         # L1 大盘环境（只标注，不参与筛选）
        "advice": advice,                 # L1 结合市场因素的推荐意见
    }
    full_json, full_csv = save_full_list(qualified, payload, run_date)
    payload["full_list_json"] = full_json
    payload["full_list_csv"] = full_csv
    archive_path = cloud_archive(payload)
    save_pending(pool, run_date)

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
    L.append("- 样本总数：**%d 只（全市场 A 股全量扫描，非抽样）**" % total)
    L.append("- 有效K线标的：%d 只" % data_ok)
    L.append("- 立信审计剔除：%d 只（一票否决）" % audited_out)
    L.append("- 符合 8 道硬条件 + H5 精选：**%d 只**（全量清单见下方）" % n_qualified)
    L.append("- 满足模型清单：%d 只（已剔除 60 上限，每只满足的都列出）" % n_qualified)
    L.append("- 仓位状态：**%s**" % position)
    L.append("---")
    if pool:
        L.append("前 %d 只预览（满足模型全量 %d 只，完整见 CSV/JSON）：" % (min(top_n, len(pool)), n_qualified))
        for i, c in enumerate(pool, 1):
            L.append("> %d. `%s` %s | 评分 %s | 收盘 %s | 上影/开 %s | 上影/实体 %s | 量比 %s | 距20日低 %s"
                     % (i, c["symbol"], c["name"], c["score"], c["close"],
                        c["upper_amp"], c["upper_to_body"], c["vol_ratio"], c["pos_pct"]))
    else:
        L.append("空仓提示：今日全市场无同时满足 8 道硬条件 + H5 精选的标的，保持空仓观望。")
    L.append("---")
    if env_enabled:
        L.append("### 市场环境与推荐意见（L1 标注，不影响筛选结果）")
        L.extend(advice["lines"])
        L.append("---")
    L.append("- 筛选漏斗（8 道硬条件淘汰数）：%s" % funnel)
    L.append("- H5 精选层（启用：%s，淘汰数）：%s" % (params.get("h5_enabled"), h5_funnel))
    if backtest:
        L.append("- 近期胜率：%s（回溯样本 %d）" % (backtest.get("win_rate"), backtest.get("backtest_samples")))
        L.append("- 参数动作：%s" % backtest.get("action"))
    else:
        L.append("- 近期胜率：暂无（首批运行）")
        L.append("- 参数动作：维持当前参数")
    L.append("- 全量合格清单：%s" % (full_json or "写入失败"))
    if full_csv:
        L.append("- 全量清单(CSV)：%s" % full_csv)
    L.append("- 归档路径：%s" % archive_path)
    brief = "\n".join(L)

    print("\n" + "=" * 56)
    print("        低位上影线样本 · 收盘后模型简报")
    print("=" * 56)
    print(brief)
    print("=" * 56 + "\n")

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
