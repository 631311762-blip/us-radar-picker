# -*- coding: utf-8 -*-
"""
推荐归口 / 首日·第N天标注 / 前期推荐复盘 —— 独立模块

按 2026-11-07 用户要求落地「此规则」：
  1) 同一标的按 code 归口：台账(history.json)里最早出现日 = 首推日。
     当日首次出现标「首日推荐」，之后再出现标「推荐第 N 天」，不再重复当新推荐。
     （针对岳阳林纸 10-26 首推、10-27/28 连续被当成新推荐重复提示的问题）
  2) 推送与每日报告必须带「前期推荐复盘」。
  3) 推送仍遵循「只在放量突破触发日推送、等待期不推观察单」；
     触发标的标注它是「首日推荐」还是「推荐第 N 天后才触发」。
  4) 不改 lib.py 的 SCHEME / BUY_P / analyze / screen —— 仅消费台账与交易日历。

开关：
  KZ_PUSH_DAILY=1  改为每个交易日推送一份日报（含当日推荐清单 + 标注 + 复盘）

用法（离线验证）：python3 recommend.py 2026-11-06
"""
import os
import sys
import glob
import json

import pandas as pd

import lib

TERMINAL = ("good", "mid", "bad", "invalid", "expired")
STATUS_CN = {"good": "好", "mid": "中", "bad": "差",
             "invalid": "作废", "expired": "作废(超时)",
             "waiting": "等待中", "triggered": "已触发"}


def load_calendar(max_files=12):
    """交易日历：取若干只 kline 的 date 并集，避免读全市场 3700+ 文件。"""
    ds = set()
    for f in sorted(glob.glob(os.path.join(lib.KLINE_DIR, "*.csv")))[:max_files]:
        try:
            ds.update(pd.read_csv(f, usecols=["date"])["date"].astype(str).tolist())
        except Exception:
            continue
    return sorted(ds)


def first_rec_map(hist):
    """code -> 首推日（台账里最早 date）。这是「首日推荐」归口的唯一依据。"""
    m = {}
    for r in hist:
        c = str(r.get("code", "")).zfill(6)
        d = str(r.get("date", ""))
        if not d:
            continue
        if c not in m or d < m[c]:
            m[c] = d
    return m


def latest_per_code(hist):
    """code -> 最新一条记录（复盘状态按最新态统计，避免重复计数）。"""
    m = {}
    for r in hist:
        c = str(r.get("code", "")).zfill(6)
        if c not in m or str(r.get("date", "")) >= str(m[c].get("date", "")):
            m[c] = r
    return m


def trade_day_no(cal, d0, d1):
    """d0~d1（含两端）之间的交易日数，至少 1。"""
    return max(1, sum(1 for x in cal if d0 <= x <= d1))


def tag_of(code, today, fmap, cal):
    """返回 (标签, 首推日)。首日推荐 / 推荐第 N 天。"""
    c = str(code).zfill(6)
    fd = fmap.get(c, today)
    if fd >= today:
        return "首日推荐", fd
    return f"推荐第 {trade_day_no(cal, fd, today)} 天", fd


