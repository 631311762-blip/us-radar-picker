# -*- coding: utf-8 -*-
"""
凯众股份相似股分析 —— 每日选股主流程
用法：KZ_TOP=6 python3 daily.py
"""
import os
import sys
import json
from datetime import datetime

import lib
import recommend as rec

TOP = int(os.environ.get("KZ_TOP", "6"))
TODAY = datetime.now().strftime("%Y-%m-%d")
# 默认维持「只在放量突破触发日推送」；设 KZ_PUSH_DAILY=1 可改为每交易日推日报
PUSH_DAILY = os.environ.get("KZ_PUSH_DAILY", "0") == "1"
COMPLIANT_NOTE = "（合规剔除已在筛选阶段静默完成，不单独列出）"
STATUS_CN = {"good": "好", "mid": "中", "bad": "差",
             "invalid": "作废", "expired": "作废(超时)", "waiting": "等待中"}


def build_push(fired, picks, last_date, tracked_n, ctx):
    """推送版：每只标注「首日推荐 / 推荐第 N 天」，并附「前期推荐复盘」。
    默认只发当日真正放量突破触发的标的，等待期观察单不推送。"""
    return rec.build_push(fired, picks, last_date, tracked_n, ctx, daily_mode=PUSH_DAILY)


def build_report(picks, replay_rows, last_date, fired=None, ctx=None):
    lines = []
    lines.append(f"# 凯众形态相似股 · {last_date}")
    lines.append("")
    lines.append("> 只做放量突破：触发价 + 所需成交量 同时满足才是买点；回踩区间仅为形态失效参考，不买入。")
    lines.append("")
    lines.append("## 〇、今日触发（放量突破确认）")
    if fired:
        for r in fired:
            lines.append(f"- **{r['name']}({r['code']})** 收盘 {r.get('today_close')}（{r.get('today_chg','--')}%），"
                         f"量 {r.get('today_vol_wan')} 万手 ≥ 所需 {round(float(r['req_vol'])/1e6,2)} 万手，"
                         f"触发价 {r['trigger']}，止损 {r['stop']}，评分 {r.get('score')}")
    else:
        lines.append("- 今日无样本放量突破触发，等待期不推送。")
    lines.append("")
    lines.append("## 一、买点回放")
    if replay_rows:
        for r in replay_rows:
            st = {"good": "好", "mid": "中", "bad": "差",
                  "invalid": "作废", "expired": "作废(超时)", "waiting": "等待中"}.get(r["status"], r["status"])
            ret = f'{r.get("ret")}%' if r.get("ret") is not None else "--"
            lines.append(f'- {r["name"]}({r["code"]}) {r["date"]} 推荐：{st}，收益 {ret}。{r.get("note","")}')
    else:
        lines.append("- 无历史推荐记录（首期），自本期起开始跟踪回放。")
    lines.append("")
    lines.append(f"## 二、今日最优 {len(picks)} 只")
    lines.append("")
    if ctx:
        lines.append("| 名称 | 代码 | 推荐标注 | 首推日 | 题材 | 符合原因 | 触发价 | 所需成交量(万手) | 止损 | 失效线 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
    else:
        lines.append("| 名称 | 代码 | 题材 | 符合原因 | 触发价 | 所需成交量(万手) | 止损 | 失效线 |")
        lines.append("|---|---|---|---|---|---|---|---|")
    for p in picks:
        theme = (p["concepts"].split(",")[0] if p["concepts"] else "") or p["industry"]
        if ctx:
            tg, fd = rec.tag_of(p["code"], last_date, ctx["fmap"], ctx["cal"])
            lines.append(
                f'| {p["name"]} | {p["code"]} | **{tg}** | {fd} | {theme} | {p["why"]} | {p["trigger"]} | '
                f'{p["req_vol_wan"]} | {p["stop"]} | {p["invalid_line"]} |')
        else:
            lines.append(
                f'| {p["name"]} | {p["code"]} | {theme} | {p["why"]} | {p["trigger"]} | '
                f'{p["req_vol_wan"]} | {p["stop"]} | {p["invalid_line"]} |')
    lines.append("")
    lines.append("## 三、明细")
    for p in picks:
        lines.append(f'### {p["name"]} ({p["code"]})')
        lines.append(f'- 题材概念：{p["concepts"] or p["industry"]}')
        lines.append(f'- 形态：{p["start_date"]} 放量启动(+{p["start_chg"]}%，量能 {p["start_vol_x"]}倍)，'
                     f'其后 {p["days"]} 日缩量整理(量能萎缩至 {p["turn_shrink"]})，'
                     f'平台上沿 {p["platform_top"]}，现价 {p["last_close"]}')
        lines.append(f'- 买点：收盘 ≥ {p["trigger"]} 且当日成交量 ≥ {p["req_vol_wan"]} 万手（放量突破）')
        lines.append(f'- 止损：{p["stop"]}（-5%）　形态失效线：{p["invalid_line"]}（跌破即作废，回踩不买）')
        lines.append(f'- 有效期：{lib.BUY_P["max_wait_days"]} 个交易日未触发自动作废')
        lines.append("")
    if ctx:
        lines.append("## 四、前期推荐复盘")
        lines.append("")
        lines.append("> 同一标的按代码归口：只记一次「首日推荐」，之后标「推荐第 N 天」，不再重复计为新推荐。")
        lines.append("")
        lines += rec.review_lines(ctx["review"])
        lines.append("")
    return "\n".join(lines)


def main():
    print(f"[1/6] 探测最新交易日 ...")
    try:
        last_date = lib.market_last_date()
    except Exception as e:
        print(f"行情探测失败：{e}")
        sys.exit(1)
    print(f"      最新K线日期：{last_date}（今日 {TODAY}）")
    if last_date != TODAY:
        print("今日非交易日，无更新")
        return

    print("[2/6] 抓取全市场快照 ...")
    spot = lib.fetch_spot_all()
    print(f"      共 {len(spot)} 只")

    print("[3/6] 加载 F10（审计机构 / 题材 / 行业）...")
    f10 = lib.load_f10()
    print(f"      F10 记录 {len(f10)} 条")

    # 初筛：非 ST/退市、有成交、价格达标（合规剔除在 lib.screen 中静默执行）
    spot = spot[(spot["volume"] > 0) & (spot["trade"] >= lib.MIN_PRICE) & (spot["amount"] >= lib.MIN_AMOUNT)]
    items = []
    names = {}
    for _, r in spot.iterrows():
        code = str(r["code"])
        sym = str(r["symbol"])
        if sym.startswith("bj"):        # 北交所不参与（流动性/涨跌幅规则差异大）
            continue
        items.append((code, sym))
        names[code] = r["name"]
    print(f"      初筛后 {len(items)} 只")

    print("[4/6] 增量更新日线缓存 ...")
    ok, fail = lib.update_cache(items, workers=16, last_trade_date=last_date)
    print(f"      更新成功 {ok}，失败 {fail}")

    print("[5/6] 形态筛选 ...")
    picks_all = lib.screen(items, names, f10)
    print(f"      命中 {len(picks_all)} 只，取前 {TOP}")

    # 一句话符合原因
    for p in picks_all:
        p["why"] = (f'{p["start_date"]} 放量启动(+{p["start_chg"]}%)，'
                    f'{p["days"]} 日缩量整理不破失效线，现价贴近平台上沿 {p["platform_top"]}，待放量突破')

    picks = picks_all[:TOP]

    # 台账写全部命中样本（用于次日回溯分析），推送只发前 TOP 只
    hist = lib.load_history()
    exist = {(h["code"], h["date"]) for h in hist}
    sym_map = dict(items)
    added = 0
    for p in picks_all:
        key = (p["code"], p["date"])
        if key not in exist:
            hist.append({
                "code": p["code"], "name": p["name"], "date": p["date"],
                "symbol": sym_map.get(p["code"], ""),
                "trigger": p["trigger"], "req_vol": p["req_vol"],
                "stop": p["stop"], "invalid_line": p["invalid_line"],
                "score": round(float(p["score"]), 4),
                "fakebreaks": p.get("fakebreaks", 0),
                "pushed": key in {(x["code"], x["date"]) for x in picks},
                "status": "waiting", "note": "",
            })
            exist.add(key)
            added += 1
    print(f"      台账新增 {added} 个样本（全量命中 {len(picks_all)} 只）")

    # 回溯分析：以前一交易日收盘为基准，评估全部在跟踪样本
    prev_date = lib.market_last_date(last_date) or last_date
    replay_rows = lib.replay(hist, [], as_of=prev_date) if hist else []
    print(f"      回溯基准日 {prev_date}，在跟踪样本 {len(replay_rows)} 个")
    lib.save_history(hist)

    # 推荐归口上下文：按代码算「首日推荐 / 推荐第 N 天」+ 前期复盘
    ctx = rec.prepare(hist, last_date)

    fired = lib.detect_today_triggers(hist, last_date)
    report = build_report(picks, replay_rows, last_date, fired=fired, ctx=ctx)
    rp = os.path.join(lib.REPORT_DIR, f"{last_date}.md")
    open(rp, "w", encoding="utf-8").write(report)
    json.dump(picks, open(os.path.join(lib.REPORT_DIR, f"{last_date}.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"      报告：{rp}")

    # 资料库台账（字段齐全，供自动化任务直接上传，避免执行端编造）
    ledger = rec.build_ledger(picks, hist, last_date, ctx, candidate_n=len(items))
    lp = os.path.join(lib.REPORT_DIR, f"凯众台账_{last_date}.md")
    open(lp, "w", encoding="utf-8").write(ledger)
    print(f"      台账：{lp}")

    print(f"[6/6] 当日突破触发检测：{len(fired)} 只")
    should_push = bool(fired) or PUSH_DAILY
    if should_push:
        push_md = build_push(fired[:TOP], picks, last_date, len(hist), ctx)
        title = (f"凯众推荐日报 {last_date}" if PUSH_DAILY
                 else f"凯众突破触发 {last_date}（{len(fired[:TOP])}只）")
        lib.push_ftqq(title, push_md)
        mode = "每交易日报" if PUSH_DAILY else "仅触发日"
        print(f"[push] 推送模式：{mode}；含首日/第N天标注 + 前期推荐复盘")
        if fired:
            print("\n===== 今日触发 =====")
            for r in fired[:TOP]:
                tg, fd = rec.tag_of(r["code"], last_date, ctx["fmap"], ctx["cal"])
                print(f'{r["name"]}({r["code"]}) [{tg}·首推{fd}] 收盘:{r.get("today_close")} '
                      f'量:{r.get("today_vol_wan")}万手 触发价:{r["trigger"]} 评分:{r.get("score")}')
    else:
        print("[push] 今日无突破触发，等待期不推送（报告与台账照常落盘）")
    return fired


if __name__ == "__main__":
    main()
