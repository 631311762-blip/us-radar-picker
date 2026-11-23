#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
怡达模型 · 每日选股（接入统一前置流水线）
================================================================
源自怡达股份(300721) 2026-08~11 走势提炼，全A股回溯验证：
  放量4倍版 2026 命中率 90.9%；放量2倍版 88.1%。

【版本】v3 涨停豁免版（2026-11-24 回退）
  · 已移除 v4 的「D1放量长阳>5%」「D2量价约束」两条形态过滤
  · 三只黄金样本（超声电子/肯特股份/怡达股份）为此版标定样本
  · 保留：阴跌≥30% / 平台放量2~7倍 / D1涨停豁免 / CV<20% / D2提示 / 5日退出

模型规则（参数见 config.json，可改）：
  ① 阴跌前置：D1前一日收盘较过去60日最高价跌幅 ≥ 30%，且收盘 < MA60
  ② 平台放量：连续4天(D1~D4)成交量均为 D1前20日均量的 2~7 倍
     ★涨停豁免：D1若为涨停板(主板10cm/双创20cm/北交30cm)，封板导致量能不足2倍属正常，
       豁免D1倍数，其余D2~D4仍严格2~7倍（2026回测新增17个信号，命中率100%）
  ③ 量能平稳：四天量能变异系数 CV < 20%（排除逐日爆量）
  ④ 提示日 = 放量第2天(D2)，以 D2 收盘价买入
  ⑤ 退出机制：提示日后关注5天，任一触发即停止关注：
     - 破位：收盘价 < D1实体中点(开+收)/2
     - 缩量：成交量 < 放量四天均量 × 0.5
     触发当日按最高价结算（保留当日浮盈）
  ⑥ 命中 = 关注期内最高收益 ≥ 3%

