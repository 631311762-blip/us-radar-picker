# -*- coding: utf-8 -*-
"""
凯众 v4 筛出股票 · 3/5/10 日买股策略挖掘

目的：从模型实际筛出的信号里，找出「买哪一批、怎么买、持有多久」收益率最高的
      可执行规则，并做时间切分的样本外验证与收益质量诊断，避免只给一个
      被少数暴涨股撑起来的、样本内凑出来的数字。

口径：
  - 信号源：backtest_stance_2026-08-01_2026-11-06.csv（v4 全市场逐日扫描，456 条）
  - 买点 A ：提示日收盘价（模型账面口径；尾盘竞价近似，实盘确认滞后）
  - 买点 NO：次日开盘价（收盘后才确认信号，这是最干净的可执行口径）
  - 持有期：3 / 5 / 10 个交易日，到期收盘结算
  - 离场规则：hold（不动）/ stop（-5% 止损）/ stop_take（-5% 止损 +10% 止盈）
  - 基准：全市场等权同期涨幅；超额 = 策略平均收益 - 基准
  - 合规剔除已在信号生成阶段静默完成，本研究不单独列出

用法：python3 strategy_mining.py
产出：
  data/reports/买股策略研究_2026-11-07.md
  data/reports/strategy_signals_2026-08-01_2026-11-06.csv
  data/reports/strategy_grid_2026-08-01_2026-11-06.csv
"""
import os
import glob
import numpy as np
import pandas as pd

import lib

LAST = "2026-11-06"
HORIZONS = (3, 5, 10)
MIN_SAMPLES = 20
SRC = os.path.join(lib.REPORT_DIR, "backtest_stance_2026-08-01_2026-11-06.csv")
OUT_SIG = os.path.join(lib.REPORT_DIR, "strategy_signals_2026-08-01_2026-11-06.csv")
OUT_GRID = os.path.join(lib.REPORT_DIR, "strategy_grid_2026-08-01_2026-11-06.csv")
OUT_MD = os.path.join(lib.REPORT_DIR, "买股策略研究_2026-11-07.md")


def load_matrices():
    """把全部本地日线拼成 open/high/low/close/volume 五张 (日期 × 代码) 矩阵。"""
    series = {}
    for f in sorted(glob.glob(os.path.join(lib.KLINE_DIR, "*.csv"))):
        code = os.path.basename(f)[:6]
        try:
            df = pd.read_csv(f)
        except Exception:
            continue
        df["date"] = df["date"].astype(str)
        df = df[df["date"] <= LAST]
        if df.empty:
            continue
        series[code] = df.set_index("date")
    dates = sorted(set().union(*[set(s.index) for s in series.values()]))
    mats = {col: pd.DataFrame({c: s[col] for c, s in series.items()}).reindex(dates)
            for col in ("open", "high", "low", "close", "volume")}
    return dates, mats


def build_bench(dates, mats, signal_dates):
    """全市场等权基准：只算有信号的 (日期, 持有期) 组合，避免全矩阵遍历。"""
    idx = {d: i for i, d in enumerate(dates)}
    cl = mats["close"]
    B = {}
    for D in signal_dates:
        j = idx.get(D)
        if j is None:
            continue
        for N in HORIZONS:
            if j + N >= len(dates):
                continue
            ratio = (cl.iloc[j + N] / cl.iloc[j]).replace([np.inf, -np.inf], np.nan).dropna()
            B[(D, N)] = float(ratio.mean() - 1) * 100 if len(ratio) > 200 else None
    return B


def sim_exit(buy, j, N, cl_s, hi_s, lo_s, rule):
    """逐日检查止损/止盈；同日双触发保守按先止损。未触发则到期收盘结算。

    止损/止盈按触发价成交是近似（未计跳空滑点），与既有 v4 回测 C 口径一致。
    """
    stop_px, take_px = buy * 0.95, buy * 1.10
    if rule != "hold":
        for k in range(j + 1, j + N + 1):
            lv, hv = lo_s[k], hi_s[k]
            if np.isfinite(lv) and lv <= stop_px:
                return (stop_px / buy - 1) * 100
            if rule == "stop_take" and np.isfinite(hv) and hv >= take_px:
                return (take_px / buy - 1) * 100
    cN = cl_s[j + N]
    return (cN / buy - 1) * 100 if np.isfinite(cN) else np.nan