def review(hist, today, cal, fmap, topn=3):
    """前期推荐复盘数据。"""
    latest = latest_per_code(hist)
    wait_limit = int(lib.BUY_P.get("max_wait_days", 5))

    terminal = [r for r in latest.values() if r.get("status") in TERMINAL]
    tracking = [r for r in latest.values() if r.get("status") in ("waiting", "triggered")]

    # 收益样本按 code 取最新一条，避免同一标的重复计数
    ret_seen = {}
    for r in hist:
        if isinstance(r.get("ret"), (int, float)):
            c = str(r["code"]).zfill(6)
            if c not in ret_seen or str(r.get("date", "")) >= str(ret_seen[c].get("date", "")):
                ret_seen[c] = r
    ret_items = sorted(ret_seen.values(), key=lambda x: -float(x["ret"]))
    rets = [float(r["ret"]) for r in ret_items]
    win = round(sum(1 for x in rets if x > 0) / len(rets) * 100, 1) if rets else None
    avg = round(sum(rets) / len(rets), 2) if rets else None

    # 状态分布
    dist = {}
    for r in terminal:
        s = STATUS_CN.get(r.get("status"), r.get("status"))
        dist[s] = dist.get(s, 0) + 1

    # 在跟踪：已等待交易日、剩余有效日、临近作废
    lefts = []
    for r in tracking:
        waited = trade_day_no(cal, str(r.get("date", today)), today)
        lefts.append(wait_limit - (waited - 1))
    avg_wait = round(sum(trade_day_no(cal, str(r.get("date", today)), today)
                         for r in tracking) / len(tracking), 1) if tracking else 0
    near_expire = sum(1 for x in lefts if 0 < x <= 2)
    expired_waiting = sum(1 for x in lefts if x <= 0)

    # 重复推荐归口（同一 code 出现 >=2 次）
    cnt = {}
    for r in hist:
        c = str(r.get("code", "")).zfill(6)
        cnt[c] = cnt.get(c, 0) + 1
    dups = []
    for c, n in sorted(cnt.items(), key=lambda x: -x[1]):
        if n < 2:
            continue
        nm = next((str(r.get("name", "")) for r in hist
                   if str(r.get("code", "")).zfill(6) == c), c)
        fd = fmap.get(c, today)
        tg, _ = tag_of(c, today, fmap, cal)
        dups.append(dict(code=c, name=nm, times=n, first=fd, tag=tg))
        if len(dups) >= 5:
            break

    return dict(
        total_codes=len(fmap), total_records=len(hist),
        terminal_n=len(terminal), dist=dist,
        tracking_n=len(tracking), avg_wait=avg_wait,
        near_expire=near_expire, expired_waiting=expired_waiting,
        ret_n=len(rets), win=win, avg=avg,
        # 收益样本通常很少，统一按收益降序给明细，避免「表现最好/最差」重叠或全是正数的尴尬
        ret_all=[(r.get("name", r["code"]), str(r["code"]).zfill(6), float(r["ret"]))
                 for r in ret_items[:6]],
        dups=dups,
    )


def prepare(hist, today):
    cal = load_calendar()
    fmap = first_rec_map(hist)
    return dict(cal=cal, fmap=fmap, today=today,
                review=review(hist, today, cal, fmap))


def _f10(hist_codes=None):
    try:
        return lib.load_f10()
    except Exception:
        return {}


def _theme(r, f10):
    row = f10.get(str(r.get("code", "")).zfill(6), {})
    c = (row.get("concepts") or "").split(",")[0] if row.get("concepts") else ""
    return c or row.get("industry", "") or r.get("industry", "") or "—"


def _chg_of(code, today):
    """从本地日线算当日真实涨跌幅；取不到就写「未提供」，绝不编造。"""
    f = os.path.join(lib.KLINE_DIR, f"{str(code).zfill(6)}.csv")
    try:
        df = pd.read_csv(f)
        df["date"] = df["date"].astype(str)
        df = df[df["date"] <= today]
        c = df["close"].values.astype(float)
        if len(c) < 2 or c[-2] <= 0:
            return "未提供"
        return f"{(c[-1] / c[-2] - 1) * 100:+.2f}%"
    except Exception:
        return "未提供"


