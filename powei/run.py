#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
破位后上涨模型（假破位洗盘 / False-Breakdown Reclaim）  powei/run.py
============================================================================
纳入「A股模型统一流水线」（a-share-pipeline.yml）的规范模型模块。

形态逻辑（与研究报告 V1.0 一致）：
    长期横盘 → 形成水平支撑 S（多触点地板） → 某日收盘破位（诱空 / 假破位）
    → 1~RECLAIM_WIN 日内重新站回 S（收复） → 视为「刚收复支撑」的博反弹候选。

接入契约（满足「合并流水线 · 样本要求」）：
  1) 数据入口只走 common.kit.ModelContext：
       ctx.universe()  → 已物理剔除 ST/退市/次新/立信审计 的全市场样本池
       ctx.kline(code) → 共同前置层共享 K 线缓存（前复权）
     —— 自动吃到四个共同条件，且不被模型自身是否写剔除所影响。
  2) 推荐经 ctx.record(picks) 落盘：
       common/state/recommendations/powei/<date>.json
     —— 按模型名(powei)隔离，流水线统一回写，天然「区分」于其它模型。
  3) 自有台账/归档写入 powei/state/（由 workflow `git add powei/state` 回写）：
       state/summary.json        机器可读当日摘要
       state/index.jsonl         每日一行索引
       state/archive/<date>.json / .csv  全量候选
       state/history.jsonl       候选历史（供次日回溯）
       state/params.json         模型常量（阈值集中，便于修正）
  4) 推送走 PushPlus（markdown），与其它模型统一。

运行：
    python3 powei/run.py                 # 收盘后复盘（主模式，默认）
    python3 powei/run.py --mode review   # 同默认
    python3 powei/run.py --mode backtest # 回溯上一日候选、更新胜率与参数
    python3 powei/run.py --selftest      # 离线自检：用已知 3 样本验证检测逻辑（新浪数据）

模型定性：高胜率「博反弹」信号（反弹几乎必到，但持有收益弱），
务必配合「支撑下 3~5% 止损 / +8~12% 止盈」使用；本脚本仅做研究，非投资建议。

操作策略（「管三天」，每次推荐必附，详见 OPER_RULE / op_plan）：
    提示日=收复信号日；T+1 缩量小涨即买；T+1 上影/绿阴则第三日开盘低位买；
    买入后第 3 日强制卖出（固定持有 ≤3 日）。
