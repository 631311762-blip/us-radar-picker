#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
莲花模型 · 每日选股推送（统一流水线版）
=========================================
按仓库共同前置层契约接入，与其他模型（yida/kaizhong/low_shadow 等）一致：
  · 样本池：读 common/state/universe.json（已剔除 ST/立信/监管立案/次新）
  · K线：读 common/state/klines 共享缓存（ctx.kline，严格离线）
  · 台账：ctx.record() 落盘，自动进入次日统一回溯（条件④）
  · 推送：复用 common/notify.py（PushPlus，token 从 PUSHPLUS_TOKEN 环境变量读）

模型逻辑：大涨突破后3~6日下探实体中位线并回升（黄金坑），提示日=下探回升当日；
         主策略V1.2 + 精选池V1.4四闸门；T+1开盘买、T+2高点卖。

运行：
  python3 lotus/daily_lotus.py                # 正式：选股+落台账+推送
  python3 lotus/daily_lotus.py --no-push      # 调试：只选股落台账，不推送
"""
import os
import sys
import json
import argparse
import numpy as np

# 仓库 common 层在上级目录
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "common"))

import kit
import notify as common_notify

# ---- 参数（与 lotus/config.json 保持一致）----
P_BASE = dict(cv=0.09, amp=0.30, brk=9.0, vr=1.5, gap=(3, 6), vcv=0.50)
P_LEAN = dict(body_min=0.5, depth_lo=20, depth_hi=150, tip_lo=-2, tip_hi=3)
P_WANE = dict(amp15=28.0, cv15=5.0, shrink=1.0)
MIN_BARS = 45


def load_cfg():
    path = os.path.join(os.path.dirname(__file__), "config.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def scan_one(ctx, code, name):
    """对单只股票跑莲花模型，返回命中信号 dict 或 None。"""
    bars = ctx.kline(code, count=120)
    if not bars or len(bars) < MIN_BARS:
        return None
    # 统一为有序列表，按 date 排序
    bars = sorted(bars, key=lambda b: b.get("date", ""))
    n = len(bars)
    c = np.array([b["close"] for b in bars], dtype=float)
    o = np.array([b["open"] for b in bars], dtype=float)
    h = np.array([b["high"] for b in bars], dtype=float)
    l = np.array([b["low"] for b in bars], dtype=float)
    v = np.array([b["volume"] for b in bars], dtype=float)
    dates = [b["date"] for b in bars]

    with np.errstate(divide="ignore", invalid="ignore"):
        pct = np.divide(np.diff(c, prepend=c[0]), c, out=np.zeros(n), where=c != 0) * 100
        vma5 = np.convolve(v, np.ones(5) / 5, mode="full")[:n]
        vma5[0:4] = np.nan
        vr = v / vma5

    # 提示日 = K线最新交易日（收盘后由前置层落盘）；比硬编码 ctx.today 更稳健，
    # 避免数据落盘延迟当天抓不到信号。流水线交易日 15:10 跑，最新即当日。
    target = dates[-1]
    for b in range(20, n):
        if not (pct[b] >= P_BASE["brk"] and vr[b] >= P_BASE["vr"]):
            continue
        body = abs(c[b] - o[b])
        if body <= 0:
            continue
        mid = (o[b] + c[b]) / 2
        w = c[b - 20:b]
        if len(w) < 20:
            continue
        if w.std() / w.mean() > P_BASE["cv"]:
            continue
        if (w.max() - w.min()) / w.min() > P_BASE["amp"]:
            continue
        # 下探日必须落在提示日 today（流水线当天），且间隔 3~6 交易日
        for t in range(b + 3, b + 7):
            if t >= n:
                continue
            if dates[t] != target:
                continue
            depth = (mid - l[t]) / body * 100
            if depth < 0 or not (c[t] >= o[t]):
                continue
            m = v[b + 1:t + 1]
            if len(m) < 3 or (m.std() / m.mean() if m.mean() > 0 else 9) > P_BASE["vcv"]:
                continue
            tpct = (c[t] / c[t - 1] - 1) * 100
            # 精选池四闸门
            w15h, w15l = h[b - 15:b], l[b - 15:b]
            w15c, w15v = c[b - 15:b], v[b - 15:b]
            amp15 = (w15h.max() - w15l.min()) / w15l.min() * 100
            cv15 = w15c.std() / w15c.mean() * 100
            shrink = w15v[-5:].mean() / w15v[:5].mean() if w15v[:5].mean() > 0 else 9
            g1 = P_LEAN["tip_lo"] <= tpct <= P_LEAN["tip_hi"]
            g2 = body >= P_LEAN["body_min"]
            g3 = P_LEAN["depth_lo"] <= depth <= P_LEAN["depth_hi"]
            g4 = (amp15 <= P_WANE["amp15"] and cv15 <= P_WANE["cv15"]
                  and shrink < P_WANE["shrink"])
            is_lean = g1 and g2 and g3 and g4
            return dict(
                code=code, name=name,
                big_day=dates[b], big_pct=round(float(pct[b]), 1), vr=round(float(vr[b]), 2),
                gap=t - b, depth=round(float(depth), 1), body=round(float(body), 2),
                tip_pct=round(float(tpct), 2), tip_close=round(float(c[t]), 2),
                amp15=round(float(amp15), 1), cv15=round(float(cv15), 2),
                shrink=round(float(shrink), 3), tip_date=dates[t],
                level="精选池" if is_lean else "主策略",
                gates="".join("过" if x else "拦" for x in (g1, g2, g3, g4)),
            )
    return None


def build_push(ctx, picks, universe_n, tip_date):
    """生成微信推送内容（PushPlus html 模板友好）。"""
    lean = [p for p in picks if p["level"] == "精选池"]
    base = [p for p in picks if p["level"] == "主策略"]
    title = "莲花模型 · %s 提示（精选%d/主策略%d）" % (tip_date, len(lean), len(base))
    lines = ["<b>莲花模型 · %s</b>｜样本池 %d｜提示日=下探回升日" % (tip_date, universe_n), ""]
    if not picks:
        lines.append("<b>今日无信号。</b>满足条件的股票本就不多，空仓也是一种操作，勿为凑数追高。")
    else:
        if lean:
            lines.append("<b>精选池 V1.4（四闸门全过·优先）——%d只</b>" % len(lean))
            lines.append("<table border='1' cellpadding='4' cellspacing='0' style='border-collapse:collapse;font-size:12px'>")
            lines.append("<tr><th>代码</th><th>名称</th><th>提示日涨幅</th><th>深度</th><th>实体</th><th>前15振幅/CV</th><th>萎缩比</th></tr>")
            for p in lean:
                lines.append("<tr><td>%s</td><td>%s</td><td>%.2f%%</td><td>%.1f%%</td><td>%.2f</td><td>%.1f%%/%.2f%%</td><td>%.3f</td></tr>" % (
                    p["code"], p["name"], p["tip_pct"], p["depth"], p["body"],
                    p["amp15"], p["cv15"], p["shrink"]))
            lines.append("</table><br>")
        if base:
            lines.append("<b>主策略 V1.2（放宽版）——%d只</b>" % len(base))
            lines.append("<table border='1' cellpadding='4' cellspacing='0' style='border-collapse:collapse;font-size:12px'>")
            lines.append("<tr><th>代码</th><th>名称</th><th>提示日涨幅</th><th>深度</th><th>实体</th></tr>")
            for p in base:
                lines.append("<tr><td>%s</td><td>%s</td><td>%.2f%%</td><td>%.1f%%</td><td>%.2f</td></tr>" % (
                    p["code"], p["name"], p["tip_pct"], p["depth"], p["body"]))
            lines.append("</table><br>")
    lines.append("买入=提示日次日(T+1)开盘价，卖出=T+2高点。最高价为理想化卖点，实盘会打折。")
    lines.append("历史形态统计，不构成投资建议。")
    return title, "<br>".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-push", action="store_true", help="只选股落台账，不推送")
    args = ap.parse_args()

    cfg = load_cfg()
    ctx = kit.ModelContext("lotus")
    print("[lotus] 莲花模型每日流程启动，日期=%s" % ctx.today, flush=True)

    universe = ctx.universe()
    print("[lotus] 共同样本池 %d 只" % len(universe), flush=True)

    picks = []
    for x in universe:
        code = str(x.get("code", "")).zfill(6)
        name = x.get("name", "")
        if not code or code == "000000":
            continue
        try:
            sig = scan_one(ctx, code, name)
        except Exception as e:
            print("[lotus] %s 扫描异常: %s" % (code, e), flush=True)
            continue
        if sig:
            picks.append(sig)

    # 精选池优先，其次按下探深度
    picks.sort(key=lambda p: (0 if p["level"] == "精选池" else 1, -p["depth"]))
    n_lean = sum(1 for p in picks if p["level"] == "精选池")
    print("[lotus] 命中 %d 只（精选池 %d / 主策略 %d）" % (
        len(picks), n_lean, len(picks) - n_lean), flush=True)

    # 落台账（统一格式；ctx.record 自带 ST/立信/样本池 gate）
    # 日期用实际提示日（K线最新交易日），避免与统一回溯错位
    tip_date = picks[0]["tip_date"] if picks else ctx.today
    ctx.record(picks, date=tip_date,
               extra={"model_version": cfg.get("版本", "V1.4")})

    # 推送（token 由 PUSHPLUS_TOKEN 环境变量提供，来自仓库 secret）
    if not args.no_push:
        title, content = build_push(ctx, picks, len(universe), tip_date)
        ok, detail = common_notify.send_alert(title, content)
        print("[lotus] 推送结果: %s (%s)" % (ok, detail), flush=True)
    else:
        print("[lotus] --no-push，跳过推送", flush=True)

    print("[lotus] 完成", flush=True)


if __name__ == "__main__":
    main()