def review_lines(rv):
    """复盘章节 Markdown 行（推送与报告共用）。"""
    L = []
    dist = " / ".join(f"{k} {v}" for k, v in rv["dist"].items()) or "无"
    L.append(f"- 累计推荐 **{rv['total_codes']}** 只标的（台账 {rv['total_records']} 笔记录，"
             f"重复推荐已按代码归口，不再重复计为新推荐）")
    stat = f"- 已了结 **{rv['terminal_n']}** 只：{dist}"
    if rv["ret_n"]:
        stat += (f"；有收益样本 **{rv['ret_n']}** 只，胜率 **{rv['win']}%**，"
                 f"平均收益 **{rv['avg']:+.2f}%**")
        if rv["ret_n"] < 10:
            stat += "（样本偏少，仅供参考）"
    L.append(stat)
    L.append(f"- 在跟踪 **{rv['tracking_n']}** 只，平均已等待 **{rv['avg_wait']}** 个交易日；"
             f"其中 **{rv['near_expire']}** 只剩 ≤2 天作废"
             + (f"，**{rv['expired_waiting']}** 只已超期" if rv["expired_waiting"] else ""))
    if rv["ret_all"]:
        L.append("- 收益明细（按收益降序）："
                 + "、".join(f"{n}({c}) {v:+.2f}%" for n, c, v in rv["ret_all"]))
    if rv["dups"]:
        seg = "；".join(f"{d['name']}({d['code']}) {d['first']} 首推·累计 {d['times']} 次→{d['tag']}"
                       for d in rv["dups"][:3])
        L.append(f"- 重复推荐归口：{seg}")
    return L


NOTE = ("止损统一 -5%；回踩不是买点；未触发 "
        f"{int(lib.BUY_P.get('max_wait_days', 5))} 个交易日自动作废。"
        "（合规剔除已在筛选阶段静默完成，不单独列出）")


def build_push(fired, picks, last_date, tracked_n, ctx, daily_mode=False):
    """组装方糖推送 Markdown。daily_mode=False 时仅触发日推送，不推观察单。"""
    fmap, cal, rv = ctx["fmap"], ctx["cal"], ctx["review"]
    f10 = _f10()
    title = "凯众推荐日报" if daily_mode else "凯众突破触发"
    L = [f"# {title} · {last_date}", ""]
    L.append("> 【推荐归口】同一标的只记一次「首日推荐」，之后再出现标「推荐第 N 天」，"
             "不再重复当成新推荐。")
    L.append("")

    if daily_mode:
        L.append(f"## 一、今日推荐清单（{len(picks)} 只）")
        L.append("")
        L.append("| 标的 | 推荐标注 | 首推日 | 触发价 | 所需量(万手) | 止损 | 题材 |")
        L.append("|---|---|---|---|---|---|---|")
        for p in picks:
            tg, fd = tag_of(p["code"], last_date, fmap, cal)
            L.append(f"| {p['name']}({p['code']}) | **{tg}** | {fd} | {p['trigger']} | "
                     f"{p['req_vol_wan']} | {p['stop']} | {_theme(p, f10)} |")
        L.append("")

    sec = "二" if daily_mode else "一"
    L.append(f"## {sec}、今日突破触发（放量突破买点 · {len(fired)} 只）")
    L.append("")
    if fired:
        for i, r in enumerate(fired, 1):
            tg, fd = tag_of(r["code"], last_date, fmap, cal)
            L.append(f"**{i}. {r['name']}({r['code']})**　`{tg}`（首推 {fd}）　"
                     f"评分 {r.get('score', '--')}")
            L.append(f"- 收盘 **{r.get('today_close')}**（{r.get('today_chg', '--')}%），"
                     f"量 {r.get('today_vol_wan')} 万手 ≥ 所需 "
                     f"{round(float(r['req_vol']) / 1e6, 2)} 万手")
            L.append(f"- 买入价按 **{r['trigger']}** 计（开盘更高则按开盘），"
                     f"止损 **{r['stop']}**（-5%）")
            L.append(f"- 题材：{_theme(r, f10)}")
            L.append("")
    else:
        L.append("- 今日无样本放量突破触发，等待期不推送观察单。")
        L.append("")

    sec = "三" if daily_mode else "二"
    L.append(f"## {sec}、前期推荐复盘")
    L.append("")
    L += review_lines(rv)
    L.append("")
    L.append(f"> 台账共跟踪 {tracked_n} 笔记录。{NOTE}")
    return "\n".join(L)


