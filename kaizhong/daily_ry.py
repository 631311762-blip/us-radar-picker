# -*- coding: utf-8 -*-
"""
反包阳线·前期入选模型（Rebound-Yang, RY）· 每日自动化入口
================================================================
2026-11-21 按用户口径上线：把「提示日」从 V3 缩量二次上攻日提前到
「破位后的反包阳线当日」，作为微信「前期入选样本」跟踪。

选股内核：rebound.py（scan_one 与 shrink_breakout 同签名、前半段口径一致）
  横盘(40~60日,集中度≥85%,压力p95/支撑p5) → 试盘(横盘最大量,放量≥3倍)
  → 破位(收盘跌破支撑线) → ★反包阳线当日(阳线+站回支撑+吞没破位实体) = 前期入选

跟踪规则（用户 2026-11-21 定）：
  - 入选日起跟踪 5 个交易日（TRACK_DAYS=5）。
  - ★黄金分割出局线 = 入选日【反包阳线实体】C-(C-O)×0.5=(开+收)/2（0.5 中位线，
    2026-11-21 修正为用户口径：不用含影线的高低点，避免上影线抬高线位误判破线，
    如凯众10-19收10.00应算未破）；任一日**收盘 < 0.5线** → 结束跟踪，
    破线说明一次后写入不可逆出局名单。基准可在 rebound.STANCE['gold_basis'] 切换。
  - ★V3 重点标识：跟踪期内任一日 shrink_breakout.scan_one 命中（V3 缩量二次
    上攻提示日）→ 标「V3 确认」，重点标识——反包入选→V3 是买点升级链条。
  - 0.382 / 0.618 线仅作报告展示与研究参考，不作出局判定。

台账/回退：
  - 独立台账 rebound_history.json；shrink_history.json / v4_history.json 原样保留供回退。
  - push_ry_state.json 仅控制微信展示（破线出局名单），不碰台账与选股口径。
  - 合规剔除静默执行，不在任何汇报中提及。

用法：
  KZ_TOP=6 python3 daily_ry.py                        # 正式：抓快照→更新缓存→扫描→推送
  KZ_SKIP_UPDATE=1 python3 daily_ry.py                # 复跑：用现有缓存
  KZ_AS_OF=2026-11-06 KZ_SKIP_UPDATE=1 KZ_NO_PUSH=1 python3 daily_ry.py  # 离线验证
"""
import os
import sys
import json
import glob
import re
from datetime import datetime

import numpy as np
import pandas as pd

import lib
import rebound as rb
import shrink_breakout as sh
import recommend as rec

TOP = int(os.environ.get("KZ_TOP", "6"))
AS_OF = os.environ.get("KZ_AS_OF") or None
SKIP_UPDATE = os.environ.get("KZ_SKIP_UPDATE", "0") == "1"
NO_PUSH = os.environ.get("KZ_NO_PUSH", "0") == "1"
DEDUP_DAYS = int(os.environ.get("KZ_DEDUP_DAYS", "5"))
TRACK_DAYS = int(os.environ.get("KZ_TRACK_DAYS", "5"))
TODAY = datetime.now().strftime("%Y-%m-%d")
HORIZONS = (1, 3, 5)
RY_HIST = os.path.join(lib.DATA, "rebound_history.json")
RY_STATE = os.path.join(lib.DATA, "push_ry_state.json")
MODEL_DESC = ("反包阳线前期入选：横盘(1-3月,集中度≥85%)→试盘(横盘最大量,放量≥3倍)→破位(跌破支撑线)"
              "→反包阳线当日入选；入选后跟踪5日，收盘破0.5黄金分割线出局，期中现V3缩量二次上攻则重点标识")


def z6(c):
    return str(c).zfill(6)


# ---------------- 数据 ----------------
def refresh_data(last_date):
    spot = lib.fetch_spot_all()
    spot = spot[(spot["volume"] > 0) & (spot["trade"] >= lib.MIN_PRICE)
                & (spot["amount"] >= lib.MIN_AMOUNT)]
    items, names = [], {}
    for _, r in spot.iterrows():
        code, sym = str(r["code"]), str(r["symbol"])
        if sym.startswith("bj"):
            continue
        items.append((code, sym))
        names[code] = r["name"]
    print(f"      初筛 {len(items)} 只")
    ok, fail = lib.update_cache(items, workers=16, last_trade_date=last_date)
    print(f"      缓存更新成功 {ok}，失败 {fail}")
    return names


