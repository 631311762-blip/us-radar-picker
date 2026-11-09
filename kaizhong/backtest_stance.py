# -*- coding: utf-8 -*-
"""
【横盘突破提示模型 v4】回测：无试盘因素、提示点 = 放量突破当天。

模型（stance_hint.STANCE）：
  - 硬前置：提示日前 40 个交易日收盘价须横盘（收盘 max/min ≤1.20、std/mean ≤0.045），
    前后各 20 日分别横盘；仅用收盘价，避免影线干扰。
  - 站稳 MA5（连续 2 日收盘 > MA5、MA5 上行）。
  - 提示条件 = 当日「收盘 ≥ 触发价 且 量 ≥ 所需量」——提示即买点，无等待期。

严格避免未来函数：
  - 每个交易日 D，只用 date <= D 的 K 线跑 stance_hint.scan_one（与实盘一致）。
  - 收益用 D 之后的真实 K 线评估（历史已知，回测允许）。
  - 合规剔除复用 lib.blocked_by_rule，静默执行。

三套口径（提示日 D = 买点日）：
  A 收盘口径：以提示日收盘价买入并持有，衡量信号本身的预测力。
  B 买点口径（对照）：以 max(触发价, 当日开盘) 买入，硬持有不执行止损 —— 会高估收益。
  C 纪律口径（可执行）：同 B 买入，但买入次日起任一日最低价 ≤ 止损价即按 -5% 离场。
  注：买入价取触发价与开盘价的较高者，实盘收盘确认通常更贵，收益偏乐观。

准确率定义：
  - 上涨胜率 = 收益 > 0 占比
  - 跑赢率   = 收益 > 同期全市场基准 占比
  - 超额     = 平均收益 - 同期全市场平均收益

用法：python3 backtest_stance.py [起始日 YYYY-MM-DD] [结束日 YYYY-MM-DD]
输出：reports/backtest_stance_<起始>_<结束>.md / .csv
"""
import os
import sys
import glob
import json

import numpy as np
import pandas as pd

import lib
import stance_hint as sh

START = sys.argv[1] if len(sys.argv) > 1 else "2026-10-24"
END = sys.argv[2] if len(sys.argv) > 2 else None
HORIZONS = (1, 3, 5, 10)


def load_all():
    data = {}
    for f in sorted(glob.glob(os.path.join(lib.KLINE_DIR, "*.csv"))):
        code = os.path.basename(f)[:6]
        try:
            df = pd.read_csv(f)
        except Exception:
            continue
        df["date"] = df["date"].astype(str)
        if len(df) < 70:
            continue
        data[code] = df.reset_index(drop=True)
    return data


def build_matrices(data):
    dates = sorted(set().union(*[set(d["date"]) for d in data.values()]))
    px = pd.DataFrame({c: d.set_index("date")["close"] for c, d in data.items()}).reindex(dates)
    op = pd.DataFrame({c: d.set_index("date")["open"] for c, d in data.items()}).reindex(dates)
    lo = pd.DataFrame({c: d.set_index("date")["low"] for c, d in data.items()}).reindex(dates)
    vo = pd.DataFrame({c: d.set_index("date")["volume"] for c, d in data.items()}).reindex(dates)
    return dates, px, op, lo, vo