"""
import argparse
import csv
import json
import os
import sys
import time
import datetime
from collections import Counter

# ---- 路径：让 `import common` 在 cwd=仓库根 与 cwd=powei/ 两种情况下都成立 ----
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
for _p in (REPO, os.path.join(REPO, "common")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

MODEL_NAME = "powei"
MODEL_LABEL = "破位后上涨模型（假破位收复）"

# 自有台账目录（绝对值，与 cwd 无关）
STATE_DIR = os.path.join(HERE, "state")
ARCHIVE_DIR = os.path.join(STATE_DIR, "archive")
HISTORY_FILE = os.path.join(STATE_DIR, "history.jsonl")
SUMMARY_FILE = os.path.join(STATE_DIR, "summary.json")
PARAMS_FILE = os.path.join(HERE, "params.json")
for _d in (STATE_DIR, ARCHIVE_DIR):
    os.makedirs(_d, exist_ok=True)

# ============================ 模型常量（阈值集中）============================
# 与研究报告 V1.0 一致；改阈值只动这里并 push 即可（流水线下一次自动生效）。
CONST = {
    "WIN": 80,          # 支撑观察窗（交易日，约 3-4 个月）
    "RANGE_MAX": 0.40,  # 横盘最大振幅 (hi-lo)/mid
    "TOUCH_MIN": 3,     # 支撑最小触点次数（多触点地板）
    "BREAK_PCT": 0.98,  # 破位阈值：收盘 < 支撑 * 0.98
    "RECLAIM_WIN": 12,  # 破位后收复窗口（交易日）
    "RECLAIM_PCT": 1.00,# 收复阈值：收盘 >= 支撑 * 1.00
    "MAX_AGE": 3,       # 仅输出近 N 个交易日发生收复的信号
    "TOP_N": 30,        # 进入推荐台账 / 推送的前 N 只
    "KLINE_COUNT": 330, # 拉取的 K 线根数
}
# 回写 params.json 供修正模型读取
try:
    with open(PARAMS_FILE, "w", encoding="utf-8") as _f:
        json.dump({"model": MODEL_NAME, "const": CONST,
                   "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")},
                  _f, ensure_ascii=False, indent=2)
except Exception:
    pass


def log(*a):
    print("[%s]" % datetime.datetime.now().strftime("%H:%M:%S"), *a, flush=True)


# ============================ 行情归一化 ============================
def _norm(bars):
    """把任意来源的 K 线（新浪 'day' / 共同层 'date'）统一成检测所需结构。"""
    out = []
    for b in bars or []:
        d = b.get("day") or b.get("date")
        if not d:
            continue
        try:
            out.append({
                "day": d,
                "open": float(b.get("open")),
                "high": float(b.get("high")),
                "low": float(b.get("low")),
                "close": float(b.get("close")),
                "volume": float(b.get("volume") or 0),
            })
        except (TypeError, ValueError):
            continue
    return out


# ============================ 形态检测 ============================
def detect(bars, const=None):
    """返回该股票所有「假破位+收复」事件。events 含支撑/破位日/信号日/量比/各周期收益/距今。"""
    c = const or CONST
    WIN = c["WIN"]; RANGE_MAX = c["RANGE_MAX"]; TOUCH_MIN = c["TOUCH_MIN"]
    BREAK_PCT = c["BREAK_PCT"]; RECLAIM_WIN = c["RECLAIM_WIN"]; RECLAIM_PCT = c["RECLAIM_PCT"]
    if len(bars) < WIN + 20:
        return []
    N = len(bars)
    events = []
    t = WIN
    # 循环上限放宽到 N-13：破位后最多 12 日收复，须留到末根以支持「当日刚收复」信号
    while t < N - 13:
        win = bars[t - WIN:t]
        lows = [x["low"] for x in win]; highs = [x["high"] for x in win]
        lo_all = min(lows); hi_all = max(highs); mid = (lo_all + hi_all) / 2
        if mid <= 0 or (hi_all - lo_all) / mid > RANGE_MAX:
            t += 1; continue
        # 多触点水平支撑：在区间下半部扫描，统计 low 落在 [S*0.98, S*1.03] 的天数
        best = None; bestcnt = 0
        for k in range(0, 26):
            cand = lo_all + (hi_all - lo_all) * 0.02 * k
            if cand > lo_all + (hi_all - lo_all) * 0.55:
                break
            cnt = sum(1 for x in win if cand * 0.98 <= x["low"] <= cand * 1.03)
            if cnt > bestcnt:
                bestcnt = cnt; best = cand
        if best is None or bestcnt < TOUCH_MIN:
            t += 1; continue
        S = best
        above = sum(1 for x in win if x["high"] >= S * 1.10)
        if above < len(win) * 0.25:   # 必须在支撑上方明显成交过（真横盘，非阴跌）
            t += 1; continue
        # 破位
        break_idx = None
        for i in range(t, min(t + 15, N - 12)):
            if bars[i]["close"] < S * BREAK_PCT:
                break_idx = i; break
        if break_idx is None:
            t += 1; continue
        # 收复（可到末根，以支持「刚收复」的当日信号）
        reclaim_idx = None
        for i in range(break_idx + 1, min(break_idx + RECLAIM_WIN + 1, N)):
            if bars[i]["close"] >= S * RECLAIM_PCT:
                reclaim_idx = i; break
        if reclaim_idx is None:
            t += 1; continue
        sig = reclaim_idx; sig_close = bars[sig]["close"]
        avgvol = sum(x["volume"] for x in win[-20:]) / 20
        def ret(n):
            j = min(sig + n, N - 1)
            return (bars[j]["close"] / sig_close - 1) if j > sig else None
        def peak(n):
            seg = bars[sig + 1:sig + n + 1]
            return (max(x["high"] for x in seg) / sig_close - 1) if seg else None
        def dd(n):
            seg = bars[sig + 1:sig + n + 1]
            return (min(x["low"] for x in seg) / sig_close - 1) if seg else None
        age = (N - 1) - sig   # 距最新交易日多少个交易日
        op = op_plan(bars, sig)   # 操作策略（管三天）
        events.append({
            "support": round(S, 2), "break_day": bars[break_idx]["day"],
            "sig_day": bars[sig]["day"], "sig_close": round(sig_close, 2), "age": age,
            "vol_ratio": round(bars[sig]["volume"] / avgvol, 2) if avgvol else None,
            "r5": ret(5), "r10": ret(10), "r20": ret(20),
            "peak10": peak(10), "peak20": peak(20), "dd20": dd(20),
            "op": op, "hold3_ret": op.get("hold3_ret"),
        })
        t = sig + 25
    return events


def score(e):
    """简易评分：近期 + 触点质量 + 量价，用于排序（越大越优先）。"""
    s = 0
    s += max(0, 6 - e["age"]) * 3                 # 越近期越高
    if e.get("vol_ratio") is not None:
        if e["vol_ratio"] >= 1.2: s += 4
        if e["vol_ratio"] < 1.0:  s += 2          # 缩量收复同样认可（研究结论）
    if e.get("peak20") is not None and e["peak20"] > 0.10: s += 3
    opd = e.get("op") or {}
    if isinstance(opd, dict) and opd.get("buy_vol_ok"):
        s += 3          # 买点缩量承接（量能确认），更优
    return s


# ============================ 操作策略（本模型「管三天」） ============================
# 每次推荐都附此策略；提示日=收复信号日，依据 T+1 形态定买点，买入后第 3 日强制卖出。
OPER_RULE = """**操作策略（本模型「管三天」，每次推荐必附）**
- 提示日 = 收复信号日（重新站回支撑之日）。
- 提示日后第二天（T+1）判断买入与否：
  · 若 **缩量小涨** → 当天可买；
  · 若 **上影线 / 绿阴线**（诱空）→ 等第三天（T+2）**开盘低位**买入。
