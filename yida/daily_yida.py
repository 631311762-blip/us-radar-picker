#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
怡达模型 · 每日选股（接入统一前置流水线）
================================================================
源自怡达股份(300721) 2026-08~11 走势提炼，全A股回溯验证：
  放量4倍版 2026 命中率 90.9%；放量2倍版 88.1%。

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


def _push_two_section(ctx, picks, universe, cfg):
    """两段式推送：
    第一段 = 本次提示日新票（picks）
    第二段 = 台账中历史入选票，用共享K线跟踪到今日（收益/退出状态）
    """
    import glob, os
    today = datetime.datetime.now(kit.cm.BJT).strftime("%m-%d")
    sig_date = picks[0].get("signal", today) if picks else today
    sig_mmdd = sig_date[5:] if len(sig_date) >= 10 else today

    lines = [f"**怡达 {sig_mmdd}** ｜ 样本{len(universe)} 新提示{len(picks)}只", ""]

    # ---- 第一段：本次提示日新票 ----
    lines.append(f"【今日提示 {sig_mmdd}·{len(picks)}只】")
    if not picks:
        lines.append("今日无新信号")
    for p in sorted(picks, key=lambda x: -float(x.get("vol_mult") or 0)):
        ret = f"{p['ret5']:+.1f}%" if p.get("ret5") is not None else "待验证"
        hit = "✓" if p.get("hit") else ""
        lu = "涨停" if p.get("d1_limit_up") else ""
        lines.append(f"`{p['code']}` {p['name']} 买{p['buy']} {ret}{hit} {lu}".rstrip())

    # ---- 第二段：历史入选跟踪到今日 ----
    lines.append("")
    lines.append("【历史入选·跟踪到今日】")
    hist = []
    try:
        files = sorted(glob.glob(os.path.join(ctx.reco_dir, "*.json")))
        # 排除本日，取最近8个批次
        files = [f for f in files if os.path.basename(f)[:10] < ctx.today][-8:]
        for f in files:
            d = kit._read_json(f)
            if not d:
                continue
            rec_date = d.get("date", "")
            for p in d.get("picks", []):
                code = str(p.get("code", "")).zfill(6)
                base = p.get("close")
                name = p.get("name", "")
                raw = p.get("raw", {})
                if not code or base is None:
                    continue
                bars = kit.cm.load_kline(code, offline=True)
                if not bars:
                    continue
                dates = [str(b["date"]) for b in bars]
                # 推荐日之后的K线
                fwd_idx = [k for k, dt in enumerate(dates) if dt > rec_date]
                if not fwd_idx:
                    continue
                k0 = fwd_idx[0]
                # 跟踪最多5日：最高价收益 + 退出状态
                hold = min(cfg["hold_days"], len(bars) - k0)
                seg = bars[k0:k0 + hold]
                maxp = max(float(b["high"]) for b in seg)
                ret = (maxp / float(base) - 1) * 100
                # 退出判定：用 raw 里的 break_line/shrink_line
                exit_tag = "持有"
                bl = raw.get("break_line")
                sl = raw.get("shrink_line")
                if bl is not None and any(float(b["close"]) < float(bl) for b in seg):
                    exit_tag = "破位"
                elif sl is not None and any(float(b["volume"]) < float(sl) for b in seg):
                    exit_tag = "缩量"
                hist.append((rec_date, code, name, ret, exit_tag))
    except Exception as e:
        print(f"[yida] 历史跟踪读取跳过：{e}", flush=True)

    if not hist:
        lines.append("暂无历史台账（投产后累积）")
    else:
        # 按日期倒序，每个批次最多列3只，控制篇幅
        hist.sort(key=lambda x: x[0], reverse=True)
        last_date, per_date = None, 0
        for rec_date, code, name, ret, exit_tag in hist:
            if rec_date != last_date:
                last_date, per_date = rec_date, 0
            if per_date >= 3:
                continue
            per_date += 1
            flag = "✓" if ret >= cfg["target"] * 100 else ""
            lines.append(f"{rec_date[5:]} `{code}` {name} {ret:+.1f}%{flag} {exit_tag}")

    lines += ["", "阴跌≥30% 平台放量2~7倍 D2提示", "⚠️非投资建议"]
    title = f"怡达 {sig_mmdd} · 提示{len(picks)}只"
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