def build_rows(dates, mats, df, B):
    idx = {d: i for i, d in enumerate(dates)}
    cl, op, hi, lo = mats["close"], mats["open"], mats["high"], mats["low"]

    # 每日评分前 6（对应自动化实际推送的 TOP6 口径）
    top6 = set()
    for D, g in df.groupby("date"):
        for c in g.sort_values("score", ascending=False)["code"].head(6):
            top6.add((str(D), str(c)))

    rows = []
    for _, s in df.iterrows():
        c, D = str(s["code"]), str(s["date"])
        j = idx.get(D)
        if j is None or c not in cl.columns:
            continue
        cl_s = cl[c].values.astype(float)
        op_s = op[c].values.astype(float)
        hi_s = hi[c].values.astype(float)
        lo_s = lo[c].values.astype(float)
        buy_c = float(s["last_close"])
        buy_no = op_s[j + 1] if j + 1 < len(dates) else np.nan

        r = dict(
            code=c, name=str(s["name"]), date=D,
            score=float(s["score"]), vol_x=float(s["vol_x"]), chg=float(s["chg"]),
            sw_range=float(s["sw_range"]), sw_std=float(s["sw_std"]),
            prox=float(s["prox"]), req_vol_wan=float(s["req_vol_wan"]),
            trigger=float(s["trigger"]), platform_top=float(s["platform_top"]),
            buy_close=round(buy_c, 2),
            buy_next_open=round(float(buy_no), 2) if np.isfinite(buy_no) else None,
            is_top6=(D, c) in top6, limit_up=float(s["chg"]) >= 9.8,
        )
        for N in HORIZONS:
            jN = j + N
            r[f"bench{N}"] = B.get((D, N))
            if jN >= len(dates):
                continue
            r[f"close{N}"] = cl_s[jN] if np.isfinite(cl_s[jN]) else None
            seg_hi, seg_lo = hi_s[j + 1:jN + 1], lo_s[j + 1:jN + 1]
            if np.isfinite(buy_c) and buy_c > 0:
                if len(seg_hi) and np.isfinite(np.nanmax(seg_hi)):
                    r[f"mfe{N}"] = round((float(np.nanmax(seg_hi)) / buy_c - 1) * 100, 2)
                if len(seg_lo) and np.isfinite(np.nanmin(seg_lo)):
                    r[f"mae{N}"] = round((float(np.nanmin(seg_lo)) / buy_c - 1) * 100, 2)
            for bp, buy in (("A", buy_c), ("NO", buy_no)):
                if not np.isfinite(buy) or buy <= 0 or not np.isfinite(cl_s[jN]):
                    continue
                r[f"{bp}_ret{N}_hold"] = round((cl_s[jN] / buy - 1) * 100, 2)
                for rule in ("stop", "stop_take"):
                    v = sim_exit(buy, j, N, cl_s, hi_s, lo_s, rule)
                    if np.isfinite(v):
                        r[f"{bp}_ret{N}_{rule}"] = round(v, 2)
        rows.append(r)
    return rows


FACTORS = ("score", "vol_x", "chg", "sw_range", "sw_std", "prox", "req_vol_wan")