def scan_all(last_date, f10, names):
    """全市场扫描当日反包入选，并收集收盘矩阵供跟踪/复盘。"""
    hits, series = [], {}
    for f in sorted(glob.glob(os.path.join(lib.KLINE_DIR, "*.csv"))):
        code = os.path.basename(f)[:6]
        try:
            df = pd.read_csv(f)
        except Exception:
            continue
        df["date"] = df["date"].astype(str)
        df = df[df["date"] <= last_date]
        if df.empty:
            continue
        try:
            series[code] = df.set_index("date")[["close", "low", "high", "open"]]
        except Exception:
            pass
        row = f10.get(code, {})
        name = names.get(code) or row.get("name") or code
        if lib.blocked_by_rule(name, code, row):
            continue
        try:
            r = rb.scan_one(code, df, as_of=last_date)
        except Exception:
            continue
        if r:
            r.update(name=name, industry=row.get("industry", ""), concepts=row.get("concepts", ""))
            hits.append(r)
    hits.sort(key=lambda x: -x["score"])
    dates = sorted(set().union(*[set(s.index) for s in series.values()])) if series else []
    px = pd.DataFrame({c: s["close"] for c, s in series.items()}).reindex(dates) if series else pd.DataFrame()
    return hits, dates, px


# ---------------- 台账 ----------------
def load_hist():
    if os.path.exists(RY_HIST):
        try:
            return json.load(open(RY_HIST, encoding="utf-8"))
        except Exception:
            return []
    return []