def build_ledger(picks, hist, last_date, ctx, candidate_n=None):
    """生成供资料库上传的《凯众台账_<date>.md》，字段齐全，避免执行端编造。"""
    fmap, cal, rv = ctx["fmap"], ctx["cal"], ctx["review"]
    f10 = _f10()
    L = [f"# 凯众形态相似股模型 · 每日推荐台账 · {last_date}", ""]
    L.append(f"- 基准日：{last_date}")
    L.append("- 模型名：凯众形态相似股（只做放量突破）")
    L.append(f"- 候选池数量：{candidate_n if candidate_n is not None else '未提供'}")
    L.append("")
    L.append("## 今日最优 6 只")
    L.append("")
    L.append("| 名称 | 6位代码 | 推荐标注 | 首推日 | 题材 | 一句话原因 | 触发价 | 所需量(万手) | 止损 | 当日收盘 | 当日涨跌幅 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for p in picks:
        tg, fd = tag_of(p["code"], last_date, fmap, cal)
        L.append(f"| {p['name']} | {p['code']} | {tg} | {fd} | {_theme(p, f10)} | "
                 f"{p.get('why', '—')} | {p['trigger']} | {p['req_vol_wan']} | {p['stop']} | "
                 f"{p.get('last_close', '未提供')} | {_chg_of(p['code'], last_date)} |")
    L.append("")
    L.append("## 在跟踪清单")
    L.append("")
    tracking = [r for r in latest_per_code(hist).values() if r.get("status") == "waiting"]
    if tracking:
        L.append("| 名称 | 代码 | 推荐标注 | 触发价 | 剩余有效交易日 |")
        L.append("|---|---|---|---|---|")
        for r in tracking[:30]:
            tg, fd = tag_of(r["code"], last_date, fmap, cal)
            left = int(lib.BUY_P.get("max_wait_days", 5)) - (
                trade_day_no(cal, str(r.get("date", last_date)), last_date) - 1)
            L.append(f"| {r.get('name', '')} | {str(r['code']).zfill(6)} | {tg} | "
                     f"{r['trigger']} | {max(left, 0)} |")
        if len(tracking) > 30:
            L.append(f"\n（其余 {len(tracking) - 30} 只从略）")
    else:
        L.append("- 无在跟踪样本。")
    L.append("")
    L.append("## 已作废旧单")
    L.append("")
    old = [r for r in latest_per_code(hist).values()
           if r.get("status") in ("invalid", "expired")]
    if old:
        for r in old[:15]:
            L.append(f"- {r.get('name', '')}({str(r['code']).zfill(6)}) "
                     f"{STATUS_CN.get(r['status'], r['status'])}：{r.get('note', '')}")
    else:
        L.append("- 无。")
    L.append("")
    L.append("## 前期推荐复盘")
    L.append("")
    L += review_lines(rv)
    L.append("")
    L.append("> 本台账供次日「美股行业异动雷达」读取，用于跨市场双模型匹配。")
    return "\n".join(L)


def main():
    """离线预览：python3 recommend.py 2026-11-06 —— 不推送、不写生产台账。"""
    today = sys.argv[1] if len(sys.argv) > 1 else lib.market_last_date()
    hist = lib.load_history()
    ctx = prepare(hist, today)
    picks_path = os.path.join(lib.REPORT_DIR, f"{today}.json")
    picks = json.load(open(picks_path, encoding="utf-8")) if os.path.exists(picks_path) else []
    fired = lib.detect_today_triggers(hist, today) if today == lib.market_last_date() else []
    md = build_push(fired[:6], picks, today, len(hist), ctx, daily_mode=False)
    print(md)
    print("\n\n===== 资料库台账预览（前 40 行）=====")
    print("\n".join(build_ledger(picks, hist, today, ctx).splitlines()[:40]))


if __name__ == "__main__":
    main()