def main():
    f10 = lib.load_f10()
    data = load_all()
    dates, px, op, lo, vo = build_matrices(data)
    idx = {d: i for i, d in enumerate(dates)}
    eval_days = [d for d in dates if d >= START and (END is None or d <= END)]

    # 合规白名单（静默剔除，不单独列出）
    allowed = {}
    for code, df in data.items():
        row = f10.get(code, {})
        nm = row.get("name") or code
        if not lib.blocked_by_rule(nm, code, row):
            allowed[code] = nm

    # ---- 逐日扫描（只用截止当日数据）----
    signals = []
    daily = []
    for D in eval_days:
        day_hits = []
        for code, nm in allowed.items():
            r = sh.scan_one(code, data[code], as_of=D)
            if r:
                r.update(name=nm, D=D)
                day_hits.append(r)
        signals.extend(day_hits)
        daily.append(dict(date=D, hits=len(day_hits),
                          universe=len(allowed),
                          rate=round(len(day_hits) / max(len(allowed), 1) * 100, 3)))

    # ---- 收益评估 ----
    BENCH = {}
    for N in HORIZONS:
        for j in range(len(dates) - N):
            ratio = (px.iloc[j + N] / px.iloc[j]).replace([np.inf, -np.inf], np.nan).dropna()
            BENCH[(dates[j], N)] = float(ratio.mean() - 1) * 100 if len(ratio) > 200 else None

    def bench(entry, N):
        return BENCH.get((entry, N))

    def fwd_close(code, D, N):
        j = idx[D]
        if j + N >= len(dates):
            return None
        c0, c1 = px.at[D, code], px.at[dates[j + N], code]
        if pd.isna(c0) or pd.isna(c1) or c0 <= 0:
            return None
        return (c1 / c0 - 1) * 100

    rows = []
    for s in signals:
        D, code = s["D"], s["code"]
        rec = dict(s)
        j0 = idx[D]

        # A 收盘口径
        for N in HORIZONS:
            r = fwd_close(code, D, N)
            b = bench(D, N)
            rec[f"A_ret{N}"] = round(r, 2) if r is not None else None
            rec[f"A_bench{N}"] = round(b, 2) if b is not None else None

        # 买点 = 提示日当天（无等待期）
        c0, o0 = px.at[D, code], op.at[D, code]
        buy_px = max(s["trigger"], float(o0) if not pd.isna(o0) else c0)
        buy_date = D
        rec["buy_date"] = buy_date
        rec["buy_px"] = round(buy_px, 2)

        # B 硬持有（对照）
        for N in HORIZONS:
            if j0 + N >= len(dates):
                rec[f"B_ret{N}"] = None
                rec[f"B_bench{N}"] = None
                continue
            c1 = px.at[dates[j0 + N], code]
            rec[f"B_ret{N}"] = round((c1 / buy_px - 1) * 100, 2) if not pd.isna(c1) else None
            b = bench(D, N)
            rec[f"B_bench{N}"] = round(b, 2) if b is not None else None

        # 止损：买入次日起，10 日观察窗内最低价是否击穿止损价
        stop = s["stop"]
        stop_at = None
        for j in range(j0 + 1, min(j0 + max(HORIZONS) + 1, len(dates))):
            lv = lo.at[dates[j], code]
            if not pd.isna(lv) and lv <= stop:
                stop_at = j
                break
        rec["hit_stop"] = stop_at is not None
        rec["c_stop_date"] = dates[stop_at] if stop_at is not None else None

        # C 严格 -5% 止损纪律
        for N in HORIZONS:
            if j0 + N >= len(dates):
                rec[f"C_ret{N}"] = None
                rec[f"C_bench{N}"] = None
                continue
            b = bench(D, N)
            rec[f"C_bench{N}"] = round(b, 2) if b is not None else None
            if stop_at is not None and stop_at <= j0 + N:
                rec[f"C_ret{N}"] = -5.0
            else:
                c1 = px.at[dates[j0 + N], code]
                rec[f"C_ret{N}"] = round((c1 / buy_px - 1) * 100, 2) if not pd.isna(c1) else None
        rows.append(rec)

    df = pd.DataFrame(rows)

    # ---- 汇总 ----
    L = []
    L.append(f"# 横盘突破提示模型回测（无试盘 · 突破当天提示）· {START} ~ {eval_days[-1]}")
    L.append("")
    L.append("> **硬前置**：提示日前 40 个交易日横盘（收盘 max/min≤1.20、收盘 std/mean≤0.045），"
             "前后各 20 日分别横盘；仅用收盘价，避免影线干扰。")
    L.append("> **提示即买点**：当日「收盘 ≥ 触发价 且 成交量 ≥ 所需量」同时满足才提示，无等待期、无试盘判定。")
    L.append("> 逐日只用截止当日数据扫描（无未来函数）；收益用之后真实K线；合规剔除静默完成。")
    L.append(f">  Universe（合规后可参与筛选）= {len(allowed)} 只，评估交易日 {len(eval_days)} 天，累计信号 {len(signals)} 条。")
    L.append("")

    L.append("## 一、每日匹配率（当日突破提示数 / 全市场）")
    L.append("")
    L.append("| 日期 | 命中数 | 匹配率 |")
    L.append("|---|---|---|")
    for d in daily:
        L.append(f"| {d['date']} | {d['hits']} | {d['rate']}% |")
    L.append("")
    L.append(f"> 日均命中 {np.mean([x['hits'] for x in daily]):.1f} 只，"
             f"日均匹配率 {np.mean([x['rate'] for x in daily]):.3f}%。")
    L.append("")

    def stat_block(tag, pfx):
        L.append(f"## {tag}")
        L.append("")
        L.append("| 持有期 | 有效样本 | 平均涨幅 | 中位 | 上涨胜率 | 同期市场 | 超额 | 跑赢率 |")
        L.append("|---|---|---|---|---|---|---|---|")
        for N in HORIZONS:
            sub = df[[f"{pfx}_ret{N}", f"{pfx}_bench{N}"]].dropna()
            if len(sub) == 0:
                L.append(f"| {N} 日 | 0 | -- | -- | -- | -- | -- | -- |")
                continue
            r = sub[f"{pfx}_ret{N}"]
            b = sub[f"{pfx}_bench{N}"]
            bm = b.mean()
            win = (r > 0).mean() * 100
            beat = (r > b).mean() * 100
            ex = r.mean() - bm
            L.append(f"| {N} 日 | {len(r)} | {r.mean():+.2f}% | {r.median():+.2f}% | {win:.1f}% | "
                     f"{bm:+.2f}% | {ex:+.2f}pp | {beat:.1f}% |")
        L.append("")

    stat_block("二、口径A：提示日收盘买入 → N 日后（信号预测力）", "A")
    stat_block("三、口径B：买点价买入 → N 日后（硬持有不止损，仅作对照）", "B")
    stat_block("四、口径C：买点价买入 → N 日后（严格 -5% 止损纪律，可执行）", "C")

    stopn = int((df["hit_stop"] == True).sum()) if len(df) else 0
    cstop = int(df["c_stop_date"].notna().sum()) if len(df) else 0
    L.append("## 五、止损统计")
    L.append("")
    L.append(f"- 全部 **{len(df)}** 条信号均为「突破当天提示即买点」，当日成交，无等待期。")
    L.append(f"- 买入后 10 日窗口内触及 -5% 止损：**{stopn}/{len(df)}（{stopn/max(len(df),1)*100:.1f}%）**")
    L.append(f"- 口径C 按 -5% 纪律离场：**{cstop}/{len(df)}（{cstop/max(len(df),1)*100:.1f}%）**")
    L.append("")

    L.append("## 六、数据边界")
    L.append("")
    L.append(f"- 缓存最后交易日为 {dates[-1]}；从 {START} 起的信号最多只有 "
             f"{len(dates)-1-idx[eval_days[0]]} 个交易日的后续数据。")
    L.append("- 凡不足 N 个交易日未来数据的信号，该 N 列记为空、不计入统计。")
    L.append("- 买入价取 max(触发价, 当日开盘)，实盘收盘确认通常更贵，收益偏乐观；未扣佣金/印花税/滑点。")
    L.append("- 「准确率」同时给出绝对上涨胜率与相对全市场基准的跑赢率；后者才是剔除大盘涨跌后的真实选股能力。")
    L.append("")
    L.append("> 交易纪律：收盘 ≥ 触发价 **且** 成交量 ≥ 所需量同时满足才是买点；回踩不买；止损 -5%。")

    rp = os.path.join(lib.REPORT_DIR, f"backtest_stance_{START}_{eval_days[-1]}.md")
    open(rp, "w", encoding="utf-8").write("\n".join(L))
    cp = os.path.join(lib.REPORT_DIR, f"backtest_stance_{START}_{eval_days[-1]}.csv")
    df.to_csv(cp, index=False, encoding="utf-8-sig")
    print("\n".join(L))
    print(f"\n报告：{rp}\n明细：{cp}")


if __name__ == "__main__":
    main()