- **买入后第三天必须卖出**（固定持有 ≤3 日，不恋战）。
- **买点量能须低于提示日**：买点当天成交量须 **小于提示日（收复日）量能**（缩量承接）；若买点放量则量能不符、谨慎或放弃。
- 风控：跌破支撑下 3~5% 止损；到达 +8~12% 或放量滞涨可提前止盈。"""

# T+1 形态识别阈值
UPPER_SHADOW_RATIO = 0.5   # 上影 >= 0.5×实体 视为「带上影」
UPPER_SHADOW_MIN = 0.008   # 上影 >= 0.8% 开盘价 才计入
SMALL_UP_MAX = 0.03         # 小涨：涨幅 <= 3%


def op_plan(bars, sig):
    """依据提示日(sig)后第一根 K 线(T+1)给出买入决策、卖出日与「管三天」收益。

    返回 dict（带 model 无关的操作计划），run_review 会把它随候选一起落盘/推送。
    """
    n = len(bars)
    t1 = sig + 1
    if t1 >= n:
        return {"buy_offset": 1, "buy_day": "提示日+1（第二天）", "trigger": "待 T+1 成型",
                "sell_day": "买入后第 3 日", "t1_shape": "N/A", "vol_ratio_t1": None,
                "vol_ratio_buy": None, "buy_vol_ok": None,
                "hold3_ret": None, "note": "T+1 尚未出现，次日再判。"}
    b1, bs = bars[t1], bars[sig]
    vol_shrink = b1["volume"] < bs["volume"]
    vol_ratio_t1 = round(b1["volume"] / bs["volume"], 2) if bs["volume"] else None
    up = b1["close"] > b1["open"]
    pct = (b1["close"] - b1["open"]) / b1["open"] if b1["open"] else 0
    small_up = up and (0 < pct <= SMALL_UP_MAX)
    body_hi = max(b1["open"], b1["close"])
    upper = b1["high"] - body_hi
    body = abs(b1["close"] - b1["open"])
    has_upper = upper >= UPPER_SHADOW_RATIO * body and upper >= UPPER_SHADOW_MIN * b1["open"]
    is_green = b1["close"] < b1["open"]   # 中国习惯：收<开为绿（阴线）

    if vol_shrink and small_up:
        buy_offset, trigger = 1, "缩量小涨"
    elif has_upper or is_green:
        buy_offset, trigger = 2, "上影线/绿阴线"
    else:
        buy_offset, trigger = 1, "其它（次日观察，满足缩量小涨即买；若收上影/阴线则转第三日低开买）"

    shape = []
    shape.append("缩量" if vol_shrink else "放量")
    if is_green:
        shape.append("绿阴")
    elif small_up:
        shape.append("小涨")
    elif up:
        shape.append("上涨")
    else:
        shape.append("小跌")
    if has_upper:
        shape.append("带上影")
    t1_shape = "、".join(shape)

    # 卖出日 = 买入日 + 3；计算「管三天」持有收益（数据足够时）
    sell_offset = buy_offset + 3
    buy_i = min(sig + buy_offset, n - 1)
    sell_i = min(sig + sell_offset, n - 1)

    # ---- 买点量能校验：买点当天量能须低于提示日（缩量承接）----
    b_buy = bars[buy_i]
    vol_ratio_buy = round(b_buy["volume"] / bs["volume"], 2) if bs["volume"] else None
    buy_vol_ok = (vol_ratio_buy is not None and vol_ratio_buy < 1.0)
    if not buy_vol_ok:
        trigger += "；⚠买点量能≥提示日·量能不符·谨慎/放弃"

    hold3_ret = None
    if sell_i > buy_i:
        entry = bars[buy_i]["open"] if buy_offset == 2 else bars[buy_i]["close"]
        hold3_ret = round(bars[sell_i]["close"] / entry - 1, 4)

    return {
        "buy_offset": buy_offset,
        "buy_day": "提示日+%d（%s）" % (buy_offset, "第二天" if buy_offset == 1 else "第三天开盘"),
        "trigger": trigger,
        "sell_day": "买入后第 3 日（提示日+%d）" % sell_offset,
        "t1_shape": t1_shape,
        "t1_day": b1["day"],
        "vol_ratio_t1": vol_ratio_t1,
        "vol_ratio_buy": vol_ratio_buy,
        "buy_vol_ok": buy_vol_ok,
        "hold3_ret": hold3_ret,
        "note": "T+1(%s): %s；%s → 买点%s(量比%.2f%s)；买入后第 3 日卖出。" % (
            b1["day"], t1_shape, trigger, "提示日+%d" % buy_offset,
            vol_ratio_buy if vol_ratio_buy is not None else 0,
            "·量能不符" if not buy_vol_ok else ""),
    }


# ============================ 推送（PushPlus, 与仓库统一） ============================
def push(title, content):
    token = (os.environ.get("PUSHPLUS_TOKEN") or "").strip()
    if not token:
        print("[推送] 未配置 PUSHPLUS_TOKEN，仅打印")
        print(content)
        return False
    try:
        import requests
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


# ============================ 归档 / 状态回写 ============================
def write_state(cands, today):
    # archive json + csv
    js = os.path.join(ARCHIVE_DIR, "%s.json" % today)
    with open(js, "w", encoding="utf-8") as f:
        json.dump({"date": today, "model": MODEL_NAME, "count": len(cands),
                   "picks": cands}, f, ensure_ascii=False, indent=2)
    cs = os.path.join(ARCHIVE_DIR, "%s.csv" % today)
    with open(cs, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["排名", "代码", "名称", "支撑", "破位日", "收复信号日",
                    "距今交易日", "量比", "评分", "R5%", "R10%", "R20%", "PEAK20%", "DD20%",
                    "操作买点", "触发条件", "卖出日", "买点量比(提示日)", "管三天收益%"])
        for i, e in enumerate(cands, 1):
            op = e.get("op") or {}
            w.writerow([i, e["code"], e.get("name", ""), e["support"], e["break_day"],
                        e["sig_day"], e["age"], e.get("vol_ratio"),
                        score(e),
                        round((e.get("r5") or 0) * 100, 1),
                        round((e.get("r10") or 0) * 100, 1),
                        round((e.get("r20") or 0) * 100, 1),
                        round((e.get("peak20") or 0) * 100, 1),
                        round((e.get("dd20") or 0) * 100, 1),
                        op.get("buy_day", ""), op.get("trigger", ""),
                        op.get("sell_day", ""),
                        ("%.2f" % op["vol_ratio_buy"]) if op.get("vol_ratio_buy") is not None else "",
                        round((e.get("hold3_ret") or 0) * 100, 1) if e.get("hold3_ret") is not None else ""])
    # summary.json（机器可读）
    summary = {
        "date": today, "model": MODEL_NAME, "label": MODEL_LABEL,
        "count": len(cands),
        "top": [{"rank": i, "code": e["code"], "name": e.get("name", ""),
                 "support": e["support"], "break_day": e["break_day"],
                 "sig_day": e["sig_day"], "age": e["age"],
                 "vol_ratio": e.get("vol_ratio"), "score": score(e),
                 "op": e.get("op")}
                for i, e in enumerate(cands[:CONST["TOP_N"]], 1)],
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    # index.jsonl（每日一行）
    with open(os.path.join(STATE_DIR, "index.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"date": today, "model": MODEL_NAME, "count": len(cands)},
                           ensure_ascii=False) + "\n")
    # history.jsonl（供回溯）
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        for e in cands:
            f.write(json.dumps({"date": today, "model": MODEL_NAME,
                                "code": e["code"], "name": e.get("name", ""),
                                "sig_day": e["sig_day"], "sig_close": e.get("close"),
                                "support": e["support"], "score": score(e)},
                               ensure_ascii=False) + "\n")
    log("状态已写盘：%s / %s / %s" % (js, SUMMARY_FILE, HISTORY_FILE))


# ============================ 主流程 ============================
def run_review(ctx, max_age=None, top_n=None):
    from common import common_lib as cm
    max_age = max_age or CONST["MAX_AGE"]
    top_n = top_n or CONST["TOP_N"]
    today = datetime.datetime.now(cm.BJT).strftime("%Y-%m-%d")

    # 非交易日：共同层未产出 / 行情未更新 → 直接退出，不推送
    is_td, qd, _ = cm.check_trading_day()
    if not is_td:
        log("非交易日 / 行情未更新（最新日 %s），跳过。" % qd)
        return []

    universe = ctx.universe()  # 已剔 ST/退市/次新/立信
    if not universe:
        log("样本池为空，疑似数据源异常，终止。")
        return []
    log("样本池 %d 只（已剔 ST/退市/次新/立信）；仅输出近 %d 交易日收复信号" % (len(universe), max_age))

    candidates = []
    t0 = time.time()
    done = 0
    for u in universe:
        code = u["code"]; name = u.get("name", "")
        bars = ctx.kline(code, count=CONST["KLINE_COUNT"])
        if not bars:
            continue
        nb = _norm(bars)
        for e in detect(nb):
            if e["age"] <= max_age:
                e["code"] = code; e["name"] = name
                e["board"] = u.get("board", ""); e["close"] = e.get("sig_close")
                candidates.append(e)
        done += 1
        if done % 500 == 0:
            log("进度 %d/%d 命中 %d %.0fs" % (done, len(universe), len(candidates), time.time() - t0))

    candidates.sort(key=score, reverse=True)
    log("扫描完成 耗时 %.1fs 命中 %d 只" % (time.time() - t0, len(candidates)))

    # 进入推荐台账（按模型名隔离 → 与别的模型「区分」）
    picks = candidates[:top_n]
    rec = [{
        "code": e["code"], "name": e.get("name", ""), "board": e.get("board", ""),
        "close": e.get("close"), "score": score(e),
        "support": e["support"], "break_day": e["break_day"], "sig_day": e["sig_day"],
        "age": e["age"], "vol_ratio": e.get("vol_ratio"),
        "r5": e.get("r5"), "r10": e.get("r10"), "r20": e.get("r20"),
        "op": e.get("op"), "hold3_ret": e.get("hold3_ret"),
        "model": MODEL_NAME,
    } for e in picks]
    n = ctx.record(rec) if rec else 0
    log("ctx.record 落盘 %d 只（common/state/recommendations/%s/）" % (n, MODEL_NAME))

    write_state(candidates, today)
    _push_report(candidates[:top_n], today)
    return candidates


def _push_report(cands, today):
    if not cands:
        push("%s %s（无信号）" % (MODEL_LABEL, today),
             "### %s\n\n今日无「假破位后收复支撑」候选（近 %d 交易日）。" % (MODEL_LABEL, CONST["MAX_AGE"]))
        return
    lines = ["### %s · 每日候选（%s）" % (MODEL_LABEL, today),
             "> 仅列近 **%d** 交易日发生「收复支撑」的候选，共 **%d** 只。博反弹信号，须严格止盈止损，非投资建议。" % (CONST["MAX_AGE"], len(cands)),
             "",
             "| 排名 | 代码 | 名称 | 支撑 | 破位日 | 收复日 | 距今 | 量比 | 评分 | 操作(管三天) |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for i, e in enumerate(cands, 1):
        op = e.get("op") or {}
        vr = op.get("vol_ratio_buy")
        op_txt = "%s｜%s｜买点量比%s" % (
            op.get("buy_day", ""), op.get("trigger", ""),
            ("%.2f" % vr) if vr is not None else "—") if op else ""
        lines.append("| %d | %s | %s | %.2f | %s | %s | %d | %s | %d | %s |" % (
            i, e["code"], e.get("name", ""), e["support"], e["break_day"],
            e["sig_day"], e["age"], e.get("vol_ratio"), score(e), op_txt))
    # 固定操作策略（每次必附）
    lines += ["", OPER_RULE]
    push("%s %s（%d只）" % (MODEL_LABEL, today, len(cands)), "\n".join(lines))


def run_backtest(ctx):
    """次日回溯：对 history.jsonl 中的候选，用共享 K 线计算 T+1/5/10 收益，更新 summary。"""
    log("=== 回溯模式 ===")
    if not os.path.exists(HISTORY_FILE):
        log("无历史候选，跳过。"); return
    rows = [json.loads(l) for l in open(HISTORY_FILE, encoding="utf-8") if l.strip()]
    HOR = (1, 5, 10)
    acc = {h: {"w": 0, "n": 0, "sum": 0.0} for h in HOR}
    for r in rows:
        code = r["code"]
        bars = _norm(ctx.kline(code, count=CONST["KLINE_COUNT"]))
        if not bars:
            continue
        # 找到 sig_day 之后的起点
        idx = next((i for i, b in enumerate(bars) if b["day"] >= r["sig_day"]), None)
        if idx is None:
            continue
        base = bars[idx]["close"]
        for h in HOR:
            j = idx + h
            if j < len(bars):
                ret = bars[j]["close"] / base - 1
                acc[h]["n"] += 1; acc[h]["sum"] += ret
                if ret > 0: acc[h]["w"] += 1
    out = {"updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "model": MODEL_NAME, "samples": acc[HOR[0]]["n"]}
    for h in HOR:
        n = acc[h]["n"]
        out["T+%d" % h] = {"samples": n,
                            "win_rate": round(acc[h]["w"] / n, 3) if n else None,
                            "avg_return": round(acc[h]["sum"] / n, 4) if n else None}

    # 新增：按操作策略「管三天」回溯（提示日 → T+1/T+2 买 → 买入后第 3 日卖）
    rule = {"n": 0, "win": 0, "sum": 0.0, "buy_t1": 0, "buy_t2": 0, "detail": []}
    for r in rows:
        bars = _norm(ctx.kline(r["code"], count=CONST["KLINE_COUNT"]))
        if not bars:
            continue
        idx = next((i for i, b in enumerate(bars) if b["day"] >= r["sig_day"]), None)
        if idx is None:
            continue
        plan = op_plan(bars, idx)
        if not plan:
            continue
        bo = plan["buy_offset"]
        buy_i = min(idx + bo, len(bars) - 1)
        sell_i = min(idx + bo + 3, len(bars) - 1)   # 买入后第 3 日
        if sell_i <= buy_i:
            continue
        entry = bars[buy_i]["open"] if bo == 2 else bars[buy_i]["close"]
        ret = bars[sell_i]["close"] / entry - 1
        rule["n"] += 1; rule["sum"] += ret
        if ret > 0: rule["win"] += 1
        if bo == 1: rule["buy_t1"] += 1
        else: rule["buy_t2"] += 1
    out["rule_3day"] = {
        "samples": rule["n"],
        "buy_on_t1": rule["buy_t1"], "buy_on_t2": rule["buy_t2"],
        "win_rate": round(rule["win"] / rule["n"], 3) if rule["n"] else None,
        "avg_return": round(rule["sum"] / rule["n"], 4) if rule["n"] else None,
    }
    with open(os.path.join(STATE_DIR, "backtest.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    log("回溯完成：%s" % json.dumps(out, ensure_ascii=False))
    return out


# ============================ 自检（离线，新浪数据，不依赖共同层） ============================
def run_selftest():
    import requests
    log("=== 自检验证（已知 3 样本，新浪日线）===")
    named = [("sh688306", "均普智能"), ("sz300528", "幸福蓝海"), ("sz301395", "仁信新材")]
    url = ("http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData?symbol={code}&scale=240&ma=no&datalen=330")
    for code, name in named:
        try:
            r = requests.get(url.format(code=code), timeout=10)
            raw = r.json()
        except Exception as e:
            log(name, "抓取失败:", e); continue
        bars = _norm(raw)
        if not bars:
            log(name, "无数据"); continue
        evs = detect(bars)
        log("%s(%s) 全周期检出事件 %d 个" % (name, code, len(evs)))
        for e in evs[-4:]:
            log("   支撑=%.2f 破位=%s 信号=%s 量比=%s R20=%.1f%% PEAK20=%.1f%% age=%d" % (
                e["support"], e["break_day"], e["sig_day"], e.get("vol_ratio"),
                (e["r20"] or 0) * 100, (e["peak20"] or 0) * 100, e["age"]))


def main():
    ap = argparse.ArgumentParser(description="破位后上涨模型（合并流水线模块）")
    ap.add_argument("--mode", default="review", choices=["review", "backtest", "selftest"])
    ap.add_argument("--max-age", type=int, default=None, help="仅输出近 N 交易日收复的信号")
    ap.add_argument("--top", type=int, default=None, help="进入台账/推送的前 N 只")
    a = ap.parse_args()

    if a.mode == "selftest":
        run_selftest()
        return 0

    from common.kit import ModelContext
    ctx = ModelContext(MODEL_NAME)

    if a.mode == "backtest":
        run_backtest(ctx)
        return 0

    # 默认 / review
    run_review(ctx, max_age=a.max_age, top_n=a.top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