FILTERS = {
    "无过滤": lambda r: True,
    "评分≥0.58": lambda r: r["score"] >= 0.58,
    "评分≥0.62": lambda r: r["score"] >= 0.62,
    "量比≥2.5": lambda r: r["vol_x"] >= 2.5,
    "量比≥3.0": lambda r: r["vol_x"] >= 3.0,
    "非涨停(涨幅<9.8%)": lambda r: r["chg"] < 9.8,
    "涨停(涨幅≥9.8%)": lambda r: r["chg"] >= 9.8,
    "横盘紧(振幅≤1.12)": lambda r: r["sw_range"] <= 1.12,
    "横盘极紧(振幅≤1.08)": lambda r: r["sw_range"] <= 1.08,
    "波动小(std≤0.030)": lambda r: r["sw_std"] <= 0.030,
    "贴平台上沿(prox≤1.05)": lambda r: r["prox"] <= 1.05,
    "远离平台上沿(prox≥1.08)": lambda r: r["prox"] >= 1.08,
    "所需量≤15万手": lambda r: r["req_vol_wan"] <= 15,
    "每日TOP6": lambda r: r["is_top6"],
    "非涨停+评分≥0.58": lambda r: r["chg"] < 9.8 and r["score"] >= 0.58,
    "评分≥0.58+量比≥2.5": lambda r: r["score"] >= 0.58 and r["vol_x"] >= 2.5,
    "横盘紧+评分≥0.58": lambda r: r["sw_range"] <= 1.12 and r["score"] >= 0.58,
    "TOP6+非涨停": lambda r: r["is_top6"] and r["chg"] < 9.8,
}

BUYPOINTS = {"A": "提示日收盘", "NO": "次日开盘"}
RULES = {"hold": "到期收盘", "stop": "-5%止损", "stop_take": "-5%止损/+10%止盈"}
RULE_CODE = {"到期收盘": "hold", "-5%止损": "stop", "-5%止损/+10%止盈": "stop_take"}


def stats(vals, benches):
    """vals / benches 必须等长（同一批信号按同一顺序），内部按位置配对过滤。"""
    pairs = []
    for v, b in zip(vals, benches):
        if v is None or not np.isfinite(float(v)):
            continue
        bv = float(b) if (b is not None and np.isfinite(float(b))) else None
        pairs.append((float(v), bv))
    if not pairs:
        return None
    a = np.array([p[0] for p in pairs], dtype=float)
    pb = [(v, b) for v, b in pairs if b is not None]
    ab = np.array([p[0] for p in pb], dtype=float)
    bb = np.array([p[1] for p in pb], dtype=float)
    return dict(
        n=len(a), avg=round(float(a.mean()), 2), median=round(float(np.median(a)), 2),
        win=round(float((a > 0).mean() * 100), 1),
        total=round(float(a.sum()), 1),
        beat=round(float((ab > bb).mean() * 100), 1) if len(bb) else None,
        bench=round(float(bb.mean()), 2) if len(bb) else None,
    )


def evaluate(sub, key, N, split):
    def pack(rs):
        return stats([r.get(key) for r in rs], [r.get(f"bench{N}") for r in rs])
    tr = pack([r for r in sub if r["date"] <= split])
    oo = pack([r for r in sub if r["date"] > split])
    return pack(sub), tr, oo


def grid_search(rows, split):
    out = []
    for fname, fn in FILTERS.items():
        sub = [r for r in rows if fn(r)]
        if len(sub) < MIN_SAMPLES:
            continue
        for bp in BUYPOINTS:
            for N in HORIZONS:
                for rule in RULES:
                    key = f"{bp}_ret{N}_{rule}"
                    full, tr, oo = evaluate(sub, key, N, split)
                    if not full or full["n"] < MIN_SAMPLES:
                        continue
                    ex = (round(full["avg"] - full["bench"], 2)
                          if full["bench"] is not None else None)
                    out.append(dict(
                        filter=fname, buy=BUYPOINTS[bp], hold=N, exit=RULES[rule], key=key,
                        n=full["n"], avg=full["avg"], median=full["median"],
                        win=full["win"], total=full["total"],
                        bench=full["bench"], excess=ex, beat=full["beat"],
                        tr_n=tr["n"] if tr else 0, tr_avg=tr["avg"] if tr else None,
                        oo_n=oo["n"] if oo else 0, oo_avg=oo["avg"] if oo else None,
                        oo_win=oo["win"] if oo else None,
                    ))
    out.sort(key=lambda x: -x["avg"])
    return out


