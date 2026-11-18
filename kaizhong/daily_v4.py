# -*- coding: utf-8 -*-
"""
缩量二次突破模型（SVDB）· 每日自动化入口
（2026-11-18 起替换原凯众 v4「横盘突破提示」，台账/复盘/推送/合规运维框架不变）

选股内核：shrink_breakout.py
  横盘(25日) → 放量假突破(强度≥1.15) → 洗盘 5~45 日 → 缩量二次上攻(量比≤0.6)
  - 提示日收盘价买入（提示即买点、无等待期）；止损 = 买入价 ×0.95
  - 大盘空头趋势下不用
  - 首次突破去重：同一标的 5 个交易日内已提示过则不再新增推荐记录（仍标注「推荐第 N 天」）

推送规则（沿用 recommend.py 的归口口径）：
  - 每只标注「首日推荐」或「推荐第 N 天」+ 首推日
  - 第一部分：当日突破提示精简表（标的/推荐标注/首推日/当日涨幅/题材/信号质量）
  - 第二部分：只跟踪相对提示日买入价「仍优」的历史标的；破位标的说明一次后
    写入 push_track_state.json 出局名单，此后不再推送（2026-11-18 用户要求）
  - 完整明细与前期推荐复盘仍保留在报告 v4_<日期>.md 与台账 缩量台账_<日期>.md
  - 有突破提示才推送，无提示不推

重要边界：
  - 不改动 lib.py 的 SCHEME='breakout' / BUY_P / analyze / screen —— 生产库保持原样，可随时回退
  - 独立台账 shrink_history.json；旧 v4_history.json 保留供回退；
    push_track_state.json 仅控制推送展示，不碰台账归口与选股口径
  - 买入价 = 提示日收盘价；纪律止损 = 买入价 ×0.95；收益一律用真实 K 线重算
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
import shrink_breakout as sh      # 2026-11-18 替换：凯众v4 → 缩量二次突破模型
import recommend as rec

TOP = int(os.environ.get("KZ_TOP", "6"))
AS_OF = os.environ.get("KZ_AS_OF") or None
SKIP_UPDATE = os.environ.get("KZ_SKIP_UPDATE", "0") == "1"
NO_PUSH = os.environ.get("KZ_NO_PUSH", "0") == "1"
DEDUP_DAYS = int(os.environ.get("KZ_DEDUP_DAYS", "5"))
TODAY = datetime.now().strftime("%Y-%m-%d")
HORIZONS = (1, 3, 5, 10)
V4_HIST = os.path.join(lib.DATA, "shrink_history.json")   # 独立台账，原 v4_history.json 保留供回退
# 微信推送的「破位出局名单」：独立于台账，不碰归口口径。写入后不可逆，确保破位标的不再推送。
TRACK_STATE = os.path.join(lib.DATA, "push_track_state.json")
MODEL_DESC = "缩量二次突破：横盘→放量假突破(强度≥1.15)→洗盘→缩量二次上攻，提示即买点"


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


def load_track_state():
    """读取微信推送出局名单。文件不存在或损毁时返回空结构（首次上线 / 容错）。"""
    if os.path.exists(TRACK_STATE):
        try:
            d = json.load(open(TRACK_STATE, encoding="utf-8"))
            if isinstance(d, dict) and isinstance(d.get("out"), dict):
                return d
        except Exception:
            pass
    return {"out": {}}


def save_track_state(state):
    json.dump(state, open(TRACK_STATE, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


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


def build_tracking(hist, last_date, cal, state):
    """微信推送第二部分：只跟踪相对提示日买入价「还优」的历史推荐。

    2026-11-18 用户要求：
      - 只列「还优」：未触及纪律止损、且现价不低于买入价（ret >= 0）的标的；
        当日首推已在第一部分展示，此处不重复。
      - 破位 = 触及 -5% 纪律止损（台账 status=bad 且 note 标「止损」）
               或 收盘跌破买入价（ret < 0）。
      - 破位标的本期「说明一次」，随即写入不可逆出局名单 state['out']；
        之后既不在跟踪表、也不再出现在破位说明里 —— 不再推送。

    仅决定「推送里显示哪些历史标的」，不改台账、不选股、不改止损与去重口径。
    返回 (markdown 行列表, 本期新破位数)；state['out'] 被原地更新。
    """
    out = state["out"]
    # 按 6 位代码归口，取最早一条为首推（与 recommend 首推口径一致）
    first = {}
    for r in hist:
        c = z6(r["code"])
        if c not in first or str(r["date"]) < str(first[c]["date"]):
            first[c] = r

    alive, broken_new = [], []
    for c, r in sorted(first.items(), key=lambda kv: str(kv[1]["date"])):
        if c in out:                       # 已出局 → 不再推送（既不跟踪也不再说明）
            continue
        buy = float(r.get("buy_price") or 0)
        if buy <= 0:
            continue
        if str(r["date"]) == last_date:    # 当日首推已在第一部分，避免重复
            continue
        ret = r.get("ret")
        ret = None if ret is None else float(ret)
        status = str(r.get("status") or "")
        note = str(r.get("note") or "")
        hit_stop = (status == "bad" and "止损" in note)
        broken_price = (ret is not None and ret < 0)
        if hit_stop or broken_price:
            if hit_stop:
                reason = "触及 -5% 止损，按纪律离场"
            else:
                reason = (f"收盘跌破买入价（现价 {buy * (1 + ret / 100):.2f}，"
                          f"较买入 {ret:+.2f}%）")
            out[c] = {"date": last_date, "first": str(r["date"]),
                      "name": r.get("name", c), "reason": reason}
            broken_new.append((r.get("name", c), c, reason))
            continue
        cur = buy * (1 + (ret or 0) / 100)
        held = trade_gap(cal, str(r["date"]), last_date)
        alive.append((r.get("name", c), c, str(r["date"]), buy, cur, ret, held))

    L = []
    if alive:
        L.append("| 标的 | 首推日 | 买入价 | 现价 | 浮动 | 持有 |")
        L.append("|---|---|---|---|---|---|")
        for nm, c, d0, buy, cur, ret, held in alive:
            r_s = f"{ret:+.2f}%" if ret is not None else "--"
            L.append(f"| {nm}({c}) | {d0} | {buy:.2f} | {cur:.2f} | **{r_s}** | {held} 日 |")
        L.append("")
        L.append(f"> 仅跟踪相对提示日买入价**仍优**的 {len(alive)} 只；"
                 "破位标的说明一次后移出，后续不再推送。")
    else:
        L.append("- 暂无仍在跟踪且相对买入价仍优的标的。")
    if broken_new:
        L.append("")
        L.append(f"**本期破位离场（{len(broken_new)} 只，说明一次后不再推送）：**")
        L.append("")
        for nm, c, reason in broken_new:
            L.append(f"- {nm}({c})：{reason}")
    return L, len(broken_new)


NOTE = ("缩量二次上攻当日收盘价买入（提示即买点、无等待期）；"
        "务必确认大盘非空头趋势；止损按买入价 -5%。")


# ---------------- 信号质量标注（纯提示，不参与选股/排序/阈值） ----------------
# 依据：2026-11 对缩量二次突破模型的全A股回测（2016-2026，同口径对比）——
#   基线缩量模型(强度≥1.15)：10日胜率 63.2%、平均收益 +4.51%、盈亏比 1.61
#   同期凯众v4放量突破：       10日胜率 41.7%、平均收益 +0.29%（故 2026-11-18 替换）
#   单因子分组结论：
#     突破强度越高越优（强度≥1.15 是核心胜负手；原始宽松参数近随机）
#     量比越低越优（极缩量 <0.4：10日胜率 46.8%、收益 +1.91%；巨量 >2.0 转负）
#     大盘多头下最优（多头胜率 65.5%/+5.08%，空头仅 40.7% 且亏钱 → 空头坚决不用）
#     洗盘间隔 8~25 日优于 ≤7 日（案例凯众 11 日、仁信 14 日落在优质区间）
# 因此按「突破够猛 + 缩量够狠 + 洗盘充分」叠加星级，仅作研究参考。

def excess_tag(h):
    """返回 (星级标注, 依据说明, 排序权重)。仅用于展示与并列时的参考排序。

    分级依据上面回测的单因子结论（方向确定，具体数值为分组均值）：
      强度 ≥1.25 且 量比 ≤0.4 : ★★★ 强（强突破 + 极缩量，最优组合）
      强度 ≥1.15 且 量比 ≤0.6 : ★★ 中（核心参数达标）
      其余（边缘达标）        : ★ 弱（注意大盘环境，空头不用）
    """
    strength = float(h.get("strength") or 0)
    vr = float(h.get("vol_ratio") or 1)
    gap = int(h.get("gap") or 0)

    if strength >= 1.25 and vr <= 0.4:
        return "★★★ 强", f"强度 {strength:.3f}≥1.25 + 量比 {vr:.2f}≤0.4（强突破+极缩量最优组）", 3
    if strength >= 1.15 and vr <= 0.6:
        g = f"，洗盘 {gap} 日{'' if 8 <= gap <= 25 else '（间隔偏短/偏长）'}" if gap else ""
        return "★★ 中", f"强度 {strength:.3f}≥1.15 + 量比 {vr:.2f}≤0.6（核心参数达标）{g}", 2
    return "★ 弱", f"强度 {strength:.3f}/量比 {vr:.2f} 边缘达标，务必确认大盘非空头", 1


def excess_note():
    return ("信号质量标注（研究参考，不影响选股）：缩量二次突破模型全A股回测显示，"
            "**突破强度 ≥1.15 是核心胜负手**（10日胜率 63.2%/收益 +4.51%，vs 凯众v4放量突破 41.7%/+0.29%）；"
            "量比越低越优（极缩量 <0.4 为 +1.91%），强度≥1.25 且量比≤0.4 为最优组合。"
            "**大盘空头趋势下胜率仅 40.7% 且亏损，空头市场坚决不用**；洗盘 8~25 日优于 ≤7 日。"
            "本列仅作研究参考，不改变选股与排序。")



# ---------------- 输出 ----------------
def build_push(hits, last_date, tracked_n, fmap, cal, rv, new_n, dup_n, track_lines):
    L = [f"# 缩量二次突破 · {last_date}", ""]
    L.append(f"> 模型：{MODEL_DESC}；同一标的只记一次「首日推荐」，"
             "之后再提示标「推荐第 N 天」。")
    L.append("")
    L.append(f"## 一、今日突破提示（全市场命中 {len(hits)} 只：台账新增 {new_n} 笔、"
             f"5 日内重复去重 {dup_n} 笔；取评分前 {min(TOP, len(hits))} 只推送）")
    L.append("")
    if hits:
        # 推送表单按用户要求精简：只保留 推荐标注 / 首推日 / 当日涨幅 / 题材 / 信号质量。
        # 触发价、所需量、止损、收盘、量比等行情数据由用户自行在同花顺查看，
        # 完整明细仍保留在报告与台账（build_report / build_ledger）中。
        L.append("| 标的 | 推荐标注 | 首推日 | 当日涨幅 | 题材 | 信号质量 |")
        L.append("|---|---|---|---|---|---|")
        for h in hits[:TOP]:
            tg, fd = rec.tag_of(h["code"], last_date, fmap, cal)
            theme = (h.get("concepts", "").split(",")[0] if h.get("concepts") else "") \
                    or h.get("industry", "") or "—"
            star, _, _ = excess_tag(h)
            L.append(f"| {h['name']}({z6(h['code'])}) | **{tg}** | {fd} | {h['chg']}% | "
                     f"{theme} | {star} |")
        L.append("")
        L.append("> " + excess_note())
    else:
        L.append("- 今日无缩量二次突破信号，不推送。")
    L.append("")
    L.append("## 二、在跟踪标的（相对提示日仍优）")
    L.append("")
    L += track_lines
    L.append("")
    L.append(f"> 台账共跟踪 {tracked_n} 笔记录。{NOTE}")
    return "\n".join(L)


def build_report(hits, last_date, tracked_n, fmap, cal, rv, new_n, dup_n, candidate_n):
    L = [f"# 缩量二次突破模型（SVDB）· {last_date}", ""]
    L.append(f"> 选股模型：{MODEL_DESC}")
    L.append("> 形态：横盘(25日) → 放量假突破(强度≥1.15) → 洗盘5~45日 → 缩量二次上攻(量比≤0.6)；"
             "提示日收盘买入。")
    L.append(f"> 覆盖本地日线 {candidate_n} 只，命中 **{len(hits)}** 只"
             f"（台账新增 {new_n} 笔、5 日内重复去重 {dup_n} 笔）；取评分前 {min(TOP, len(hits))} 只推送。")
    L.append("")
    L.append("## 一、今日缩量二次突破提示")
    L.append("")
    L.append("| 名称 | 代码 | 推荐标注 | 首推日 | 首突日 | 突破强度 | 量比 | 洗盘间隔 | 平台上沿 | 止损 | 收盘 | 涨幅 | 评分 | 信号质量 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for h in hits:
        tg, fd = rec.tag_of(h["code"], last_date, fmap, cal)
        stop = round(float(h["last_close"]) * (1 - sh.STANCE["stop_loss"]), 2)
        star, why_x, _ = excess_tag(h)
        L.append(f"| {h['name']} | {z6(h['code'])} | {tg} | {fd} | {h['b1_date']} | "
                 f"{h['strength']} | {h['vol_ratio']} | {h['gap']}日 | "
                 f"{h['platform_top']} | {stop} | {h['last_close']} | {h['chg']}% | "
                 f"{h['score']} | {star}（{why_x}） |")
    L.append("")
    L.append("> " + excess_note())
    L.append("")
    L.append("## 二、前期推荐复盘")
    L.append("")
    L += review_lines(rv)
    L.append("")
    L.append(f"> {NOTE}")
    return "\n".join(L)


def build_ledger(hits, hist, last_date, fmap, cal, rv, candidate_n):
    """资料库台账《缩量台账_<日期>.md》—— 字段齐全、真实涨跌幅，避免执行端编造。"""
    L = [f"# 缩量二次突破模型 · 每日推荐台账 · {last_date}", ""]
    L.append(f"- 基准日：{last_date}")
    L.append(f"- 模型名：缩量二次突破（SVDB，{MODEL_DESC}）")
    L.append(f"- 候选池数量：{candidate_n}")
    L.append("")
    L.append(f"## 今日最优 {min(TOP, len(hits))} 只")
    L.append("")
    L.append("| 名称 | 6位代码 | 推荐标注 | 首推日 | 题材 | 一句话原因 | 平台上沿 | 止损 | 当日收盘 | 当日涨跌幅 | 信号质量 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for h in hits[:TOP]:
        tg, fd = rec.tag_of(h["code"], last_date, fmap, cal)
        theme = (h.get("concepts", "").split(",")[0] if h.get("concepts") else "") \
                or h.get("industry", "") or "—"
        stop = round(float(h["last_close"]) * (1 - sh.STANCE["stop_loss"]), 2)
        why = (f"横盘 {h['sw_win']} → {h['b1_date']} 放量假突破（强度 {h['strength']}）"
               f"→ 洗盘 {h['gap']} 日 → 缩量二次上攻（量比 {h['vol_ratio']}）")
        star, why_x, _ = excess_tag(h)
        L.append(f"| {h['name']} | {z6(h['code'])} | {tg} | {fd} | {theme} | {why} | "
                 f"{h['platform_top']} | {stop} | "
                 f"{h['last_close']} | {h['chg']:+.2f}% | {star}（{why_x}） |")
    L.append("")
    L.append("> " + excess_note())
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
    # 共同前置层接入（强制）：patch lib 的列表来源到共享样本池（已剔除 ST/立信）。
    # 凯众 K 线保留自有新浪增量缓存，但出口 gate 仍会对最终推荐再做一次剔除兜底。
    _ad = None
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "..", "common"))
        import adapter as _ad
        _ad.install("kaizhong", vars(lib))
    except Exception:
        _ad = None
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

    print("[3/5] 全市场缩量二次突破扫描（合规剔除静默执行）...")
    hits, dates, px, lo = scan_and_matrix(last_date, f10, names)
    cal = [d for d in dates if d <= last_date]
    # 共同前置层：写统一推荐台账（出口 gate 强制再剔一次 ST/立信）
    if _ad:
        try:
            _ad.record("kaizhong", hits[:TOP])
        except Exception:
            pass
    print(f"      命中 {len(hits)} 只")

    hist = load_hist()
    if not hist:
        # 2026-11-18：缩量模型独立台账首次上线，从零开始积累真实信号。
        # 不再调用 bootstrap_from_backtest —— 那份是凯众 v4 回测 CSV，口径与
        # 缩量模型不同，混入会污染「前期推荐复盘」统计。原函数保留仅作研究参考。
        print("      缩量模型台账首次上线：从零积累，不导入 v4 历史（口径不同）")
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
    lp = os.path.join(lib.REPORT_DIR, f"缩量台账_{last_date}.md")
    open(lp, "w", encoding="utf-8").write(
        build_ledger(hits, hist, last_date, fmap, cal, rv, len(px.columns)))
    print(f"      报告：{rp}\n      台账：{lp}")

    # 微信推送第二部分：只跟踪仍优的历史标的；破位说明一次并写入不可逆出局名单。
    # 出局名单只在「本次确实发出推送」时落盘，避免离线/无信号日把说明名额消耗掉。
    state = load_track_state()
    track_lines, broken_n = build_tracking(hist, last_date, cal, state)
    push_md = build_push(hits, last_date, len(hist), fmap, cal, rv,
                         new_n, dup_n, track_lines)
    sp = os.path.join(lib.REPORT_DIR, f"推送样例_v4_{last_date}.md")
    open(sp, "w", encoding="utf-8").write(push_md)
    print(f"      推送内容：{sp}")

    if hits and not NO_PUSH:
        lib.push_ftqq(f"缩量二次突破 {last_date}（{len(hits)}只）", push_md)
        if broken_n:
            save_track_state(state)
            print(f"      [track] 本期 {broken_n} 只破位离场，出局名单已落盘（不再推送）")
    elif NO_PUSH:
        print("[push] KZ_NO_PUSH=1，离线验证模式，未推送（出局名单不落盘）")
    else:
        print("[push] 今日无突破提示，不推送（报告与台账照常落盘）")
    return hits


if __name__ == "__main__":
    main()