推送（三段式，2026-11-23 定版）：
  · 第一段 = 最新提示日的票（当天新 D2 信号）
  · 第二段 = 历史入选、未破位、仍在5日观察期内的票（跟踪表现）
  · 第三段 = 退出告知：5日内触发破位/缩量、且退出日恰为当日 → 推一条告知后永久拉黑；
    退出日早于当日的票视为已告知过，静默不再推（无需额外状态文件，破位日是K线客观事实）

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
    n = len(c)
    lb, bw = cfg["lookback"], cfg["base_win"]
    hold, sig_day = cfg["hold_days"], cfg["signal_day"]

    # 只用最近 window 天找信号，避免每天重复推同一形态
    start = max(lb + bw, n - cfg.get("recent_window", 15))
    # 滚动基准
    base = np.array([np.mean(v[i - bw:i]) if i >= bw else np.nan for i in range(n)])
    ma60 = np.array([np.mean(c[i - lb:i]) if i >= lb else np.nan for i in range(n)])
    peak = np.array([np.max(h[i - lb:i]) if i >= lb else np.nan for i in range(n)])

    for i in range(start, n - 1):
        b = base[i]
        if not np.isfinite(b) or b <= 0 or not np.isfinite(peak[i]):
            continue
        prev = c[i - 1]
        # ① 阴跌 + 空头排列
        if not (prev / peak[i] - 1 <= -cfg["decline_min"] and prev < ma60[i]):
            continue
        # ② 平台放量 D1~D4
        vols = v[i:i + 4]
        if len(vols) < 4:
            continue
        ratios = vols / b
        # —— 涨停启动日豁免 ——
        # D1涨停(主板10cm/双创20cm/北交30cm)时，封板导致量能不足2倍属正常，
        # 豁免D1放量倍数；其余D2~D4仍严格2~7倍。
        d1_limit_up = c[i] / c[i - 1] - 1 >= _limit_up_threshold(code, cfg)
        if not d1_limit_up and not (cfg["vol_lo"] <= ratios[0] <= cfg["vol_hi"]):
            continue
        if not np.all((ratios[1:] >= cfg["vol_lo"]) & (ratios[1:] <= cfg["vol_hi"])):
            continue
        # ③ 量能平稳（豁免日仍用四天原始量算CV，保证平台整体平稳）
        cv = np.std(vols) / np.mean(vols)
        if cv > cfg["cv_max"]:
            continue
        # D1前一日不得已放量（确保D1是启动首日）
        if v[i - 1] / b > 2.5:
            continue
        # D1 涨幅（仅记录，不做形态过滤——v3 已回退 D1长阳/D2量价约束）
        d1_chg = c[i] / prev - 1
        # ④ 提示日 D2（第2根放量日）
        j = i + sig_day - 1
        if j >= n:
            continue
        buy = c[j]
        # ⑤ 退出机制：第一根放量K线实体中点（破位线）
        d1_open = float(bars[i]["open"])
        d1_close = float(bars[i]["close"])
        break_line = (d1_open + d1_close) / 2
        # 缩量线：放量四天均量的一半
        shrink_line = np.mean(vols) * cfg.get("shrink_ratio", 0.5)

        # 从提示日次日起，逐日检查退出条件（最多关注 hold 天）
        exit_reason = None
        exit_day = None
        max_price = h[j]                       # 提示日当日最高也计入
        last_close = c[j]
        checked = 0
        for k in range(j + 1, min(j + 1 + hold, n)):
            checked += 1
            max_price = max(max_price, h[k])   # 触发前(含当日)最高价
            last_close = c[k]
            # 破位：收盘价跌破实体中点
            if c[k] < break_line:
                exit_reason = "破位"
                exit_day = dates[k]
                break
            # 缩量：成交量低于放量四天均量的一半
            if v[k] < shrink_line:
                exit_reason = "缩量"
                exit_day = dates[k]
                break
        # 结算：按触发当日最高价（保留当日浮盈）；未触发则按关注期内最高价
        ret5 = float(max_price / buy - 1) if checked > 0 else None
        return dict(
            code=str(bars[j]).zfill(6) if False else "",  # 占位，主程序填 code
            d1=dates[i], signal=dates[j],
            buy=round(float(buy), 2),
            d1_chg=round(float(d1_chg) * 100, 1),
            d2_vol_ratio=round(float(v[j] / v[i]), 2),
            decline=round(float(prev / peak[i] - 1) * 100, 1),
            vol_mult=round(float(np.mean(ratios)), 2),
            cv=round(float(cv) * 100, 1),
            break_line=round(float(break_line), 2),
            shrink_line=round(float(shrink_line)),
            exit_reason=exit_reason,
            exit_day=exit_day,
            held_days=checked,
            ret5=round(ret5 * 100, 1) if ret5 is not None else None,
            hit=bool(ret5 is not None and ret5 >= cfg["target"]),
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
                break_line=raw.get("break_line"),
                shrink_line=raw.get("shrink_line"),
                vol_mult=raw.get("vol_mult"), decline=raw.get("decline"),
                cv=raw.get("cv"), d1_limit_up=raw.get("d1_limit_up"),
            )
    return sigs


def _track_signal(sig, bars, cfg, latest_date):
    """把单条信号跟踪到最新交易日：算退出日/退出原因/关注期最高收益/已观察交易日数。
    退出日 = 提示日(D2)次日起，5个交易日内首个触发破位或缩量的日期。"""
    dates = [str(b["date"]) for b in bars]
    h = [float(b["high"]) for b in bars]
    c = [float(b["close"]) for b in bars]
    v = [float(b["volume"]) for b in bars]
    sig_date = sig.get("signal", "")
    if sig_date not in dates:
        return None
    j = dates.index(sig_date)
    buy = float(sig["buy"])
    bl = sig.get("break_line")
    sl = sig.get("shrink_line")
    exit_day, exit_reason = None, None
    maxp = h[j]
    # 提示日次日起，最多跟踪 hold 个交易日
    for k in range(j + 1, min(j + 1 + cfg["hold_days"], len(c))):
        maxp = max(maxp, h[k])
        if bl is not None and c[k] < float(bl):
            exit_day, exit_reason = dates[k], "破位"
            break
        if sl is not None and v[k] < float(sl):
            exit_day, exit_reason = dates[k], "缩量"
            break
    ret = (maxp / buy - 1) * 100 if buy > 0 else 0.0
    held = sum(1 for d in dates if sig_date < d <= latest_date)
    return dict(exit_day=exit_day, exit_reason=exit_reason,
                ret=round(ret, 1), held=held)


