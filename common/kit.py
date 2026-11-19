#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kit.py —— 所有模型（含未来新增）的统一接入框架（共同条件强制入口）
========================================================================
设计目标（用户口径）：
  「四个共同条件后面所有模型都适用，新增模型也自动匹配；即使模型条件里没写，
    也视为遗漏补上」—— 因此不靠各模型自觉调用，而是通过「数据入口收口」强制：

  ModelContext 是模型拿数据、写结果、读回溯、取参数的唯一入口：
    - ctx.universe()   → 已物理剔除「ST/退市/次新 + 立信审计」的全市场样本池（条件①③②）
    - ctx.kline(code)  → 共享的全市场行情记录 K 线缓存（条件①）
    - ctx.record(list) → 写当日推荐台账，自动进入次日回溯（条件④）
    - ctx.review()     → 读取本模型历史推荐的 T+1/5/10 回溯结算与胜率（条件④）
    - ctx.params(defaults) → 依据回溯胜率温和调参后的参数（条件④「修正模型」）
    - ctx.is_excluded(code) → 随时查询某代码是否已被共同层剔除

  由于样本池本身不含被剔除项，且 K 线/台账/参数都经 ctx，
  任何模型（哪怕代码里完全没写立信/ST/回溯）都会自动满足四条共同条件。

新增模型最小接入（3 行）：
    from common.kit import ModelContext
    ctx = ModelContext("我的新模型")
    for u in ctx.universe():            # 已剔除 ST/立信
        bars = ctx.kline(u["code"])     # 共享行情
        ... 你的选股逻辑 ...
    ctx.record(picks)                   # 自动纳入回溯与调参
    my_params = ctx.params(DEFAULT_PARAMS)

目录结构（全部落在 common/state/，由 workflow 统一回写）：
    recommendations/<model>/<date>.json   当日推荐台账
    reviews/<model>/latest.json           回溯结算结果
    params/<model>.json                   修正后的参数 + 调参历史
