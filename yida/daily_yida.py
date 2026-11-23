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
  ③ 量能平稳：四天量能变异系数 CV < 20%（排除逐日爆量）
  ④ 提示日 = 放量第2天(D2)，以 D2 收盘价买入
  ⑤ 命中 = 买入后5日内最高收益 ≥ 3%

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
def detect(bars, cfg):
    """bars: 共享K线列表 [{date,open,high,low,close,volume}, ...] 升序
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
        if not np.all((ratios >= cfg["vol_lo"]) & (ratios <= cfg["vol_hi"])):
            continue
        # ③ 量能平稳
        cv = np.std(vols) / np.mean(vols)
        if cv > cfg["cv_max"]:
            continue
        # D1前一日不得已放量（确保D1是启动首日）
        if v[i - 1] / b > 2.5:
            continue
        # ④ 提示日 D2
        j = i + sig_day - 1
        if j >= n:
            continue
        buy = c[j]
        fwd = h[j + 1:j + 1 + hold]
        ret5 = float(np.max(fwd) / buy - 1) if len(fwd) else None
        return dict(
            code=str(bars[j]).zfill(6) if False else "",  # 占位，主程序填 code
            d1=dates[i], signal=dates[j],
            buy=round(float(buy), 2),
            decline=round(float(prev / peak[i] - 1) * 100, 1),
            vol_mult=round(float(np.mean(ratios)), 2),
            cv=round(float(cv) * 100, 1),
            ret5=round(ret5 * 100, 1) if ret5 is not None else None,
            hit=bool(ret5 is not None and ret5 >= cfg["target"]),
        )
    return None


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
        sig = detect(bars, cfg)
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

    # 推送（复用统一通道，含 PushPlus→Server酱 兜底）
    if not args.no_push and picks:
        title = f"怡达模型 · {datetime.datetime.now(kit.cm.BJT).strftime('%m-%d')} 选股提示"
        lines = [f"## 📈 怡达模型 · 每日选股提示", "",
                 f"> 样本池 {len(universe)} 只 / 信号 **{len(picks)}** 个", "",
                 "| 代码 | 名称 | 提示日 | 买入价 | 阴跌 | 放量 | CV | 5日收益 |",
                 "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        for p in picks:
            ret = f"{p['ret5']}%" if p["ret5"] is not None else "待验证"
            hit = " ✅" if p["hit"] else ""
            lines.append(f"| {p['code']} | {p['name']} | {p['signal']} | {p['buy']} | "
                         f"{p['decline']}% | {p['vol_mult']}x | {p['cv']}% | {ret}{hit} |")
        lines += ["", "---", "**模型**：阴跌≥30% + 平台放量2~7倍 + CV<20% + 放量第2天提示",
                  "", "⚠️ 历史统计不构成投资建议，市场有风险。"]
        ok, msg = common_notify.send_alert(title, "\n".join(lines))
        print(f"[yida] 推送：{'成功' if ok else '失败'} {msg}", flush=True)


if __name__ == "__main__":
    main()