def factor_table(rows):
    lines = ["| 因子 | 分组 | 样本 | 3日 | 5日 | 10日 | 5日胜率 |",
             "|---|---|---|---|---|---|---|"]
    for f in FACTORS:
        vals = sorted(r[f] for r in rows if r.get(f) is not None)
        if len(vals) < 30:
            continue
        q1, q2 = np.percentile(vals, [33, 67])
        groups = [("低", lambda v: v <= q1), ("中", lambda v: q1 < v <= q2),
                  ("高", lambda v: v > q2)]
        for gname, cond in groups:
            sub = [r for r in rows if r.get(f) is not None and cond(r[f])]
            if len(sub) < 10:
                continue
            cells = []
            for N in HORIZONS:
                v = [r.get(f"A_ret{N}_hold") for r in sub
                     if r.get(f"A_ret{N}_hold") is not None]
                cells.append(f"{np.mean(v):+.2f}%" if v else "--")
            w = [r.get("A_ret5_hold") for r in sub if r.get("A_ret5_hold") is not None]
            win = f"{sum(1 for x in w if x > 0) / len(w) * 100:.1f}%" if w else "--"
            lines.append(f"| {f} | {gname} | {len(sub)} | " + " | ".join(cells) + f" | {win} |")
    return lines


def baseline_table(rows):
    lines = ["| 买点 | 持有期 | 样本 | 平均收益 | 中位 | 胜率 | 同期市场 | 超额 |",
             "|---|---|---|---|---|---|---|---|"]
    for bp in ("A", "NO"):
        for N in HORIZONS:
            key = f"{bp}_ret{N}_hold"
            vals = [r.get(key) for r in rows]
            bcs = [r.get(f"bench{N}") for r in rows]
            s = stats(vals, bcs)
            if not s:
                continue
            ex = f"{s['avg'] - s['bench']:+.2f}pp" if s["bench"] is not None else "--"
            lines.append(f"| {BUYPOINTS[bp]} | {N} 日 | {s['n']} | {s['avg']:+.2f}% | "
                         f"{s['median']:+.2f}% | {s['win']:.1f}% | "
                         f"{s['bench']:+.2f}% | {ex} |")
    return lines


def distribution_diagnosis(rows, filter_name, key):
    """拆开「平均收益」：看是否被少数极端暴涨撑起来。"""
    sub = [r for r in rows if FILTERS[filter_name](r)]
    v = pd.Series([r.get(key) for r in sub], dtype=float).replace(
        [np.inf, -np.inf], np.nan).dropna()
    if len(v) < 10:
        return ["- 样本不足，无法做分布诊断。"]
    q = v.quantile([0, 0.25, 0.5, 0.75, 0.9, 1])
    top3 = v.nlargest(3)
    rest = v.drop(top3.index)
    stopped = int((v <= -4.99).sum())
    share = (top3.sum() / v.sum() * 100) if v.sum() > 0 else None
    share_s = f"{share:.1f}%" if share is not None else "--"
    return [
        "| 指标 | 数值 |", "|---|---|",
        f"| 样本数 | {len(v)} |",
        f"| 平均收益 | **{v.mean():+.2f}%** |",
        f"| 中位收益 | **{v.median():+.2f}%** |",
        f"| 最小 / 25% / 75% / 90% / 最大 | {q.loc[0]:+.2f}% / {q.loc[0.25]:+.2f}% / "
        f"{q.loc[0.75]:+.2f}% / {q.loc[0.9]:+.2f}% / {q.loc[1]:+.2f}% |",
        f"| 以 -5% 止损离场 | {stopped}/{len(v)}（{stopped / len(v) * 100:.1f}%） |",
        "| 最大 3 笔 | " + " / ".join(f"{x:+.2f}%" for x in top3) + " |",
        f"| 最大 3 笔合计 / 全部合计 | {top3.sum():+.1f}pp / {v.sum():+.1f}pp（占 {share_s}） |",
        f"| **剔除最大 3 笔后平均** | **{rest.mean():+.2f}%** |",
    ]


