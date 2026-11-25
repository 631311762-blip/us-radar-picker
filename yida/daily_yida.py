#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
怡达模型 · 每日选股（接入统一前置流水线）
================================================================
源自怡达股份(300721) 2026-08~11 走势提炼，全A股回溯验证。

【版本】v5.4 D2实时预判版（2026-11-25）
  · 一套引擎通吃两种强势形态：平台型（怡达原型）+ 加速型（南极光原型）
  · 黄金样本标定：超声电子/肯特股份/怡达股份（平台型）、南极光（加速型）
  · v5.4 核心修正：取消"4天放量确认"滞后——原v5要D1~D4四根齐才能算CV分流,
    导致D2提示实际要等D4收盘才发(马后炮,D3买点已过)。v5.4改为D2当天就用
    D1+D2两天数据出提示,标签按 D2/D1量比 预判,确保D3可实际买入。
  · 买点：D2盘后出信号 → 次日D3开盘价买入 + T+1(退出从D4起)
    （entry_rule 可切回 d2_close 回退；黄金样本标签预判5/5正确）

【统一骨架】阴跌 + D1/D2连续2天≥2倍放量 + D1前一日≤2.5倍 + 提示日D2
【双标签预判（只用D1/D2两天）】
  · 平台型(platform)：D2/D1量比<1.35 量能平稳
      - 阴跌≥30%，D1涨停豁免或2~7倍，D2严格2~7倍
      - 跟踪5日 + 破位/缩量退出
  · 加速型(accel)：D2/D1量比≥1.35（D2明显放量、递增启动）
      - 阴跌≥20%，两天均≥2倍（不设上限）
      - 跟踪3日固定止盈（无破位退出）
      - 黄金样本：南极光300940 D2/D1量比1.54→预判加速型

模型规则（参数见 config.json，可改）：
  ① 阴跌前置：D1前一日收盘较过去60日最高价跌幅达标，且收盘 < MA60
     （平台型≥30% / 加速型≥20%）
  ② 放量：D1、D2连续2天成交量 ≥ D1前20日均量的 2倍（v5.4预判,不再等D3/D4）
     · 平台型：D1涨停豁免或2~7倍，D2严格2~7倍，且 D2/D1量比<1.35
     · 加速型：两天≥2倍、D2/D1量比≥1.35（预判为递增启动）
  ③ 提示日 = 放量第2天(D2)当天盘后即发（不再等D3/D4确认）
  ④ 买点 = D3开盘价；退出：平台型跟踪5日(破位/缩量/移动止盈)；加速型跟踪3日固定
  ⑤ 命中 = 策略退出收益 ≥ 3%；D3/D4若缩量,历史跟踪按破位/缩量退出兜底

推送（严格两段式，2026-11-24 定版）：
  · 第一段 = 当天提示的票（signal == 最新交易日）
  · 第二段 = 历史提示、未破位、仍在5日观察期内的票
  · ★破位/缩量的票 → 从K线重算破位线判定后直接剔除，完全不推送（无退出告知段）
  · ★观察期满(≥5交易日) → 自然剔除
  · 破位线不依赖台账字段（历史台账常缺 break_line），一律由 signal 前一根D1实体中点重算

数据流（完全遵循仓库共同前置框架）：
  · 样本池：读 common/state/universe.json（已剔除 ST/立信/监管立案/次新）
  · K线：读 common/state/klines 共享缓存（ctx.kline，严格离线）
  · 台账：ctx.record() 落盘，自动进入次日统一回溯（条件④）
  · 推送：复用 common/notify.py（PushPlus，PUSHPLUS_TOKEN）

用法：
  python3 yida/daily_yida.py              # 正式（选股+台账+推送）
  python3 yida/daily_yida.py --no-push    # 本地复跑/回溯不推送