def save_hist(hist):
    json.dump(hist, open(RY_HIST, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def load_state():
    if os.path.exists(RY_STATE):
        try:
            d = json.load(open(RY_STATE, encoding="utf-8"))
            if isinstance(d, dict) and isinstance(d.get("out"), dict):
                return d
        except Exception:
            pass
    return {"out": {}}


def save_state(state):
    json.dump(state, open(RY_STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def trade_gap(cal, d0, d1):
    return sum(1 for d in cal if d0 < d <= d1)


def update_ledger(hist, hits, last_date, cal, dates, px):
    """新增反包入选（5日去重）+ 刷新全部在跟踪样本的破线/V3状态。"""
    by_code = {}
    for r in hist:
        by_code.setdefault(z6(r["code"]), []).append(r)

    new_n = dup_n = 0
    for h in hits:
        c = z6(h["code"])
        recs = by_code.get(c, [])
        hint_dates = []
        for x in recs:
            hint_dates += (x.get("hit_dates") or [x["date"]])
        near = [d for d in set(hint_dates) if 0 <= trade_gap(cal, d, last_date) <= DEDUP_DAYS]
        if near:
            target = max(recs, key=lambda x: x["date"])
            target.setdefault("hit_dates", [target["date"]])
            if last_date not in target["hit_dates"]:
                target["hit_dates"].append(last_date)
            dup_n += 1
            continue
        item = dict(
            code=c, name=h["name"], date=last_date,
            buy_price=round(float(h["last_close"]), 2),
            gold_500=float(h["gold_500"]), gold_382=float(h["gold_382"]),
            gold_618=float(h["gold_618"]),
            gold_h=float(h["high"]), gold_l=float(h["low"]),
            bd_date=h["bd_date"], b1_date=h["b1_date"],
            platform_top=float(h["platform_top"]), support=float(h["support"]),
            plat_conc=float(h["plat_conc"]), body=float(h.get("body", 0)),
            break_vol_x=float(h.get("break_vol_x", 0)), score=float(h["score"]),
            status="tracking", ret=None, out_date=None, out_reason="",
            v3_date=None, concepts=h.get("concepts", ""), industry=h.get("industry", ""),
            hit_dates=[last_date], last_check=last_date, note="反包阳线入选，跟踪5日",
        )
        hist.append(item)
        by_code.setdefault(c, []).append(item)
        new_n += 1

    # 刷新在跟踪样本：破 0.5 线出局 / V3 重点标识（当日命中）
    # 2026-11-21 设计：破线只结束「5日跟踪表」展示，不屏蔽 V3 重点标识。
    #   V3 是更高级的买点升级信号——凯众即「破0.5线后又走V3」的实例，不能漏。
    idx = {d: i for i, d in enumerate(dates)}
    j_end = idx.get(last_date, len(dates) - 1)
    v3_today = set()
    for r in hist:
        c = z6(r["code"])
        d0 = r["date"]
        held = trade_gap(cal, d0, last_date)
        if held < 0 or held > TRACK_DAYS:
            continue
        j0 = idx.get(d0)
        if j0 is None or c not in px.columns:
            continue
        gl = float(r["gold_500"])
        cur = px[c].iloc[min(j_end, len(dates) - 1)]
        if pd.isna(cur):
            continue
        buy = float(r["buy_price"])
        ret = round((cur / buy - 1) * 100, 2)
        r["ret"] = ret
        r["last_check"] = last_date

        # ① 破 0.5 线（收盘）—— 入选当日不判定（入选本就站上支撑）
        if held >= 1 and r.get("status") != "out":
            seg = px[c].iloc[j0 + 1: min(j0 + held + 1, len(dates))]
            if len(seg) and float(seg.min()) < gl and not r.get("v3_date"):
                r.update(status="out", out_date=last_date,
                         out_reason=f"收盘跌破0.5黄金分割线{gl:.2f}",
                         note=f"反包入选后破0.5线出局，持有{held}日")

        # ② V3 重点标识：跟踪窗口内任一日命中即锁定（破线后仍检测，优先级更高）
        if held >= 1 and not r.get("v3_date"):
            try:
                df = pd.read_csv(os.path.join(lib.KLINE_DIR, c + ".csv"))
                df["date"] = df["date"].astype(str)
                if sh.scan_one(c, df, as_of=last_date):
                    r["v3_date"] = last_date
                    v3_today.add(c)
                    r["status"] = "v3"   # V3 升级，覆盖 out/tracking
            except Exception:
                pass
        if not r.get("v3_date") and r.get("status") != "out":
            r["status"] = "tracking" if held <= TRACK_DAYS else "done"

    save_hist(hist)
    return new_n, dup_n, v3_today


# ---------------- 推送 ----------------
def build_tracking(hist, last_date, cal, state):
    """微信第二部分：跟踪期内(≤5日)前期入选样本 + 破线出局说明一次。"""
    out = state["out"]
    alive, broken_new, v3_mark = [], [], []
    for r in sorted(hist, key=lambda x: x["date"]):
        c = z6(r["code"])
        held = trade_gap(cal, r["date"], last_date)
        v3 = bool(r.get("v3_date"))
        # ★V3确认优先：即使破线出局，V3升级信号也要重点标识（凯众即此例）
        if v3 and str(r.get("v3_date")) == last_date:
            # 曾破0.5线后重新走强 → V3标识加注，提示用户"假破位后转强"
            was_out = bool(r.get("out_date"))
            v3_mark.append((r["name"], c, r["date"], float(r["buy_price"]),
                            float(r.get("ret") or 0), held, float(r["gold_500"]),
                            True, str(r["v3_date"]), was_out))
            continue
        # 破线出局：说明一次，写名单（不再展示在跟踪表）
        if r.get("status") == "out" and c not in out:
            out[c] = {"date": last_date, "first": r["date"], "name": r["name"],
                      "reason": r.get("out_reason", "破0.5线出局")}
            broken_new.append((r["name"], c, r.get("out_reason", "破0.5线出局")))
        if c in out:
            continue
        if held < 0 or held > TRACK_DAYS:
            continue
        if str(r["date"]) == last_date:
            continue  # 当日入选在第一部分
        alive.append((r["name"], c, r["date"], float(r["buy_price"]),
                      float(r.get("ret") or 0), held, float(r["gold_500"]), False, "", False))

    L = []
    if v3_mark:
        L.append("### ★ V3 确认（重点标识：反包入选后现缩量二次上攻，买点升级）")
        L.append("")
        L.append("| 标的 | 入选日 | 入选价 | 现价 | 浮动 | V3确认日 | 备注 |")
        L.append("|---|---|---|---|---|---|---|")
        for nm, c, d0, buy, ret, held, gl, _, v3d, was_out in v3_mark:
            note = "曾破0.5线后重新走强（假破位）" if was_out else "一路未破线"
            L.append(f"| **{nm}({c})** | {d0} | {buy:.2f} | {buy*(1+ret/100):.2f} | "
                     f"**{ret:+.2f}%** | {v3d} | {note} |")
        L.append("")
    if alive:
        L.append("### 前期入选样本 · 5日跟踪（未破0.5线）")
        L.append("")
        L.append("| 标的 | 入选日 | 入选价 | 现价 | 浮动 | 持有 | 0.5线 |")
        L.append("|---|---|---|---|---|---|---|")
        for nm, c, d0, buy, ret, held, gl, _, _, _ in alive:
            L.append(f"| {nm}({c}) | {d0} | {buy:.2f} | {buy*(1+ret/100):.2f} | "
                     f"{ret:+.2f}% | {held}日 | {gl:.2f} |")
        L.append("")
    if not v3_mark and not alive:
        L.append("- 暂无在跟踪的前期入选样本（入选后未破0.5线且未到V3）。")
    if broken_new:
        L.append("")
        L.append(f"**破0.5线出局（{len(broken_new)}只，说明一次后不再推送）：**")
        for nm, c, reason in broken_new:
            L.append(f"- {nm}({c})：{reason}")
    return L, len(broken_new)


def build_push(hits, last_date, tracked_n, v3_today, track_lines):
    L = [f"# 反包阳线·前期入选 · {last_date}", ""]
    L.append(f"> 模型：{MODEL_DESC}。")
    L.append("")
    L.append(f"## 一、今日反包阳线入选（全市场 {len(hits)} 只，取评分前 {min(TOP,len(hits))} 只）")
    L.append("")
    if hits:
        L.append("| 标的 | 入选日 | 当日涨幅 | 破位日 | 0.5线 | 题材 | 质量 |")
        L.append("|---|---|---|---|---|---|---|")
        for h in hits[:TOP]:
            theme = (h.get("concepts", "").split(",")[0] if h.get("concepts") else "") \
                    or h.get("industry", "") or "—"
            star = "★★★" if h["score"] >= 0.6 else ("★★" if h["score"] >= 0.45 else "★")
            L.append(f"| {h['name']}({z6(h['code'])}) | {h['date']} | {h['chg']}% | "
                     f"{h['bd_date']} | {h['gold_500']} | {theme} | {star} |")
        L.append("")
        L.append("> 反包入选=**前期观察**，非直接买点；跟踪5日，收盘破0.5黄金分割线出局，"
                 "期中现V3缩量二次上攻则重点标识（买点升级）。")
    else:
        L.append("- 今日无反包阳线入选。")
    L.append("")
    L.append("## 二、前期入选样本跟踪（5日 / V3标识 / 破线出局）")
    L.append("")
    L += track_lines
    L.append("")
    if v3_today:
        L.append(f"> ★ 今日新出现 V3 确认 {len(v3_today)} 只，已在上方重点标识。")
    L.append(f"> 台账累计入选 {tracked_n} 笔。风险提示：历史回测仅供研究，不构成投资建议；严格按0.5线纪律出局。")
    return "\n".join(L)


def build_report(hits, last_date, tracked_n, dates, px):
    L = [f"# 反包阳线·前期入选模型（RY）· {last_date}", ""]
    L.append(f"> 模型：{MODEL_DESC}")
    L.append("> 形态：横盘(40-60日,集中度≥85%) → 试盘(横盘最大量,放量≥3倍) → 破位(跌破支撑线) "
             "→ 反包阳线当日入选；入选后跟踪5日，破0.5线出局，现V3重点标识。")
    L.append(f"> 覆盖本地日线 {len(px.columns)} 只，当日反包入选 **{len(hits)}** 只。")
    L.append("")
    L.append("## 一、今日反包入选明细")
    L.append("")
    L.append("| 名称 | 代码 | 入选日 | 破位日 | 试盘日 | 平台 | 集中度 | 0.382线 | 0.5线 | 0.618线 | 阳线实体 | 涨幅 | 评分 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for h in hits:
        L.append(f"| {h['name']} | {z6(h['code'])} | {h['date']} | {h['bd_date']} | {h['b1_date']} | "
                 f"{h['sw_win']}({h['plat_len']}日) | {h['plat_conc']} | {h['gold_382']} | "
                 f"{h['gold_500']} | {h['gold_618']} | {h.get('body','')}% | {h['chg']}% | {h['score']} |")
    L.append("")
    L.append("> 黄金分割线基于入选日【反包阳线实体】(开~收)；0.5线=(开+收)/2为出局线，0.382/0.618仅供研究。")
    return "\n".join(L)


def build_ledger(hist, last_date, cal):
    L = [f"# 反包阳线·前期入选模型 · 台账 · {last_date}", ""]
    L.append(f"- 基准日：{last_date}")
    L.append(f"- 模型：{MODEL_DESC}")
    L.append(f"- 台账笔数：{len(hist)}")
    L.append("")
    L.append("| 名称 | 代码 | 入选日 | 入选价 | 破位日 | 0.5线 | 0.382线 | 0.618线 | 状态 | V3确认日 | 出局日 | 收益 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in sorted(hist, key=lambda x: x["date"]):
        st = {"tracking":"跟踪中","v3":"V3确认","out":"破线出局","done":"跟踪结束"}.get(r.get("status"), r.get("status"))
        ret = f"{r['ret']:+.2f}%" if isinstance(r.get("ret"), (int,float)) else "--"
        L.append(f"| {r['name']} | {z6(r['code'])} | {r['date']} | {r['buy_price']} | {r['bd_date']} | "
                 f"{r['gold_500']} | {r['gold_382']} | {r['gold_618']} | {st} | "
                 f"{r.get('v3_date') or '--'} | {r.get('out_date') or '--'} | {ret} |")
    return "\n".join(L)


# ---------------- 主流程 ----------------
def main():
    _ad = None
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "..", "common"))
        import adapter as _ad
        _ad.install("kaizhong", vars(lib))
        os.environ["KZ_OFFLINE"] = "1"
    except Exception:
        _ad = None
    print("[1/5] 探测最新交易日 ...")
    try:
        last_date = AS_OF or ((_ad.cm.load_meta() or {}).get("quote_date")
                              if _ad else lib.market_last_date())
        if not last_date:
            raise RuntimeError("共同前置层缺少 quote_date")
    except Exception as e:
        print(f"行情探测失败：{e}")
        sys.exit(1)
    print(f"      基准日 {last_date}（今日 {TODAY}）")
    if not AS_OF and last_date != TODAY:
        print("今日非交易日，无更新")
        return

    f10 = lib.load_f10()
    if SKIP_UPDATE:
        print("[2/5] 跳过行情抓取（使用现有缓存）")
        names = {}
    else:
        print("[2/5] 抓取全市场快照并增量更新日线 ...")
        names = refresh_data(last_date)

    print("[3/5] 全市场反包阳线入选扫描（合规剔除静默执行）...")
    hits, dates, px = scan_all(last_date, f10, names)
    cal = [d for d in dates if d <= last_date]
    if _ad:
        try:
            _ad.record("kaizhong", hits[:TOP])
        except Exception:
            pass
    print(f"      反包入选 {len(hits)} 只")

    hist = load_hist()
    print("[4/5] 入选去重 + 写台账 + 刷新5日跟踪（破线/V3）...")
    new_n, dup_n, v3_today = update_ledger(hist, hits, last_date, cal, dates, px)
    print(f"      新增入选 {new_n}，5日内重复去重 {dup_n}，台账共 {len(hist)} 笔，今日V3确认 {len(v3_today)} 只")

    print("[5/5] 生成报告与台账 ...")
    rp = os.path.join(lib.REPORT_DIR, f"ry_{last_date}.md")
    open(rp, "w", encoding="utf-8").write(build_report(hits, last_date, len(hist), dates, px))
    json.dump(hits[:TOP], open(os.path.join(lib.REPORT_DIR, f"ry_{last_date}.json"), "w",
                               encoding="utf-8"), ensure_ascii=False, indent=2)
    lp = os.path.join(lib.REPORT_DIR, f"反包台账_{last_date}.md")
    open(lp, "w", encoding="utf-8").write(build_ledger(hist, last_date, cal))

    state = load_state()
    track_lines, broken_n = build_tracking(hist, last_date, cal, state)
    push_md = build_push(hits, last_date, len(hist), v3_today, track_lines)
    sp = os.path.join(lib.REPORT_DIR, f"推送样例_ry_{last_date}.md")
    open(sp, "w", encoding="utf-8").write(push_md)
    print(f"      报告：{rp}\n      台账：{lp}\n      推送内容：{sp}")

    # ★推送触发：当日入选 或 当日V3确认 或 当日破线出局，任一发生即推送。
    #   V3/破线常发生在无新入选的日子（如凯众10-23），只盯 hits 会漏掉重点标识。
    if (hits or v3_today or broken_n) and not NO_PUSH:
        parts = [f"入选{len(hits)}只"]
        if v3_today: parts.append(f"V3确认{len(v3_today)}只")
        if broken_n: parts.append(f"破线出局{broken_n}只")
        lib.push_ftqq(f"反包阳线前期入选 {last_date}（{'、'.join(parts)}）", push_md)
        if broken_n:
            save_state(state)
            print(f"      [track] 本期 {broken_n} 只破线出局，名单已落盘")
    elif NO_PUSH:
        print("[push] KZ_NO_PUSH=1，离线验证模式，未推送（出局名单不落盘）")
    else:
        print("[push] 今日无反包入选，不推送（报告与台账照常落盘）")
    return hits


if __name__ == "__main__":
    main()