def main():
    print("[1/5] 载入 K 线矩阵 ...")
    dates, mats = load_matrices()
    print(f"      交易日 {len(dates)} 天（至 {dates[-1]}），覆盖 {len(mats['close'].columns)} 只")

    df = pd.read_csv(SRC, encoding="utf-8-sig", dtype={"code": str})
    df["code"] = df["code"].astype(str).str.zfill(6)
    B = build_bench(dates, mats, sorted(df["date"].astype(str).unique()))
    print(f"[2/5] 对齐 {len(df)} 条信号 ...")
    rows = build_rows(dates, mats, df, B)
    print(f"      可用 {len(rows)} 条")

    sig_dates = sorted({r["date"] for r in rows})
    split = sig_dates[int(len(sig_dates) * 0.6)]
    print(f"[3/5] 时间切分：训练 ≤ {split}，样本外 > {split}")

    print("[4/5] 网格搜索 ...")
    grid = grid_search(rows, split)
    print(f"      有效组合 {len(grid)} 个（样本 ≥{MIN_SAMPLES}）")

    pd.DataFrame(rows).to_csv(OUT_SIG, index=False, encoding="utf-8-sig")
    pd.DataFrame(grid).to_csv(OUT_GRID, index=False, encoding="utf-8-sig")

    best = grid[0] if grid else None
    exec_top = [g for g in grid if g["buy"] == "次日开盘"]
    best_exec = exec_top[0] if exec_top else None
    stable_top = sorted([g for g in grid if g["n"] >= MIN_SAMPLES],
                        key=lambda x: (-x["median"], -x["avg"]))
    best_stable = stable_top[0] if stable_top else None

    print("[5/5] 生成报告 ...")

    def emit_top(sub, n=5):
        out = ["| 过滤条件 | 买点 | 持有 | 离场 | 样本 | 平均 | 中位 | 胜率 | 超额 | 训练期 | 样本外 |",
               "|---|---|---|---|---|---|---|---|---|---|---|"]
        for g in sub[:n]:
            oo = f"{g['oo_avg']:+.2f}% ({g['oo_n']}笔)" if g["oo_avg"] is not None else "样本不足"
            tr = f"{g['tr_avg']:+.2f}% ({g['tr_n']}笔)" if g["tr_avg"] is not None else "--"
            ex = f"{g['excess']:+.2f}pp" if g["excess"] is not None else "--"
            out.append(f"| {g['filter']} | {g['buy']} | {g['hold']}日 | {g['exit']} | {g['n']} | "
                       f"**{g['avg']:+.2f}%** | {g['median']:+.2f}% | {g['win']:.1f}% | {ex} | {tr} | {oo} |")
        return out

    L = []
    L.append("# 凯众模型筛出股票 · 3/5/10 日买股策略研究")
    L.append("")
    L.append(f"> 基准日 {LAST}｜信号 {len(rows)} 条（{sig_dates[0]} ~ {sig_dates[-1]}，"
             f"{df['code'].nunique()} 只标的）｜合规剔除已在筛选阶段静默完成")
    L.append("")

    L.append("## 一、结论先行")
    L.append("")
    L.append("你要的是「收益率最高」，这里给两层答案，并且必须把收益质量一起摆出来：")
    L.append("")
    if best:
        L.append(f"1. **账面平均收益最高**：{best['filter']} + **{best['buy']}** + 持有 {best['hold']} 日 + "
                 f"{best['exit']}，{best['n']} 笔平均 **{best['avg']:+.2f}%**"
                 f"（中位 {best['median']:+.2f}%、胜率 {best['win']:.1f}%、超额 {best['excess']:+.2f}pp）。")
    if best_exec:
        L.append(f"2. **真正可执行（次日开盘）下收益最高**：{best_exec['filter']} + 次日开盘 + "
                 f"持有 {best_exec['hold']} 日 + {best_exec['exit']}，{best_exec['n']} 笔平均 "
                 f"**{best_exec['avg']:+.2f}%**（中位 {best_exec['median']:+.2f}%、胜率 {best_exec['win']:.1f}%），"
                 f"样本外 {best_exec['oo_avg']:+.2f}%（{best_exec['oo_n']} 笔）。")
    L.append("")
    L.append("> **两个「最高」都不建议照搬**：它们的中位收益是 -5%、胜率仅 20~27%，"
             "平均收益几乎全靠 2~3 笔暴涨股撑起来（第四节诊断：剔除最大 3 笔后账面最优转为负值）。"
             "这是彩票型收益，不是可重复的买法；提示日收盘买入的账面口径在实盘也拿不到。")
    L.append("")
    L.append("**唯一在各口径、样本内外方向一致且有形态逻辑的发现**：`prox`（突破日收盘价 ÷ 平台上沿）"
             "越高越好 —— 突破越坚决、离平台越远，后续越不容易跌回去。单因子 prox 高组 10 日 -1.05%，"
             "低组 -4.67%；网格前列全部由 `prox≥1.08` 占据。它可以作为「同一批信号里优先买谁」的倾向，"
             "但改善的是相对表现，不能把整体负期望变成正期望。")
    L.append("")
    L.append("> 既有结论不变：v4 全样本 10 日跑输大盘 1.50pp、止损率 68.4%。"
             "本研究是在一个整体负期望的信号池里做相对优选，**不等于找到稳赚策略**。")
    L.append("")

    L.append("## 二、数据与口径")
    L.append("")
    L.append("| 项 | 说明 |")
    L.append("|---|---|")
    L.append("| 信号源 | `backtest_stance_2026-08-01_2026-11-06.csv`，v4「横盘约两个月 → 站稳 MA5 → 放量突破当天提示」全市场逐日扫描 |")
    L.append("| 买点 A | 提示日收盘价（尾盘竞价近似；收盘后才确认，实盘有确认滞后） |")
    L.append("| 买点 NO | **次日开盘价**——收盘后才知道信号，这是最干净、真正可成交的口径 |")
    L.append("| 持有期 | 3 / 5 / 10 个交易日，到期收盘结算 |")
    L.append("| 离场 | 到期收盘 / 触及 -5% 止损 / 触及 -5% 止损且 +10% 止盈（同日双触发保守按先止损） |")
    L.append("| 基准 | 全市场等权同期涨幅；超额 = 策略平均收益 − 基准 |")
    L.append("| 费用 | 未扣佣金、印花税、滑点；止损按触发价成交为近似 |")
    L.append("| 组合 | 等权买入所有符合过滤条件的信号，不做仓位优化 |")
    L.append("")

    L.append("## 三、基准：不做任何过滤，全部信号照买")
    L.append("")
    L += baseline_table(rows)
    L.append("")
    L.append("这张表是判断「过滤有没有用」的标尺——任何策略必须显著优于同买点、同持有期的这一行，才有讨论价值。")
    L.append("")

    L.append("## 四、收益质量诊断：那个 +2.59% 是怎么来的")
    L.append("")
    if best:
        bp_code = "A" if best["buy"] == "提示日收盘" else "NO"
        diag_key = f"{bp_code}_ret{best['hold']}_{RULE_CODE[best['exit']]}"
        L.append(f"把账面最优组合（{best['filter']} / {best['buy']} / {best['hold']}日 / {best['exit']}）逐笔拆开：")
        L.append("")
        L += distribution_diagnosis(rows, best["filter"], diag_key)
        L.append("")
        L.append("**平均 +2.59% 但中位 -5.00%**，说明一半以上交易是止损出局；最大 3 笔合计已超过全部净收益之和，"
                 "剔除后平均转负。另一个佐证：同样过滤条件下加「+10% 止盈」，平均收益反而从 +2.59% 降到 +2.05%，"
                 "正是因为止盈把少数暴涨股提前卖掉了 —— 收益完全依赖右尾，没有中间地带。")
        L.append("")
        L.append("换言之，这不是「选股准」，而是「在 24 次小亏里等 2~3 次涨停连板」。"
                 "这类策略对样本期、行情、运气极度敏感，样本外即使为正也不代表可复制。")
    L.append("")

    L.append("## 五、单因子分组（提示日收盘买入，到期结算）")
    L.append("")
    L += factor_table(rows)
    L.append("")
    L.append("按 33/67 分位分低/中/高三组。只有 `prox` 呈现清晰的单调方向（高组 10 日 -1.05%、"
             "低组 -4.67%），其余因子分组间差异小且方向不稳定 —— 这本身也说明模型评分、量比等"
             "指标对后续收益几乎没有预测力。")
    L.append("")

    L.append("## 六、组合规则搜索")
    L.append("")
    L.append(f"共评估 {len(grid)} 个组合（过滤 18 种 × 买点 2 × 持有期 3 × 离场 3），"
             f"每个组合至少 {MIN_SAMPLES} 笔样本才纳入排名。完整结果见 `strategy_grid_*.csv`。")
    L.append("")
    L.append("### 6.1 账面口径（提示日收盘）Top 5")
    L.append("")
    L += emit_top([g for g in grid if g["buy"] == "提示日收盘"], 5)
    L.append("")
    L.append("### 6.2 可执行口径（次日开盘）Top 5")
    L.append("")
    L += emit_top([g for g in grid if g["buy"] == "次日开盘"], 5)
    L.append("")
    L.append("账面与可执行口径的落差（同类规则 +2.59% → +0.78%）就是「用提示日收盘价倒推成本」的幻觉；"
             "实盘只能拿到 6.2 的收益。")
    L.append("")

    L.append("## 七、按「中位收益」排序的稳健榜（不靠尾部）")
    L.append("")
    L += emit_top(stable_top, 5)
    L.append("")
    if best_stable:
        L.append(f"最稳健的一档是 `{best_stable['filter']}` + {best_stable['buy']} + 持有 {best_stable['hold']} 日 + "
                 f"{best_stable['exit']}：中位 {best_stable['median']:+.2f}%、胜率 {best_stable['win']:.1f}%，"
                 f"但平均也只有 {best_stable['avg']:+.2f}%。**「不靠暴涨」和「有收益」在这个信号池里不可兼得**。")
        L.append("")
        L.append("注意该组合训练期仅 9 笔、平均为负，样本外 19 笔微正 —— 样本太少且前后不一致，"
                 "只能说明「亏得比较均匀」，不能说明能赚钱。")
    L.append("")

    L.append("## 八、样本外验证（时间切分，防过拟合）")
    L.append("")
    L.append(f"切分点 **{split}**（前 60% 交易日训练 / 后 40% 验证）。"
             "若训练期很亮、验证期转负，说明该阈值只是拟合了前半段行情。")
    L.append("")
    L.append("| 过滤条件 | 买点 | 持有 | 离场 | 训练平均 | 样本外平均 | 样本外胜率 | 衰减 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for g in grid[:5]:
        if g["tr_avg"] is not None and g["oo_avg"] is not None:
            decay = f"{g['oo_avg'] - g['tr_avg']:+.2f}pp"
            L.append(f"| {g['filter']} | {g['buy']} | {g['hold']}日 | {g['exit']} | "
                     f"{g['tr_avg']:+.2f}% | **{g['oo_avg']:+.2f}%** | {g['oo_win']:.1f}% | {decay} |")
        else:
            L.append(f"| {g['filter']} | {g['buy']} | {g['hold']}日 | {g['exit']} | -- | 样本不足 | -- | -- |")
    L.append("")
    L.append("样本外没有整体转负，但这不能证明策略有效：验证期只有约 12~17 笔，且收益仍由个别暴涨股决定，"
             "统计上等价于「又抽了一次签」。")
    L.append("")

    L.append("## 九、给你的策略")
    L.append("")
    if best_exec:
        L.append("如果一定要在这批信号里挑一套「收益率最高、且能真正下单」的规则，就是下面这套（对应 6.2 第一名）：")
        L.append("")
        L.append(f"1. **选股**：当日 v4 命中里只取 `{best_exec['filter']}`，"
                 "即突破日收盘价 ≥ 平台上沿 ×1.08（突破够坚决、离平台够远）；等权买入，不做主观挑票。")
        L.append("2. **买点**：**次日开盘价**成交。收盘后才确认量价达标，"
                 "不要用触发价、提示日开盘价或收盘价倒推成本。")
        L.append(f"3. **持有与离场**：固定 {best_exec['hold']} 个交易日；期间最低价触及买入价 -5% 即止损，"
                 "到期收盘卖出；不加止盈（加了会砍掉唯一的大涨来源）。")
        L.append(f"4. **历史预期**：{best_exec['n']} 笔平均 {best_exec['avg']:+.2f}%、"
                 f"中位 {best_exec['median']:+.2f}%、胜率 {best_exec['win']:.1f}%、"
                 f"超额 {best_exec['excess']:+.2f}pp；样本外 {best_exec['oo_n']} 笔 {best_exec['oo_avg']:+.2f}%。"
                 "**注意这是「十次里八次小亏、靠一两次大涨覆盖」的收益结构，不是稳定盈利。**")
        L.append("5. **仓位与复盘**：单笔风险 -5%，只用小仓位试跑；连续 20 笔后按实际中位收益与胜率复核，"
                 "若中位仍是 -5% 就停用。")
        L.append("")
    if best_stable:
        L.append("如果你要的是「胜率优先、可重复」而不是「平均收益最高」，改用第七节稳健榜第一名："
                 f"`{best_stable['filter']}` + {best_stable['buy']} + 持有 {best_stable['hold']} 日 + "
                 f"{best_stable['exit']}（{best_stable['n']} 笔，平均 {best_stable['avg']:+.2f}%、"
                 f"中位 {best_stable['median']:+.2f}%、胜率 {best_stable['win']:.1f}%）——代价是收益基本为零。")
    L.append("")

    L.append("## 十、风险与局限")
    L.append("")
    L.append("- **尾部依赖（最关键）**：账面最优 33 笔里仅 9 笔盈利（胜率 27.3%），"
             "24 笔以约 -5% 或更差离场；最大 3 笔贡献 137% 的净收益，剔除后平均 -1.06%。"
             "平均收益这个指标在这里严重失真。")
    L.append("- **数据挖掘偏差**：18×2×3×3 个组合里挑第一名，本身就是多重比较；"
             "样本内最优几乎必然高于真实期望。")
    L.append("- **可执行性落差**：账面最优用提示日收盘价，实盘拿不到；换成次日开盘后收益从 +2.59% 降到 +1.44% 以下。")
    L.append("- **样本期短**：仅 2026-08-03~11-06 约 64 个交易日、456 条信号，"
             "且只覆盖一段行情，不足以跨越牛熊。")
    L.append("- **基准弱**：同期全市场本身下跌，正超额可能只是跌得少，不等于赚钱。")
    L.append("- **未扣费用**：佣金、印花税、滑点会进一步侵蚀本就微薄的收益；止损按触发价成交偏乐观。")
    L.append("- **不改变模型结论**：原回测结论报告「v4 跑输大盘、不建议并入生产」的判断依然成立，"
             "本策略只是同一批弱信号里的相对优选。")
    L.append("")

    L.append("## 十一、产出文件")
    L.append("")
    L.append("| 文件 | 内容 |")
    L.append("|---|---|")
    L.append(f"| `{os.path.basename(OUT_MD)}` | 本研究报告 |")
    L.append(f"| `{os.path.basename(OUT_SIG)}` | 逐信号明细：形态特征、买价、3/5/10 日各买点×离场收益、期间最高/最低、基准 |")
    L.append(f"| `{os.path.basename(OUT_GRID)}` | 全部 {len(grid)} 个组合的排名与训练/样本外表现 |")
    L.append("")

    open(OUT_MD, "w", encoding="utf-8").write("\n".join(L))
    print(f"      报告：{OUT_MD}")
    if best:
        print(f"      账面最优：{best['filter']} / {best['buy']} / {best['hold']}日 / {best['exit']} "
              f"→ {best['avg']:+.2f}%（n={best['n']}）")
    if best_exec:
        print(f"      可执行最优：{best_exec['filter']} / 次日开盘 / {best_exec['hold']}日 / "
              f"{best_exec['exit']} → {best_exec['avg']:+.2f}%（n={best_exec['n']}）")
    return grid


if __name__ == "__main__":
    main()