"""
import os
import sys
import json
import argparse
import datetime
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
for _p in (REPO, os.path.join(REPO, "common")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import kit
import notify as common_notify

CFG_PATH = os.path.join(HERE, "config.json")
CFG = json.load(open(CFG_PATH, encoding="utf-8"))


def load_cfg():
    return CFG


# ---------- 形态识别 ----------
def _limit_up_threshold(code, cfg):
    """按板块返回涨停判定阈值（留四舍五入余量）：
       创业板/科创板 20cm / 北交所 30cm / 沪深主板 10cm"""
    s = str(code).zfill(6)
    if s.startswith(("300", "301", "688", "689")):
        return cfg.get("limit_up_growth", 0.195)
    if s.startswith(("8", "4", "92")):
        return cfg.get("limit_up_bj", 0.295)
    return cfg.get("limit_up_main", 0.098)


def _rebound_filter(c, o, i, j, d1_limit_up, cfg):
    """下跌中继反抽过滤（v5.1，仅平台型）：剔除高位诱多尾部风险。
    大亏损10只前5日涨幅全部为正(0.4~10.1%)＝下跌途中反抽放量；黄金样本要么
    前5日为负(真底部)要么>10%(真突破)。命中任一即剔除：
      A) D1涨停豁免 且 D1前5日涨幅>0
      B) D1前5日涨幅 0.5~11% 且 (|D2涨跌幅|≥8% 或 D2收阴)
      C) D1前5日涨幅 2~11% 且 D2温和阳线 0~6%
    2026回测：10/10全拦截大亏损，黄金5/5零误伤。"""
    if not cfg.get("risk_filter", True):
        return False
    if i < 6:
        return False
    pre5 = (c[i - 1] / c[i - 6] - 1) * 100
    d2_chg = (c[j] / c[i] - 1) * 100
    d2_yin = 1 if c[j] < o[j] else 0
    a = bool(d1_limit_up and pre5 > 0)
    b = bool(0.5 <= pre5 <= 11 and (abs(d2_chg) >= 8 or d2_yin))
    cc = bool(2 <= pre5 <= 11 and 0 < d2_chg < 6)
    return a or b or cc


def exit_sim(o, h, c, v, dates, j, buy, pattern, pat_hold, cfg, break_line, shrink_line, n,
             buy_idx=None):
    """v5.3 分类止盈模拟，返回 (ret%, exit_reason, exit_day, held, max_price)。
    v5.3 新增 buy_idx=实际买入日索引（D3）：T+1 当日不可卖，退出判定从 buy_idx+1（D4）起。
      不传 buy_idx 时兼容 v5.2（判定从 j+1 起）。
    平台型：盘中触及 tp_activate(默认+10%)激活移动止盈，此后收盘跌破『持仓期收盘峰值
      ×(1-tp_drawdown)』离场；破位/缩量硬退出优先；pat_hold 日到期按收盘平仓。
    加速型：死拿 pat_hold 日收盘，不设任何中途退出。
    ret 为策略实际退出收益（不再是关注期最高价），hit 据此判定。"""
    max_price = float(h[buy_idx] if buy_idx is not None else j)
    # 判定起点：v5.3 T+1 = 买入次日；v5.2 = D2 次日
    check_from = (buy_idx + 1) if buy_idx is not None else (j + 1)
    if pattern == "accel":
        k = min(j + pat_hold, n - 1)
        if k > j:
            max_price = max(max_price, float(np.max(h[check_from:k + 1])) if k >= check_from else max_price)
        ret = (float(c[k]) / buy - 1) * 100
        return ret, None, None, k - j, max_price

    # ---- 平台型：硬退出 + 宽松移动止盈 + 到期 ----
    activate = cfg.get("tp_activate", 0.10)
    drawdown = cfg.get("tp_drawdown", 0.03)
    peak_close = buy
    activated = False
    # T+1：买当天(check_from 之前)不判定退出，但峰值照常累积
    for k in range(check_from, min(j + pat_hold, n)):
        hi, cl = float(h[k]), float(c[k])
        max_price = max(max_price, hi)
        peak_close = max(peak_close, cl)
        if not activated and hi / buy - 1 >= activate:
            activated = True
        if cl < break_line:
            return (cl / buy - 1) * 100, "破位", dates[k], k - j, max_price
        if v[k] < shrink_line:
            return (cl / buy - 1) * 100, "缩量", dates[k], k - j, max_price
        if activated and cl <= peak_close * (1 - drawdown):
            return (cl / buy - 1) * 100, "移动止盈", dates[k], k - j, max_price
    k = min(j + pat_hold, n - 1)
    ret = (float(c[k]) / buy - 1) * 100
    return ret, None, None, k - j, max_price


def detect(bars, cfg, code=""):
    """bars: 共享K线列表 [{date,open,high,low,close,volume}, ...] 升序
    code: 6位代码，用于判断D1是否涨停（涨停启动日豁免规则）
    返回最新一个有效信号 dict 或 None"""
    if not bars or len(bars) < cfg["lookback"] + cfg["base_win"] + cfg["hold_days"] + 5:
        return None
    dates = [str(b["date"]) for b in bars]
    h = np.array([float(b["high"]) for b in bars])
    c = np.array([float(b["close"]) for b in bars])
    v = np.array([float(b["volume"]) for b in bars])
    o = np.array([float(b["open"]) for b in bars])
    n = len(c)
    lb, bw = cfg["lookback"], cfg["base_win"]
    hold, sig_day = cfg["hold_days"], cfg["signal_day"]

    # 只用最近 window 天找信号，避免每天重复推同一形态
    start = max(lb + bw, n - cfg.get("recent_window", 15))
    # 滚动基准
    base = np.array([np.mean(v[i - bw:i]) if i >= bw else np.nan for i in range(n)])
    ma60 = np.array([np.mean(c[i - lb:i]) if i >= lb else np.nan for i in range(n)])
    peak = np.array([np.max(h[i - lb:i]) if i >= lb else np.nan for i in range(n)])

    # v5.4：从后往前扫,优先返回窗口内【最新】信号(避免旧信号吃掉当天新提示)
    for i in range(n - 2, start - 1, -1):
        b = base[i]
        if not np.isfinite(b) or b <= 0 or not np.isfinite(peak[i]):
            continue
        prev = c[i - 1]
        decline = prev / peak[i] - 1
        if prev >= ma60[i]:          # 统一骨架：必须在 MA60 之下（空头排列）
            continue
        # v5.4 D2实时预判：只用 D1、D2 两根放量K线（不再等D3/D4）
        #   原v5需四根齐才能算CV分流,导致D2提示滞后到D4收盘(马后炮)。
        #   v5.4只要D1+D2连续2天放量就当天出提示,标签按 D2/D1 量比预判。
        j = i + sig_day - 1          # 提示日 D2 = i+1
        if j >= n:
            continue
        vols = v[i:j + 1]            # D1、D2 两根
        if len(vols) < 2:
            continue
        ratios = vols / b
        d1_limit_up = c[i] / c[i - 1] - 1 >= _limit_up_threshold(code, cfg)
        # D1前一日不得已放量（确保D1是启动首日）
        if v[i - 1] / b > 2.5:
            continue
        r21 = float(v[j] / v[i])     # D2/D1 量比 = 标签预判分水岭

        # ===== 双标签预判（互斥，只用2天信息）=====
        pattern, pat_hold = None, hold
        if r21 >= cfg.get("vol_ratio_accel", 1.35):
            # 加速型预判：D2量≥1.35×D1(递增启动) + 阴跌≥20% + 两天都≥2倍(不设上限)
            #   加速型不享受涨停豁免（递增形态本身要求逐日放量）
            if (decline <= -cfg.get("accel_decline_min", 0.20)
                    and np.all(ratios >= cfg["vol_lo"])):
                pattern, pat_hold = "accel", cfg.get("accel_hold_days", 3)
        else:
            # 平台型预判：量能平稳(量比<1.35) + 阴跌≥30% + D1涨停豁免/2~7倍 + D2严格2~7倍
            if decline <= -cfg["decline_min"] and (
                d1_limit_up or (cfg["vol_lo"] <= ratios[0] <= cfg["vol_hi"])
            ) and (cfg["vol_lo"] <= ratios[1] <= cfg["vol_hi"]):
                pattern, pat_hold = "platform", cfg["hold_days"]
        if pattern is None:
            continue
        # v5.1 风控：平台型若命中「下跌中继反抽」特征则剔除（高位诱多尾部风险）
        if pattern == "platform" and _rebound_filter(c, o, i, j, d1_limit_up, cfg):
            continue
        d1_chg = c[i] / prev - 1
        # v5.4：破位线 = D1实体中点；缩量线 = 两天放量均量×ratio（预判只用D1/D2）
        break_line = (float(bars[i]["open"]) + float(bars[i]["close"])) / 2
        shrink_line = np.mean(vols) * cfg.get("shrink_ratio", 0.5)
        cv = float(np.std(vols) / np.mean(vols))  # 两天量波动率(仅作展示参考,不再用于分流)

        # v5.3 实盘买点：D2盘后出信号买不到 → 次日D3开盘价买入，T+1从D4起判定退出
        entry_rule = cfg.get("entry_rule", "d3_open")
        buy_idx = j + 1  # D3 买入日
        pending = False  # 当天最新提示：D3尚未到来，无法知道开盘价
        if entry_rule == "d3_open" and buy_idx < n:
            buy = float(o[buy_idx])       # D3 开盘价（真实可成交）
        elif entry_rule == "d3_open":
            buy = float(c[j])             # D3未到，占位（主程序标注待开盘）
            pending = True
        else:
            buy = float(c[j])             # 回退 v5.2：D2 收盘买
            buy_idx = None

        # v5.3 分类止盈结算（T+1：买当天不判定退出）
        if pending:
            # D3 未到，尚无买入与持有，收益不可结算
            ret, exit_reason, exit_day, checked, max_price = None, None, None, 0, float(c[j])
        else:
            ret, exit_reason, exit_day, checked, max_price = exit_sim(
                o, h, c, v, dates, j, buy, pattern, pat_hold, cfg, break_line, shrink_line, n,
                buy_idx=buy_idx)
        return dict(
            code="",  # 占位，主程序填 code
            pattern=pattern,
            d1=dates[i], signal=dates[j],
            buy=round(float(buy), 2),
            entry_day=dates[buy_idx] if (buy_idx is not None and buy_idx < n) else "",
            pending=pending,  # True=当天提示,D3未开盘,买入价占位
            d1_chg=round(float(d1_chg) * 100, 1),
            d2_vol_ratio=round(float(v[j] / v[i]), 2),
            decline=round(float(decline) * 100, 1),
            vol_mult=round(float(np.mean(ratios)), 2),
            cv=round(float(cv) * 100, 1),
            break_line=round(float(break_line), 2),
            shrink_line=round(float(shrink_line)),
            hold_days=pat_hold,
            exit_reason=exit_reason,
            exit_day=exit_day,
            held_days=checked,
            ret5=round(ret, 1) if ret is not None else None,  # exit_sim已返回百分数
            hmax=round(float(max_price / buy - 1) * 100, 1),  # 关注期最高价收益(参考)
            hit=bool(ret is not None and ret >= cfg["target"] * 100),  # target(0.03)转百分数
        )
    return None


def _has_history(ctx):
    """台账里是否已有历史推荐批次"""
    try:
        import glob, os
        return len(glob.glob(os.path.join(ctx.reco_dir, "*.json"))) > 0
    except Exception:
        return False


def _load_reco_signals(ctx):
    """从台账加载历史入选信号，按 (code, 提示日signal) 去重，保留最早一次记录。
    返回 {(code,signal): sig_dict}。raw 里保存了 detect 的全部字段。"""
    import glob, os
    sigs = {}
    for f in sorted(glob.glob(os.path.join(ctx.reco_dir, "*.json"))):
        d = kit._read_json(f)
        if not d:
            continue
        rec_date = d.get("date", "")
        for p in d.get("picks", []):
            code = str(p.get("code", "")).zfill(6)
            raw = p.get("raw", {}) or {}
            signal = raw.get("signal", "")
            if not code or not signal:
                continue
            key = (code, signal)
            if key in sigs:
                continue  # 同一轮形态只保留最早记录
            sigs[key] = dict(
                code=code, name=p.get("name", ""), signal=signal,
                buy=raw.get("buy") or p.get("close"),
                pattern=raw.get("pattern", "platform"),   # 历史信号默认平台型
                hold_days=raw.get("hold_days", CFG.get("hold_days", 5)),
                break_line=raw.get("break_line"),
                shrink_line=raw.get("shrink_line"),
                vol_mult=raw.get("vol_mult"), decline=raw.get("decline"),
                cv=raw.get("cv"), d1_limit_up=raw.get("d1_limit_up"),
            )
    return sigs


def _track_signal(sig, bars, cfg, latest_date):
    """把单条信号跟踪到最新交易日。破位线/缩量线一律从K线重算，不依赖台账字段
    （历史台账常缺 break_line，导致破位判断失效、破位票被误推）。
    D2=signal，D1=signal前一根；破位线=D1实体中点，缩量线=D1+D2两天均量×shrink_ratio(v5.4与detect一致)。"""
    dates = [str(b["date"]) for b in bars]
    o = [float(b["open"]) for b in bars]
    h = [float(b["high"]) for b in bars]
    c = [float(b["close"]) for b in bars]
    v = [float(b["volume"]) for b in bars]
    sig_date = sig.get("signal", "")
    if sig_date not in dates:
        return None
    j = dates.index(sig_date)
    if j < 1:
        return None
    pattern = sig.get("pattern", "platform")
    pat_hold = int(sig.get("hold_days") or (
        cfg.get("accel_hold_days", 3) if pattern == "accel" else cfg["hold_days"]))
    # 从K线重算破位线/缩量线（D1=D2前一根，绝不依赖台账可能缺失的字段）
    d1 = j - 1
    bl = (o[d1] + c[d1]) / 2
    sl = float(np.mean(v[max(0, d1):j + 1])) * cfg.get("shrink_ratio", 0.5)  # v5.4: D1+D2两天均量
    # v5.3 实盘买点：历史票用 D3 开盘价重算买入（不信台账旧 buy）
    entry_rule = cfg.get("entry_rule", "d3_open")
    buy_idx = j + 1
    if entry_rule == "d3_open" and buy_idx < len(c):
        buy = float(o[buy_idx])   # D3 开盘价（真实可成交）
    else:
        buy = float(sig["buy"]) if sig.get("buy") else c[j]  # 回退 v5.2 / D3未到
        buy_idx = None
    # 分类止盈：只跟踪到 latest_date，未走完持有期的按当前最新价临时结算
    avail = min(j + pat_hold, len(c) - 1)                  # 持有期上限
    avail = min(avail, max(j, dates.index(latest_date) if latest_date in dates else len(c) - 1))
    ret_pct, exit_reason, exit_day, held, maxp = exit_sim(
        np.array(o), np.array(h), np.array(c), np.array(v), dates,
        j, buy, pattern, pat_hold, cfg, bl, sl, avail + 1, buy_idx=buy_idx)
    # 尚未触发退出且未走完 → 标记跟踪中，按最新收盘临时结算
    if exit_reason is None and avail >= j:
        ret_pct = (c[avail] / buy - 1) * 100 if buy > 0 else 0.0
    held = sum(1 for d in dates if sig_date < d <= latest_date)
    return dict(exit_day=exit_day, exit_reason=exit_reason, pattern=pattern,
                pat_hold=pat_hold, ret=round(ret_pct, 1), held=held,
                buy=round(buy, 2), break_line=round(float(bl), 2))


def _push_two_section(ctx, picks, universe, cfg):
    """严格两段式推送（用户定版 2026-11-24）：
    第一段 = 当天提示的票（signal == 最新交易日/最新提示日）
    第二段 = 历史提示、仍在5日观察期、且未破位的票
    ★ 破位/缩量的票 → 直接剔除，完全不推送（无退出告知段）
    ★ 观察期满(≥5交易日) → 自然剔除
    """
    import datetime
    today = datetime.datetime.now(kit.cm.BJT).strftime("%m-%d")

    # 1) 汇总所有信号：台账历史 + 本次 picks（picks 参数最全，覆盖同 key）
    allsig = _load_reco_signals(ctx)
    for p in picks:
        code = str(p.get("code", "")).zfill(6)
        signal = p.get("signal", "")
        if not code or not signal:
            continue
        allsig[(code, signal)] = dict(
            code=code, name=p.get("name", ""), signal=signal,
            buy=p.get("buy"), vol_mult=p.get("vol_mult"),
            decline=p.get("decline"), cv=p.get("cv"),
            pattern=p.get("pattern", "platform"),
            hold_days=p.get("hold_days"),
        )

    # 2) 最新提示日 与 最新交易日（以某只票的K线末日为准）
    latest_signal = max(v["signal"] for v in allsig.values()) if allsig else ""
    latest_date = latest_signal
    for (code, _), sig in allsig.items():
        b = kit.cm.load_kline(code, offline=True)
        if b:
            latest_date = str(b[-1]["date"])
            break

    # 3) 逐信号跟踪并分流：破位/缩量一律剔除，不进任何段落
    seg1, seg2 = [], []
    dropped = []
    for (code, sig_date), sig in allsig.items():
        if sig.get("buy") is None:
            continue
        bars = kit.cm.load_kline(code, offline=True)
        if not bars:
            continue
        st = _track_signal(sig, bars, cfg, latest_date)
        if not st:
            continue
        item = dict(sig)
        item.update(st)
        # ★破位/缩量 → 直接剔除，不推送
        if st["exit_day"]:
            dropped.append(f"{code} {sig.get('name','')} {st['exit_reason']}({st['exit_day']})")
            continue
        # 观察期满 → 剔除（按各自形态的跟踪周期）
        if st["held"] >= st["pat_hold"]:
            dropped.append(f"{code} {sig.get('name','')} 观察期满{st['held']}日")
            continue
        # 最新提示日那批 → 第一段；更早的历史提示仍在观察 → 第二段
        (seg1 if sig_date == latest_signal else seg2).append(item)

    # 4) 组装推送（只有两段，票名带形态标签）
    def tag(x):
        return "⚡加速" if x.get("pattern") == "accel" else "🔵平台"
    sig_mmdd = latest_signal[5:] if len(latest_signal) >= 10 else today
    n_accel = sum(1 for x in seg1 + seg2 if x.get("pattern") == "accel")
    lines = [f"**怡达·双模型 {today}** ｜ 样本{len(universe)} ｜ 已剔除破位/期满{len(dropped)}只", ""]

    entry_open = cfg.get("entry_rule", "d3_open") == "d3_open"
    lines.append(f"【一、当天提示 {sig_mmdd}·{len(seg1)}只】")
    if not seg1:
        lines.append("今日无新提示")
    for x in sorted(seg1, key=lambda z: -float(z.get("vol_mult") or 0)):
        if entry_open and x.get("pending"):
            # 当天提示：D3尚未开盘，买入价未知，明确操作指令
            lines.append(f"`{x['code']}` {x['name']} {tag(x)} **次日开盘介入** · 勿追尾盘")
        else:
            ret = f"{x['ret']:+.1f}%" if x.get("ret") is not None else "—"
            lines.append(f"`{x['code']}` {x['name']} {tag(x)} 买{x['buy']} 观察{x['held']}日 {ret}")

    lines += ["", f"【二、历史提示·观察中 {len(seg2)}只（未破位）】"]
    if not seg2:
        lines.append("无")
    for x in sorted(seg2, key=lambda z: z["signal"], reverse=True):
        flag = "✓" if x["ret"] >= cfg["target"] * 100 else ""
        lines.append(f"`{x['code']}` {x['name']} {tag(x)} {x['signal'][5:]}提示 买{x['buy']} "
                     f"观察{x['held']}/{x['pat_hold']}日 {x['ret']:+.1f}%{flag}")

    foot = ("🔵平台:阴跌≥30%平稳放量跟5日 ｜ ⚡加速:阴跌≥20%递增放量跟3日" if not entry_open
            else "📌买点:提示次日开盘价介入(T+1次日可卖,勿等尾盘) ｜ 🔵平台跟5日 ⚡加速跟3日")
    lines += ["", foot, "⚠️非投资建议"]
    title = f"怡达 {today} · 提示{len(seg1)} 观察中{len(seg2)}(加速{n_accel})"
    body = "\n".join(lines)
    print(f"[yida] ===PUSH_BODY_START===\n{title}\n{body}\n===PUSH_BODY_END===", flush=True)
    return common_notify.send_alert(title, body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-push", action="store_true", help="只选股落台账，不推送")
    args = ap.parse_args()

    cfg = load_cfg()
    ctx = kit.ModelContext("yida")
    print("[yida] 读取前置流水线样本池（common/state/universe.json）...", flush=True)
    universe = ctx.universe()
    print(f"[yida] 样本池 {len(universe)} 只", flush=True)

    picks = []
    for x in universe:
        code = str(x.get("code", "")).zfill(6)
        bars = ctx.kline(code, count=cfg["lookback"] + cfg["base_win"] + cfg["hold_days"] + 30)
        if not bars:
            continue
        sig = detect(bars, cfg, code=code)
        if not sig:
            continue
        sig["code"] = code
        sig["name"] = x.get("name", "")
        sig["score"] = sig["vol_mult"]
        sig["close"] = sig["buy"]
        sig["board"] = x.get("board", "")
        picks.append(sig)

    print(f"[yida] 识别信号 {len(picks)} 个（平台{sum(1 for p in picks if p.get('pattern')=='platform')} "
          f"/ 加速{sum(1 for p in picks if p.get('pattern')=='accel')}）", flush=True)
    for p in picks:
        tag = "⚡加速" if p.get("pattern") == "accel" else "🔵平台"
        print(f"  {p['code']} {p['name']} {tag} 提示日{p['signal']} "
              f"阴跌{p['decline']}% 放量{p['vol_mult']}x CV{p['cv']}%", flush=True)

    # 台账（自动进入统一回溯）
    ctx.record(picks)
    print("[yida] 台账已写入", flush=True)

    # 回溯结算
    try:
        ctx.review(save=True)
    except Exception as e:
        print(f"[yida] 回溯结算跳过：{e}", flush=True)

    # 推送（两段式：第一段=本次提示日新票；第二段=历史入选跟踪到今日）
    if not args.no_push and (picks or _has_history(ctx)):
        ok, msg = _push_two_section(ctx, picks, universe, cfg)
        print(f"[yida] 推送：{'成功' if ok else '失败'} {msg}", flush=True)


if __name__ == "__main__":
    main()