def _push_two_section(ctx, picks, universe, cfg):
    """两段式推送（带退出告知+永久拉黑）：
    第一段 = 最新提示日的票（picks 中 signal 取全局最新）
    第二段 = 历史入选、未破位、仍在5日观察期内的票（跟踪表现）
    退出告知 = 5日内触发破位/缩量、且退出日恰为最新交易日的票 → 推一条告知
              退出日早于最新交易日的票视为已告知过 → 永久拉黑，不再推送
    观察期满(≥5交易日)未破位的票 → 观察结束，不再推送
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
            buy=p.get("buy"), break_line=p.get("break_line"),
            shrink_line=p.get("shrink_line"), vol_mult=p.get("vol_mult"),
            decline=p.get("decline"), cv=p.get("cv"),
            d1_limit_up=p.get("d1_limit_up"),
        )
    if not allsig:
        return common_notify.send_alert(f"怡达 {today} · 无信号", "今日无信号，观察中。")

    # 2) 最新提示日 L 与最新交易日
    latest_signal = max(v["signal"] for v in allsig.values())
    latest_date = latest_signal
    for (code, _), sig in allsig.items():
        b = kit.cm.load_kline(code, offline=True)
        if b:
            latest_date = str(b[-1]["date"])
            break

    # 3) 逐信号跟踪并分流
    seg1, seg2, exits = [], [], []
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
        # 触发退出
        if st["exit_day"]:
            if st["exit_day"] == latest_date:
                exits.append(item)      # 今日刚退出 → 告知一次
            # 退出日早于今日 → 已告知过，永久拉黑（静默跳过）
            continue
        # 未退出：观察期满则结束
        if st["held"] >= cfg["hold_days"]:
            continue
        # 仍在观察期：最新提示日 → 第一段；否则 → 第二段
        (seg1 if sig_date == latest_signal else seg2).append(item)

    # 4) 组装推送
    sig_mmdd = latest_signal[5:] if len(latest_signal) >= 10 else today
    lines = [f"**怡达 · {today}** ｜ 样本{len(universe)}", ""]

    lines.append(f"【一、提示日的票 {sig_mmdd}·{len(seg1)}只】")
    if not seg1:
        lines.append("今日无新提示")
    for x in sorted(seg1, key=lambda z: -float(z.get("vol_mult") or 0)):
        ret = f"{x['ret']:+.1f}%" if x.get("ret") is not None else "—"
        lines.append(f"`{x['code']}` {x['name']} 买{x['buy']} 观察{x['held']}日 {ret}")

    lines += ["", f"【二、观察中·未破位 {len(seg2)}只（5日内）】"]
    if not seg2:
        lines.append("无")
    for x in sorted(seg2, key=lambda z: z["signal"], reverse=True):
        flag = "✓" if x["ret"] >= cfg["target"] * 100 else ""
        lines.append(f"`{x['code']}` {x['name']} 买{x['buy']} 观察{x['held']}日 {x['ret']:+.1f}%{flag}")

    if exits:
        lines += ["", f"【三、⚠️ 退出告知 {len(exits)}只（停止关注）】"]
        for x in exits:
            lines.append(f"`{x['code']}` {x['name']} {x['exit_reason']}退出 "
                         f"买{x['buy']} 结算{x['ret']:+.1f}%")

    lines += ["", "阴跌≥30% 平台放量2~7倍 D2提示 观察5天", "⚠️非投资建议"]
    title = f"怡达 {today} · 提示{len(seg1)} 退出{len(exits)}"
    return common_notify.send_alert(title, "\n".join(lines))


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

    print(f"[yida] 识别信号 {len(picks)} 个", flush=True)
    for p in picks:
        print(f"  {p['code']} {p['name']} 提示日{p['signal']} "
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