"""
import os
import sys
import json
import glob
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common_lib as cm

RECO_ROOT = os.path.join(cm.STATE_DIR, "recommendations")
REVIEW_ROOT = os.path.join(cm.STATE_DIR, "reviews")
PARAMS_ROOT = os.path.join(cm.STATE_DIR, "params")

# 回溯结算口径
HORIZONS = (1, 5, 10)          # T+1 / T+5 / T+10
WIN_THRESHOLD = 0.0            # 相对收益 >0 记胜（可按模型覆盖）
LOOKBACK_SESSIONS = 20         # 近 20 个推荐批次参与胜率统计
# 参数修正：胜率低于下限 → 收紧；高于上限 → 放宽；幅度有界，防漂移
TIGHTEN_BELOW = 0.42
LOOSEN_ABOVE = 0.62
ADJUST_STEP = 0.04             # 每档调整 4%
ADJUST_CAP = 0.20              # 累计相对默认最多 ±20%


def _safe_name(name):
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in name)


def _read_json(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return default


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)


class ModelContext:
    """单个模型的共同条件上下文。model 名为唯一标识（用于台账/回溯/参数隔离）。"""

    def __init__(self, model, win_threshold=WIN_THRESHOLD):
        self.model = _safe_name(model)
        self.win_threshold = win_threshold
        self.today = datetime.now(cm.BJT).strftime("%Y-%m-%d")
        self.reco_dir = os.path.join(RECO_ROOT, self.model)
        self.review_path = os.path.join(REVIEW_ROOT, self.model, "latest.json")
        self.params_path = os.path.join(PARAMS_ROOT, self.model + ".json")

    # ---- 条件①③②：样本池（ST/立信 已在 common 层物理剔除） ----
    def universe(self, board=None, price_min=None, price_max=None):
        """返回共同样本池。可附加本模型自己的过滤（不改共同层结果）。

        board: 可选，按板块过滤（如易点只要 创业板/科创板/北交所）。
        price_min/price_max: 可选，本模型专属价格门槛。
        """
        u = cm.load_universe()
        if u is None:
            # 兜底：若预筛尚未产出，则实时拉一次（仍带全部硬剔除）
            u = cm.get_universe(verbose=False)
        out = []
        for x in u:
            if board:
                boards = board if isinstance(board, (list, tuple, set)) else [board]
                if x.get("board") not in boards:
                    continue
            if price_min is not None and x.get("price", 0) < price_min:
                continue
            if price_max is not None and x.get("price", 0) > price_max:
                continue
            out.append(dict(x))
        return out

    # ---- 条件①：共享全市场行情 K 线 ----
    def kline(self, code, count=None, fallback=False):
        """读共享 K 线缓存；默认严格离线，缺失返回 None 并跳过该标的。

        fallback 仅供独立调试显式开启；统一流水线中的所有模型不得开启。
        """
        bars = cm.load_kline(code, count=count)
        if not bars and fallback:
            raw = cm._fetch_one_kline(code, count=count or cm.KLINE_CACHE_COUNT)
            if raw:
                cm._write_kline_csv(code, raw)
                bars = raw[-count:] if count else raw
        return bars

    def is_excluded(self, code):
        """查询某代码是否已被共同层剔除（立信 / ST）。供模型做二次确认。"""
        lixin = cm.load_lixin_exclude()
        if code in lixin:
            return True, "立信审计"
        u = cm.load_universe() or []
        codes = {x["code"] for x in u}
        if code not in codes:
            return True, "不在共同样本池（ST/退市/次新/无行情/立信）"
        return False, ""

    # ---- 条件④：写推荐台账 ----
    def record(self, picks, date=None, extra=None):
        """写入当日推荐。picks 每项至少含 code，可含 name/close/score 等。

        统一格式，自动成为次日回溯的输入。
        【出口强制 gate】写入前再做一次「立信 / ST / 样本池」三重校验 ——
        即使某模型自身漏写剔除、或改用了别的数据源，被禁标的也无法进入
        统一台账（并因此无法影响回溯与参数修正），实现「遗漏也算补上」。
        """
        d = date or self.today
        # gate 依据：共同样本池（已剔除 ST/退市/次新/立信/无行情）+ 立信排除集
        allowed = {x["code"] for x in (cm.load_universe() or [])}
        lixin = cm.load_lixin_exclude()
        norm, dropped = [], []
        for p in picks or []:
            code = str(p.get("code") or p.get("symbol") or "").zfill(6)
            if not code or code == "000000":
                continue
            if code in lixin:
                dropped.append({"code": code, "reason": "立信审计"})
                continue
            if allowed and code not in allowed:
                dropped.append({"code": code, "reason": "不在共同样本池(ST/退市/次新/立信/无行情)"})
                continue
            raw = {}
            for k, v in p.items():
                if isinstance(v, (str, int, float, bool, type(None))):
                    raw[k] = v
            norm.append({
                "code": code,
                "name": p.get("name", ""),
                "close": p.get("close") or p.get("price"),
                "score": p.get("score"),
                "board": p.get("board", ""),
                "raw": raw,
            })
        payload = {
            "date": d, "model": self.model, "count": len(norm),
            "picks": norm, "dropped": dropped, "extra": extra or {},
            "gate": {"lixin": len(lixin), "universe": len(allowed),
                     "dropped": len(dropped)},
            "written_at": datetime.now(cm.BJT).isoformat(timespec="seconds"),
        }
        _write_json(os.path.join(self.reco_dir, "%s.json" % d), payload)
        if dropped:
            print("[kit:%s] gate 剔除 %d 只：%s"
                  % (self.model, len(dropped),
                     ", ".join("%s(%s)" % (x["code"], x["reason"]) for x in dropped[:8])))
        return len(norm)

    # ---- 条件④：回溯结算（用共享 K 线缓存，不额外请求行情） ----
    def _load_recent_recos(self):
        files = sorted(glob.glob(os.path.join(self.reco_dir, "*.json")))
        # 排除当日，回看最近 LOOKBACK_SESSIONS 个批次
        files = [f for f in files if os.path.basename(f)[:10] < self.today]
        return files[-LOOKBACK_SESSIONS:]

    def review(self, save=True):
        """对本模型历史推荐做 T+1/5/10 回溯结算，产出胜率/平均收益。"""
        files = self._load_recent_recos()
        rows = []
        by_h = {h: {"wins": 0, "n": 0, "sum": 0.0} for h in HORIZONS}
        for f in files:
            d = _read_json(f)
            if not d:
                continue
            d0 = d.get("date")
            for p in d.get("picks", []):
                code, base = p.get("code"), p.get("close")
                if not code:
                    continue
                bars = cm.load_kline(code, offline=True)
                if not bars:
                    continue
                dates = [b["date"] for b in bars]
                # 找到推荐日之后第一根 K 线
                try:
                    idx = next(i for i, dt in enumerate(dates) if dt > d0)
                except StopIteration:
                    continue
                if base is None:
                    base = bars[idx]["close"]
                r = {"code": code, "name": p.get("name", ""), "date": d0}
                for h in HORIZONS:
                    j = idx - 1 + h
                    if j < len(bars) and base:
                        ret = bars[j]["close"] / base - 1.0
                        r["t%d" % h] = round(ret, 4)
                        by_h[h]["n"] += 1
                        by_h[h]["sum"] += ret
                        if ret > self.win_threshold:
                            by_h[h]["wins"] += 1
                rows.append(r)
        summary = {}
        for h in HORIZONS:
            n = by_h[h]["n"]
            summary["t%d" % h] = {
                "samples": n,
                "win_rate": round(by_h[h]["wins"] / n, 4) if n else None,
                "avg_return": round(by_h[h]["sum"] / n, 4) if n else None,
            }
        # 综合胜率（优先用样本最充分的 T+5，其次 T+1/T+10）
        cand = [summary["t%d" % h]["win_rate"] for h in (5, 1, 10)
                if summary["t%d" % h]["win_rate"] is not None]
        overall = round(sum(cand) / len(cand), 4) if cand else None
        result = {
            "model": self.model,
            "generated_at": datetime.now(cm.BJT).isoformat(timespec="seconds"),
            "batches": len(files), "samples": len(rows),
            "win_rate": overall, "by_horizon": summary, "details": rows[-200:],
        }
        if save:
            _write_json(self.review_path, result)
        return result

    # ---- 条件④：依据回溯胜率温和调参（「修正模型」） ----
    def params(self, defaults):
        """返回修正后的参数。defaults 为本模型的默认阈值字典。

        修正规则：近 N 批综合胜率
          < TIGHTEN_BELOW → 收紧（数值上调 ADJUST_STEP，让筛选更严）
          > LOOSEN_ABOVE  → 放宽（数值下调）
          区间内        → 维持
        仅修正「看起来像阈值」的数值参数（键含 min/max/threshold/score/count/
        ratio/pct/gate 等），且累计相对默认不超过 ADJUST_CAP，防漂移。
        修正历史持久化，可追溯、可回退。
        """
        rv = _read_json(self.review_path)
        wr = (rv or {}).get("win_rate")
        state = _read_json(self.params_path) or {"factor": 1.0, "history": []}
        factor = float(state.get("factor", 1.0))

        if wr is not None:
            if wr < TIGHTEN_BELOW:
                factor = min(1.0 + ADJUST_CAP, factor + ADJUST_STEP)
                action = "收紧"
            elif wr > LOOSEN_ABOVE:
                factor = max(1.0 - ADJUST_CAP, factor - ADJUST_STEP)
                action = "放宽"
            else:
                action = "维持"
            state["history"] = (state.get("history") or [])[-49:] + [{
                "date": self.today, "win_rate": wr, "factor": round(factor, 4),
                "action": action,
            }]
        else:
            action = "维持(无回溯样本)"
        state["factor"] = round(factor, 4)
        state["updated_at"] = datetime.now(cm.BJT).isoformat(timespec="seconds")
        _write_json(self.params_path, state)

        out = {}
        for k, v in defaults.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool) and \
                    any(t in k.lower() for t in
                        ("min", "max", "threshold", "score", "count", "top",
                         "ratio", "pct", "gate", "window", "limit", "days")):
                nv = v * factor
                out[k] = int(round(nv)) if isinstance(v, int) else round(nv, 4)
            else:
                out[k] = v
        out["__factor"] = state["factor"]
        out["__win_rate"] = wr
        out["__action"] = action
        return out
