# -*- coding: utf-8 -*-
"""
凯众模型 v4（横盘突破提示）· 每日自动化入口

选股模型：stance_hint.py  v4「横盘约两个月 → 站稳 MA5 → 放量突破当天提示」
  - 不考虑试盘因素；提示即买点（收盘 ≥ 触发价 且 成交量 ≥ 所需量 同时满足）
  - 硬前置：提示日前 40 个交易日收盘 max/min≤1.20、std/mean≤0.045，前后各 20 日分别横盘
  - 首次突破去重：同一标的 5 个交易日内已提示过则不再新增推荐记录（仍标注「推荐第 N 天」）

推送规则（沿用 recommend.py 的归口口径）：
  - 每只标注「首日推荐」或「推荐第 N 天」+ 首推日
  - 附「前期推荐复盘」：1/3/5/10 日平均涨幅、上涨胜率、跑赢率、超额、止损率、收益明细
  - 有突破提示才推送，无提示不推（与既有「只在突破当天推送」偏好一致）

重要边界：
  - 不改动 lib.py 的 SCHEME='breakout' / BUY_P / analyze / screen —— 生产库保持原样，可随时回退
  - 独立台账 data/v4_history.json，不污染生产 history.json
  - 买入价 = 提示日收盘价（上一轮回测论证的唯一可执行口径）；纪律止损 = 买入价 ×0.95
  - 首次上线从 v4 回测明细导入历史提示作为复盘基线，收益一律用真实 K 线重算
  - 合规剔除静默执行，不在任何汇报中提及

用法：
  KZ_TOP=6 python3 daily_v4.py                       # 正式：抓快照→更新缓存→扫描→推送
  KZ_SKIP_UPDATE=1 python3 daily_v4.py               # 复跑：用现有缓存
  KZ_AS_OF=2026-11-06 KZ_SKIP_UPDATE=1 KZ_NO_PUSH=1 python3 daily_v4.py   # 离线验证
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
import stance_hint as sh
import recommend as rec

TOP = int(os.environ.get("KZ_TOP", "6"))
AS_OF = os.environ.get("KZ_AS_OF") or None
SKIP_UPDATE = os.environ.get("KZ_SKIP_UPDATE", "0") == "1"
NO_PUSH = os.environ.get("KZ_NO_PUSH", "0") == "1"
DEDUP_DAYS = int(os.environ.get("KZ_DEDUP_DAYS", "5"))
TODAY = datetime.now().strftime("%Y-%m-%d")
HORIZONS = (1, 3, 5, 10)
V4_HIST = os.path.join(lib.DATA, "v4_history.json")
MODEL_DESC = "横盘约两个月 → 站稳 MA5 → 放量突破当天提示（不考虑试盘因素）"


def z6(c):
    return str(c).zfill(6)


# ---------------- 数据 ----------------
def refresh_data(last_date):
    """抓全市场快照 + 增量更新日线缓存，返回 code->name。"""
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


def scan_and_matrix(last_date, f10, names):
    """一次遍历全市场日线：同时做 v4 扫描、合规剔除，并收集 close/low 矩阵供复盘算基准。"""
    hits, series = [], {}
    for f in sorted(glob.glob(os.path.join(lib.KLINE_DIR, "*.csv"))):
        code = os.path.basename(f)[:6]
        try:
            df = pd.read_csv(f)
        except Exception:
            continue
        df["date"] = df["date"].astype(str)
        df = df[df["date"] <= last_date]          # 复盘矩阵一并截断，杜绝未来函数
        if df.empty:
            continue
        try:
            series[code] = df.set_index("date")[["close", "low"]]
        except Exception:
            pass
        row = f10.get(code, {})
        name = names.get(code) or row.get("name") or code
        if lib.blocked_by_rule(name, code, row):   # 合规剔除静默执行
            continue
        try:
            r = sh.scan_one(code, df, as_of=last_date)
        except Exception:
            continue
        if r:
            r.update(name=name,
                     industry=row.get("industry", ""),
                     concepts=row.get("concepts", ""))
            hits.append(r)
    hits.sort(key=lambda x: -x["score"])
    dates = sorted(set().union(*[set(s.index) for s in series.values()])) if series else []
    px = pd.DataFrame({c: s["close"] for c, s in series.items()}).reindex(dates) if series else pd.DataFrame()
    lo = pd.DataFrame({c: s["low"] for c, s in series.items()}).reindex(dates) if series else pd.DataFrame()
    return hits, dates, px, lo


# ---------------- 台账 ----------------
def load_hist():
    if os.path.exists(V4_HIST):
        try:
            return json.load(open(V4_HIST, encoding="utf-8"))
        except Exception:
            return []
    return []


def save_hist(hist):
    json.dump(hist, open(V4_HIST, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def trade_gap(cal, d0, d1):
    return sum(1 for d in cal if d0 < d <= d1)


def bootstrap_from_backtest(last_date, cal):
    """首次上线：从扩展窗口回测明细导入历史 v4 提示，作为「前期推荐」复盘基线。

    只导入信号身份与形态字段；收益、状态、1/3/5/10 日涨幅一律由后续流程
    基于真实 K 线重算，避免与回测脚本的买点口径混淆。
    """
    # 只认「日期_日期.csv」主明细，排除 _no_sideways / _strict / _v3 等备份档；
    # 并在多个窗口里取样本最全的一份（行数最大），避免误取 10-24 短窗口或 v3 备份。
    cands = [p for p in glob.glob(os.path.join(lib.REPORT_DIR, "backtest_stance_*.csv"))
             if re.search(r"backtest_stance_\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}\.csv$", p)]
    best = None
    for p in cands:
        try:
            d = pd.read_csv(p, encoding="utf-8-sig", dtype={"code": str})
        except Exception:
            continue
        if "D" not in d.columns or "last_close" not in d.columns:
            continue
        if best is None or len(d) > len(best[1]):
            best = (p, d)
    if best is None:
        return []
    df = best[1]
    print(f"      复盘基线源：{os.path.basename(best[0])}（{len(df)} 条信号）")
    df["code"] = df["code"].astype(str).str.zfill(6)
    hist, rec_by_code = [], {}
    lo_date, hi_date = None, None
    for c, g in df.sort_values("D").groupby("code"):
        last_hint = None
        for _, s in g.iterrows():
            D = str(s["D"])
            if D >= last_date:      # 只导入「今天之前」的历史，今日信号由实盘扫描产生
                continue
            lo_date = D if lo_date is None or D < lo_date else lo_date
            hi_date = D if hi_date is None or D > hi_date else hi_date
            buy = float(s["last_close"])
            if last_hint and 0 <= trade_gap(cal, last_hint, D) <= DEDUP_DAYS:
                target = rec_by_code[c][-1]
                if D not in target["hit_dates"]:
                    target["hit_dates"].append(D)
                last_hint = D
                continue
            item = dict(
                code=c, name=str(s.get("name") or c), date=D, symbol="",
                trigger=float(s["trigger"]), req_vol=int(float(s["req_vol"])),
                req_vol_wan=float(s["req_vol_wan"]),
                model_stop=float(s["stop"]), stop=round(buy * (1 - sh.STANCE["stop_loss"]), 2),
                buy_price=round(buy, 2), buy_date=D,
                platform_top=float(s["platform_top"]), sw_win=str(s["sw_win"]),
                sw_range=float(s["sw_range"]), sw_std=float(s["sw_std"]),
                vol_x=float(s["vol_x"]), chg=float(s["chg"]), last_close=buy,
                score=float(s["score"]), concepts="", industry="",
                status="triggered", ret=None, note="", hit_dates=[D],
                last_check=last_date, source="backtest_bootstrap",
            )
            hist.append(item)
            rec_by_code.setdefault(c, []).append(item)
            last_hint = D
    if hist:
        hist.sort(key=lambda x: x["date"])
        print(f"      复盘基线：{lo_date} ~ {hi_date}，共 {len(hist)} 只（已按首次突破去重）")
    return hist


def update_ledger(hist, hits, last_date, cal, dates, px, lo):
    """首次突破去重 + 新增推荐 + 刷新全部记录的持仓状态。"""
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
        near = [d for d in set(hint_dates)
                if 0 <= trade_gap(cal, d, last_date) <= DEDUP_DAYS]
        if near:
            # 去重：不新增记录，仅在最近一条上追加命中日（用于「推荐第 N 天」与累计次数）
            target = max(recs, key=lambda x: x["date"])
            target.setdefault("hit_dates", [target["date"]])
            if last_date not in target["hit_dates"]:
                target["hit_dates"].append(last_date)
            dup_n += 1
            continue

        buy_px = float(h["last_close"])           # 唯一可执行口径：提示日收盘价
        stop = round(buy_px * (1 - sh.STANCE["stop_loss"]), 2)
        item = dict(
            code=c, name=h["name"], date=last_date, symbol="",
            trigger=h["trigger"], req_vol=h["req_vol"], req_vol_wan=h["req_vol_wan"],
            model_stop=h["stop"], stop=stop,
            buy_price=round(buy_px, 2), buy_date=last_date,
            platform_top=h["platform_top"], sw_win=h["sw_win"],
            sw_range=h["sw_range"], sw_std=h["sw_std"],
            vol_x=h["vol_x"], chg=h["chg"], last_close=h["last_close"],
            score=h["score"], concepts=h.get("concepts", ""), industry=h.get("industry", ""),
            status="triggered", ret=None, note="提示日收盘买入，持有观察",
            hit_dates=[last_date], last_check=last_date,
        )
        hist.append(item)
        by_code.setdefault(c, []).append(item)
        new_n += 1

    # 刷新持仓状态（止损从买入次日起算）
    idx = {d: i for i, d in enumerate(dates)}
    j_end = idx.get(last_date, len(dates) - 1)
    for r in hist:
        c, j0 = z6(r["code"]), idx.get(r["date"])
        if j0 is None or c not in px.columns:
            continue
        buy = float(r["buy_price"])
        seg = lo[c].iloc[j0 + 1: min(j0 + max(HORIZONS) + 1, len(dates))]
        hit_stop = bool((seg <= r["stop"]).any()) if len(seg) else False
        cur = px[c].iloc[min(j_end, len(dates) - 1)]
        if pd.isna(cur) or buy <= 0:
            continue
        ret = round((cur / buy - 1) * 100, 2)
        if hit_stop:
            r.update(status="bad", ret=-5.0, note="触及 -5% 止损离场")
        else:
            st = "good" if ret >= 8 else ("mid" if ret >= 0 else "bad")
            r.update(status=st, ret=ret,
                     note=f"提示日收盘买入 {buy:.2f}，现价 {cur:.2f}")
        r["last_check"] = last_date
    save_hist(hist)
    return new_n, dup_n


# ---------------- 复盘 ----------------
def v4_review(hist, last_date, fmap, cal, dates, px, lo):
    """前期推荐复盘：1/3/5/10 日涨幅、上涨胜率、跑赢率、超额、止损率。"""
    idx = {d: i for i, d in enumerate(dates)}
    first = {}
    for r in hist:
        c = z6(r["code"])
        if c not in first or str(r["date"]) < str(first[c]["date"]):
            first[c] = r

    # 全市场等权基准：只算首推日对应的 (j, N)，避免遍历全部交易日
    jset = sorted({idx[r["date"]] for r in first.values() if r["date"] in idx})
    BENCH = {}
    for N in HORIZONS:
        for j in jset:
            if j + N >= len(dates):
                continue
            ratio = (px.iloc[j + N] / px.iloc[j]).replace([np.inf, -np.inf], np.nan).dropna()
            BENCH[(dates[j], N)] = float(ratio.mean() - 1) * 100 if len(ratio) > 200 else None

    rows = []
    for c, r in first.items():
        j0 = idx.get(r["date"])
        if j0 is None or c not in px.columns:
            continue
        buy = float(r["buy_price"])
        if buy <= 0:
            continue
        item = dict(name=r["name"], code=c, date=r["date"], buy=buy)
        for N in HORIZONS:
            item[f"ret{N}"] = None
            item[f"bench{N}"] = BENCH.get((r["date"], N))
            if j0 + N < len(dates):
                cN = px.at[dates[j0 + N], c]
                if not pd.isna(cN):
                    item[f"ret{N}"] = round((cN / buy - 1) * 100, 2)
        seg = lo[c].iloc[j0 + 1: min(j0 + max(HORIZONS) + 1, len(dates))]
        item["hit_stop"] = bool((seg <= r["stop"]).any()) if len(seg) else False
        rows.append(item)

    stat = {}
    for N in HORIZONS:
        vals = [x[f"ret{N}"] for x in rows if x[f"ret{N}"] is not None]
        pairs = [(x[f"ret{N}"], x[f"bench{N}"]) for x in rows
                 if x[f"ret{N}"] is not None and x[f"bench{N}"] is not None]
        stat[N] = dict(
            n=len(vals),
            avg=round(float(np.mean(vals)), 2) if vals else None,
            win=round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1) if vals else None,
            beat=round(sum(1 for v, b in pairs if v > b) / len(pairs) * 100, 1) if pairs else None,
            bench=round(float(np.mean([b for _, b in pairs])), 2) if pairs else None,
        )

    # 收益明细：优先按 5 日，其次 10 日，其次 1 日
    def keyf(x):
        for N in (5, 10, 3, 1):
            if x[f"ret{N}"] is not None:
                return x[f"ret{N}"]
        return -999
    ranked = sorted(rows, key=keyf, reverse=True)
    ret_all = [(x["name"], x["code"], keyf(x)) for x in ranked[:6] if keyf(x) != -999]

    # 重复提示归口
    cnt = {}
    for r in hist:
        c = z6(r["code"])
        dates_hit = r.get("hit_dates") or [r["date"]]
        cnt[c] = cnt.get(c, 0) + len(set(dates_hit))
    dups = []
    for c, n in sorted(cnt.items(), key=lambda x: -x[1]):
        if n < 2 or c not in fmap:
            continue
        nm = next((r["name"] for r in hist if z6(r["code"]) == c), c)
        tg, _ = rec.tag_of(c, last_date, fmap, cal)
        dups.append(dict(code=c, name=nm, times=n, first=fmap[c], tag=tg))
        if len(dups) >= 3:
            break

    stop_n = sum(1 for x in rows if x["hit_stop"])
    return dict(total_codes=len(first), total_records=len(hist),
                stat=stat, ret_all=ret_all, dups=dups,
                stop_n=stop_n, stop_rate=round(stop_n / len(rows) * 100, 1) if rows else 0,
                rows_n=len(rows))


def review_lines(rv):
    L = [f"- 累计推荐 **{rv['total_codes']}** 只标的（台账 {rv['total_records']} 笔记录，"
         f"重复提示已按代码归口去重，不再计为新推荐）"]
    if not rv["rows_n"]:
        L.append("- 暂无可评估的前期推荐样本，自本期起逐日累积。")
        return L
    L.append("")
    L.append("| 持有期 | 有效样本 | 平均涨幅 | 上涨胜率 | 同期市场 | 超额 | 跑赢率 |")
    L.append("|---|---|---|---|---|---|---|")
    for N in HORIZONS:
        s = rv["stat"][N]
        if not s["n"]:
            L.append(f"| {N} 日 | 0 | -- | -- | -- | -- | -- |")
            continue
        ex = round(s["avg"] - s["bench"], 2) if (s["avg"] is not None and s["bench"] is not None) else None
        avg_s = f"{s['avg']:+.2f}%" if s["avg"] is not None else "--"
        win_s = f"{s['win']:.1f}%" if s["win"] is not None else "--"
        bench_s = f"{s['bench']:+.2f}%" if s["bench"] is not None else "--"
        ex_s = f"{ex:+.2f}pp" if ex is not None else "--"
        beat_s = f"{s['beat']:.1f}%" if s["beat"] is not None else "--"
        L.append(f"| {N} 日 | {s['n']} | {avg_s} | {win_s} | {bench_s} | {ex_s} | {beat_s} |")
    L.append("")
    L.append(f"- 买入价按**提示日收盘价**计（唯一可执行口径）；纪律止损为买入价 -5%，"
             f"10 日窗口内触及止损 **{rv['stop_n']}/{rv['rows_n']}（{rv['stop_rate']}%）**")
    if rv["ret_all"]:
        L.append("- 收益明细（仅列涨幅前 6，不代表整体，整体见上表）："
                 + "、".join(f"{n}({c}) {v:+.2f}%" for n, c, v in rv["ret_all"]))
    if rv["dups"]:
        L.append("- 重复提示归口："
                 + "；".join(f"{d['name']}({d['code']}) {d['first']} 首推·累计 {d['times']} 次→{d['tag']}"
                            for d in rv["dups"]))
    return L


NOTE = ("收盘 ≥ 触发价 **且** 成交量 ≥ 所需量同时满足才是买点；"
        "回踩不买；止损按买入价 -5%；提示即买点、无等待期。")


# ---------------- 输出 ----------------
def build_push(hits, last_date, tracked_n, fmap, cal, rv, new_n, dup_n):
    L = [f"# 凯众横盘突破 · {last_date}", ""]
    L.append(f"> 模型：{MODEL_DESC}；**提示即买点**，同一标的只记一次「首日推荐」，"
             "之后再提示标「推荐第 N 天」。")
    L.append("")
    L.append(f"## 一、今日突破提示（全市场命中 {len(hits)} 只：台账新增 {new_n} 笔、"
             f"5 日内重复去重 {dup_n} 笔；取评分前 {min(TOP, len(hits))} 只推送）")
    L.append("")
    if hits:
        L.append("| 标的 | 推荐标注 | 首推日 | 触发价 | 所需量(万手) | 止损 | 收盘 | 涨幅 | 量比 | 题材 |")
        L.append("|---|---|---|---|---|---|---|---|---|---|")
        for h in hits[:TOP]:
            tg, fd = rec.tag_of(h["code"], last_date, fmap, cal)
            theme = (h.get("concepts", "").split(",")[0] if h.get("concepts") else "") \
                    or h.get("industry", "") or "—"
            stop = round(float(h["last_close"]) * (1 - sh.STANCE["stop_loss"]), 2)
            L.append(f"| {h['name']}({z6(h['code'])}) | **{tg}** | {fd} | {h['trigger']} | "
                     f"{h['req_vol_wan']} | {stop} | {h['last_close']} | {h['chg']}% | "
                     f"{h['vol_x']} | {theme} |")
    else:
        L.append("- 今日无样本放量突破，不推送。")
    L.append("")
    L.append("## 二、前期推荐复盘")
    L.append("")
    L += review_lines(rv)
    L.append("")
    L.append(f"> 台账共跟踪 {tracked_n} 笔记录。{NOTE}")
    return "\n".join(L)


def build_report(hits, last_date, tracked_n, fmap, cal, rv, new_n, dup_n, candidate_n):
    L = [f"# 凯众形态相似股模型 v4 · {last_date}", ""]
    L.append(f"> 选股模型：{MODEL_DESC}")
    L.append("> 硬前置：提示日前 40 个交易日收盘价横盘（max/min≤1.20、std/mean≤0.045），"
             "前后各 20 日分别横盘；仅用收盘价，避免影线干扰。")
    L.append(f"> 覆盖本地日线 {candidate_n} 只，命中 **{len(hits)}** 只"
             f"（台账新增 {new_n} 笔、5 日内重复去重 {dup_n} 笔）；取评分前 {min(TOP, len(hits))} 只推送。")
    L.append("")
    L.append("## 一、今日突破提示")
    L.append("")
    L.append("| 名称 | 代码 | 推荐标注 | 首推日 | 横盘区间 | 收盘振幅/std | 平台上沿 | 触发价 | 所需量(万手) | 止损 | 收盘 | 涨幅 | 量比 | 评分 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for h in hits:
        tg, fd = rec.tag_of(h["code"], last_date, fmap, cal)
        stop = round(float(h["last_close"]) * (1 - sh.STANCE["stop_loss"]), 2)
        L.append(f"| {h['name']} | {z6(h['code'])} | {tg} | {fd} | {h['sw_win']} | "
                 f"{h['sw_range']}/{h['sw_std']} | {h['platform_top']} | {h['trigger']} | "
                 f"{h['req_vol_wan']} | {stop} | {h['last_close']} | {h['chg']}% | "
                 f"{h['vol_x']} | {h['score']} |")
    L.append("")
    L.append("## 二、前期推荐复盘")
    L.append("")
    L += review_lines(rv)
    L.append("")
    L.append(f"> {NOTE}")
    return "\n".join(L)


def build_ledger(hits, hist, last_date, fmap, cal, rv, candidate_n):
    """资料库台账《凯众台账_<日期>.md》—— 字段齐全、真实涨跌幅，避免执行端编造。"""
    L = [f"# 凯众形态相似股模型 · 每日推荐台账 · {last_date}", ""]
    L.append(f"- 基准日：{last_date}")
    L.append(f"- 模型名：凯众形态相似股 v4（{MODEL_DESC}）")
    L.append(f"- 候选池数量：{candidate_n}")
    L.append("")
    L.append(f"## 今日最优 {min(TOP, len(hits))} 只")
    L.append("")
    L.append("| 名称 | 6位代码 | 推荐标注 | 首推日 | 题材 | 一句话原因 | 触发价 | 所需量(万手) | 止损 | 当日收盘 | 当日涨跌幅 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for h in hits[:TOP]:
        tg, fd = rec.tag_of(h["code"], last_date, fmap, cal)
        theme = (h.get("concepts", "").split(",")[0] if h.get("concepts") else "") \
                or h.get("industry", "") or "—"
        stop = round(float(h["last_close"]) * (1 - sh.STANCE["stop_loss"]), 2)
        why = (f"横盘 {h['sw_win']}（收盘比 {h['sw_range']}/std {h['sw_std']}），"
               f"放量 {h['vol_x']} 倍突破平台 {h['platform_top']}")
        L.append(f"| {h['name']} | {z6(h['code'])} | {tg} | {fd} | {theme} | {why} | "
                 f"{h['trigger']} | {h['req_vol_wan']} | {stop} | "
                 f"{h['last_close']} | {h['chg']:+.2f}% |")
    L.append("")
    L.append("## 在跟踪清单")
    L.append("")
    latest = rec.latest_per_code(hist)
    # v4「提示即买点、无等待期」，只列最近 10 个交易日内提示且未止损的在跟踪样本
    holding = [r for r in latest.values()
               if trade_gap(cal, r["date"], last_date) <= max(HORIZONS)
               and r.get("status") in ("triggered", "good", "mid")]
    if holding:
        L.append("| 名称 | 代码 | 推荐标注 | 首推日 | 买入价 | 止损 | 浮动收益 | 状态 |")
        L.append("|---|---|---|---|---|---|---|---|")
        for r in holding[:30]:
            tg, fd = rec.tag_of(r["code"], last_date, fmap, cal)
            st = rec.STATUS_CN.get(r.get("status"), r.get("status"))
            ret_s = f"{r['ret']:+.2f}%" if isinstance(r.get("ret"), (int, float)) else "--"
            L.append(f"| {r['name']} | {z6(r['code'])} | {tg} | {fd} | {r['buy_price']} | "
                     f"{r['stop']} | {ret_s} | {st} |")
        if len(holding) > 30:
            L.append(f"\n（其余 {len(holding) - 30} 只从略）")
    else:
        L.append("- 无在跟踪样本。")
    L.append("")
    L.append("## 浮亏 / 止损单")
    L.append("")
    bad = [r for r in latest.values() if r.get("status") == "bad"]
    if bad:
        for r in bad[:15]:
            L.append(f"- {r['name']}({z6(r['code'])}) {r.get('ret', '--')}%：{r.get('note', '')}")
    else:
        L.append("- 无。")
    L.append("")
    L.append("## 前期推荐复盘")
    L.append("")
    L += review_lines(rv)
    L.append("")
    L.append("> 本台账供次日「美股行业异动雷达」读取，用于跨市场双模型匹配。")
    return "\n".join(L)


# ---------------- 主流程 ----------------
def main():
    print("[1/5] 探测最新交易日 ...")
    try:
        last_date = AS_OF or lib.market_last_date()
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

    print("[3/5] 全市场 v4 扫描（合规剔除静默执行）...")
    hits, dates, px, lo = scan_and_matrix(last_date, f10, names)
    cal = [d for d in dates if d <= last_date]
    print(f"      命中 {len(hits)} 只")

    hist = load_hist()
    if not hist:
        print("      首次上线：从 v4 回测明细导入历史提示，作为「前期推荐」复盘基线")
        hist = bootstrap_from_backtest(last_date, cal)
    fmap = rec.first_rec_map(hist)
    print("[4/5] 首次突破去重 + 写台账 + 刷新持仓状态 ...")
    new_n, dup_n = update_ledger(hist, hits, last_date, cal, dates, px, lo)
    fmap = rec.first_rec_map(hist)
    print(f"      新增 {new_n} 只，重复提示去重 {dup_n} 只，台账共 {len(hist)} 笔")

    rv = v4_review(hist, last_date, fmap, cal, dates, px, lo)

    print("[5/5] 生成报告与台账 ...")
    rp = os.path.join(lib.REPORT_DIR, f"v4_{last_date}.md")
    open(rp, "w", encoding="utf-8").write(
        build_report(hits, last_date, len(hist), fmap, cal, rv,
                     new_n, dup_n, len(px.columns)))
    json.dump(hits[:TOP], open(os.path.join(lib.REPORT_DIR, f"v4_{last_date}.json"), "w",
                               encoding="utf-8"), ensure_ascii=False, indent=2)
    lp = os.path.join(lib.REPORT_DIR, f"凯众台账_{last_date}.md")
    open(lp, "w", encoding="utf-8").write(
        build_ledger(hits, hist, last_date, fmap, cal, rv, len(px.columns)))
    print(f"      报告：{rp}\n      台账：{lp}")

    push_md = build_push(hits, last_date, len(hist), fmap, cal, rv, new_n, dup_n)
    sp = os.path.join(lib.REPORT_DIR, f"推送样例_v4_{last_date}.md")
    open(sp, "w", encoding="utf-8").write(push_md)
    print(f"      推送内容：{sp}")

    if hits and not NO_PUSH:
        lib.push_ftqq(f"凯众横盘突破 {last_date}（{len(hits)}只）", push_md)
    elif NO_PUSH:
        print("[push] KZ_NO_PUSH=1，离线验证模式，未推送")
    else:
        print("[push] 今日无突破提示，不推送（报告与台账照常落盘）")
    return hits


if __name__ == "__main__":
    main()
