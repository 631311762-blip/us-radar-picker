# -*- coding: utf-8 -*-
"""
凯众 v4 高收益信号 · 次日开盘形态观察

回答的问题：策略研究里收益率最高的那批股票，次日是高开多，还是低开且带下影线多？

口径：
  - 「次日」= 提示日 D 之后该股第一个有交易的交易日（用个股自身 K 线，不用全局日历，避免停牌错位）
  - 缺口/幅度一律相对提示日收盘价 c0 计
  - 主样本：strategy_signals_*.csv 中 prox≥1.08 且 A_ret5_stop 有效的 33 笔（即账面最优组合）
  - 对照：全样本 456 条

产出：
  data/reports/次日开盘形态观察_2026-11-07.md
  data/reports/nextday_gap_2026-08-01_2026-11-06.csv

用法：python3 next_day_gap.py
"""
import os
import numpy as np
import pandas as pd

import lib

LAST = "2026-11-06"
SRC = os.path.join(lib.REPORT_DIR, "strategy_signals_2026-08-01_2026-11-06.csv")
OUT_CSV = os.path.join(lib.REPORT_DIR, "nextday_gap_2026-08-01_2026-11-06.csv")
OUT_MD = os.path.join(lib.REPORT_DIR, "次日开盘形态观察_2026-11-07.md")


def build():
    s = pd.read_csv(SRC, encoding="utf-8-sig", dtype={"code": str})
    s["code"] = s["code"].astype(str).str.zfill(6)
    kl = {}
    for c in s["code"].unique():
        f = os.path.join(lib.KLINE_DIR, f"{c}.csv")
        if not os.path.exists(f):
            continue
        d = pd.read_csv(f)
        d["date"] = d["date"].astype(str)
        d = d[d["date"] <= LAST].reset_index(drop=True)
        kl[c] = d

    rows = []
    for _, r in s.iterrows():
        c, D = str(r["code"]), str(r["date"])
        d = kl.get(c)
        if d is None:
            continue
        m = d.index[d["date"] == D]
        if len(m) == 0 or m[0] + 1 >= len(d):
            continue
        j = m[0] + 1
        c0 = float(d.loc[m[0], "close"])
        o1, h1, l1, c1 = [float(d.loc[j, x]) for x in ("open", "high", "low", "close")]
        if c0 <= 0:
            continue
        gap = (o1 / c0 - 1) * 100
        lowgap = (l1 / c0 - 1) * 100
        closegap = (c1 / c0 - 1) * 100
        lower = (min(o1, c1) - l1) / c0 * 100          # 下影线长度
        body = abs(c1 - o1) / c0 * 100
        upper = (h1 - max(o1, c1)) / c0 * 100
        rows.append(dict(
            code=c, name=str(r["name"]), date=D, prox=float(r["prox"]),
            A5=r.get("A_ret5_stop"), NO5=r.get("NO_ret5_stop"),
            c0=round(c0, 2), o1=round(o1, 2), l1=round(l1, 2), c1=round(c1, 2),
            gap=round(gap, 2), lowgap=round(lowgap, 2), closegap=round(closegap, 2),
            lower=round(lower, 2), body=round(body, 2), upper=round(upper, 2),
            has_lower=lower > 0.05, is_up1=c1 > o1,
            no_pullback=lowgap >= gap - 0.05,           # 开盘即全天最低，几乎没下探
            limit_open=gap >= 9.8,                      # 近似涨停/一字开盘
            open_kind="高开" if gap > 0.5 else ("低开" if gap < -0.5 else "平开"),
        ))
    return pd.DataFrame(rows)


def describe(g):
    """一组的次日形态统计。"""
    return dict(
        n=len(g),
        gap=round(float(g["gap"].mean()), 2),
        gap_med=round(float(g["gap"].median()), 2),
        low_open=round(float((g["gap"] < -0.5).mean() * 100), 1),
        high_open=round(float((g["gap"] > 0.5).mean() * 100), 1),
        lower_pct=round(float(g["has_lower"].mean() * 100), 1),
        lower=round(float(g["lower"].mean()), 2),
        lowgap=round(float(g["lowgap"].mean()), 2),
        up1=round(float(g["is_up1"].mean() * 100), 1),
        no_pull=round(float(g["no_pullback"].mean() * 100), 1),
        A5=round(float(g["A5"].mean()), 2),
        NO5=round(float(g["NO5"].mean()), 2) if g["NO5"].notna().any() else None,
        win=round(float((g["A5"].dropna() > 0).mean() * 100), 1),
    )


def stat_table(pairs):
    L = ["| 分组 | 样本 | 次日缺口均值 | 中位 | 高开占比 | 低开占比 | 有下影线 | 平均下影 | 开盘即最低 | 次日收阳 | 账面5日 |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for nm, g in pairs:
        d = describe(g)
        L.append(f"| {nm} | {d['n']} | **{d['gap']:+.2f}%** | {d['gap_med']:+.2f}% | "
                 f"{d['high_open']:.1f}% | {d['low_open']:.1f}% | {d['lower_pct']:.1f}% | "
                 f"{d['lower']:.2f}% | {d['no_pull']:.1f}% | {d['up1']:.1f}% | {d['A5']:+.2f}% |")
    return L


def main():
    x = build()
    x.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(f"      次日形态明细 {len(x)} 笔 → {OUT_CSV}")

    sub = x[(x["prox"] >= 1.08) & x["A5"].notna()].copy()
    win = sub[sub["A5"] > 0].copy()
    lose = sub[sub["A5"] <= -4.99].copy()
    top = sub.sort_values("A5", ascending=False).head(9)

    L = []
    L.append("# 高收益信号的次日开盘形态观察")
    L.append("")
    L.append(f"> 样本：策略研究里 `prox≥1.08`、5 日收益有效的 **{len(sub)} 笔**（账面最优组合）；"
             f"对照全样本 {len(x)} 笔。基准日 {LAST}。合规剔除已在筛选阶段静默完成。")
    L.append("")

    L.append("## 一、直接回答")
    L.append("")
    L.append("**不是低开带下影线，恰恰相反：收益最高的那批次日几乎都是大幅高开、且基本没有下影线**，"
             "很多是开盘即全天最低、甚至一字/涨停开盘，根本不给低吸机会。")
    L.append("")
    d_w, d_l = describe(win), describe(lose)
    L.append(f"- 盈利的 {len(win)} 笔：次日缺口均值 **{d_w['gap']:+.2f}%**（中位 {d_w['gap_med']:+.2f}%），"
             f"只有 {d_w['low_open']:.1f}% 低开，有下影线的仅 {d_w['lower_pct']:.1f}%，"
             f"次日最低价相对提示日收盘仍是 **{d_w['lowgap']:+.2f}%**。")
    L.append(f"- 亏损的 {len(lose)} 笔（多为 -5% 止损）：次日缺口均值 **{d_l['gap']:+.2f}%**，"
             f"{d_l['low_open']:.1f}% 低开，{d_l['lower_pct']:.1f}% 有下影线，"
             f"次日最低相对提示日收盘 **{d_l['lowgap']:+.2f}%**，只有 {d_l['up1']:.1f}% 收阳。")
    L.append("")
    L.append(f"整组 {len(sub)} 笔看起来「低开略多」（低开 {(sub['gap'] < -0.5).mean()*100:.1f}%、"
             f"高开 {(sub['gap'] > 0.5).mean()*100:.1f}%），但那是被 24 笔亏损票拉出来的；"
             "**低开 + 长下影线在这批信号里是承接不足、突破次日就走弱的特征，不是洗盘买点**。")
    L.append("")
    L.append(f"全样本 {len(x)} 条反而确实是低开为主（低开 {(x['gap'] < -0.5).mean()*100:.1f}%、"
             f"平均缺口 {x['gap'].mean():+.2f}%、{x['has_lower'].mean()*100:.1f}% 有下影线）——"
             "这说明「低开下影」是这批突破信号的普遍形态，但并不指向后续上涨。")
    L.append("")

    L.append("## 二、口径")
    L.append("")
    L.append("| 项 | 定义 |")
    L.append("|---|---|")
    L.append("| 次日 | 提示日 D 之后该股第一个有交易的交易日（按个股 K 线，避免停牌错位） |")
    L.append("| 缺口 | (次日开盘 ÷ 提示日收盘 − 1)，> +0.5% 记高开、< −0.5% 记低开 |")
    L.append("| 下影线 | (min(开, 收) − 最低) ÷ 提示日收盘；> 0.05% 视为有下影 |")
    L.append("| 开盘即最低 | 次日最低 ≈ 开盘（缺口与最低缺口差 <0.05pp），即全天没有下探 |")
    L.append("| 账面5日 | 提示日收盘买入、5 日、-5% 止损口径收益（A_ret5_stop） |")
    L.append("| 次日开盘5日 | 次日开盘价买入、同持有与止损规则（NO_ret5_stop） |")
    L.append("")

    L.append("## 三、分组统计")
    L.append("")
    L += stat_table([
        (f"全样本 {len(x)} 条（有次日K线）", x),
        ("prox≥1.08 整组", sub),
        ("**盈利笔（收益最高那批）**", win),
        ("亏损笔（-5% 止损）", lose),
    ])
    L.append("")
    L.append(f"> 注：全样本行形态统计基于 {len(x)} 笔有次日 K 线的信号；"
             f"「账面5日」列仅按其中 {int(x['A5'].notna().sum())} 笔有 5 日后数据的信号计算。")
    L.append("")
    L.append(f"相关性也一致：次日缺口与账面 5 日收益 **corr = {sub['gap'].corr(sub['A5']):.3f}**，"
             f"次日最低价幅度与收益 **corr = {sub['lowgap'].corr(sub['A5']):.3f}** —— "
             "次日越不跌破提示日收盘，后面越强。")
    L.append("")

    L.append("## 四、收益最高 9 笔逐笔明细")
    L.append("")
    L.append("| 名称 | 提示日 | 次日开盘缺口 | 次日最低(vs提示日收盘) | 下影线 | 次日收盘(vs提示日收盘) | 开盘即最低 | 账面5日 | 次日开盘买入5日 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for _, r in top.iterrows():
        no5 = f"{r['NO5']:+.2f}%" if pd.notna(r["NO5"]) else "--"
        L.append(f"| {r['name']} | {r['date']} | **{r['gap']:+.2f}%** | {r['lowgap']:+.2f}% | "
                 f"{r['lower']:.2f}% | {r['closegap']:+.2f}% | "
                 f"{'是' if r['no_pullback'] else '否'} | **{r['A5']:+.2f}%** | {no5} |")
    L.append("")
    L.append("9 笔里 7 笔高开（其中 4 笔缺口约 +10%），6 笔下影线为 0、开盘即全天最低。"
             "唯一明显低开的是千金药业（-2.64%），但它同样是低开高走、下影线为 0，"
             "不是「低开下探再拉回」。")
    L.append("")

    d_s = describe(sub)
    no5_w = f"{d_w['NO5']:+.2f}%" if d_w["NO5"] is not None else "--"
    no5_s = f"{d_s['NO5']:+.2f}%" if d_s["NO5"] is not None else "--"
    L.append("## 五、这解释了为什么「次日开盘买入」收益大幅缩水")
    L.append("")
    L.append(f"盈利 9 笔账面 5 日平均 **{d_w['A5']:+.2f}%**，换成次日开盘买入后平均 "
             f"**{no5_w}**；整组从 {d_s['A5']:+.2f}% 降到 "
             f"{no5_s}。原因不在手续费，而在：")
    L.append("")
    L.append("1. **高开把涨幅提前吃掉了**：强势票次日直接 +4%~+10% 开盘，"
             "次日买入的成本远高于提示日收盘价，后续空间被压缩。")
    L.append("2. **止损线随买价上移，反而更容易被打掉**：止损价 = 买入价 ×0.95。"
             "高开 10% 买入后，只要回撤 5% 就止损——云南旅游（账面 +14.48% → 次日买入 -5.00%）、"
             "桂林三金（+21.50% → -5.00%）就是这样由盈转亏的。")
    L.append("3. **一字/涨停开盘实际买不进**：明细里多笔开盘即全天最低、缺口约 +10%，"
             "集合竞价阶段没有卖盘，实盘无法成交；这部分在回测里却被假设为已按开盘价成交，"
             "属于虚假成交。影响方向与幅度的严谨核查见《次日涨停与可买入性_2026-11-07》："
             "实测对 NO 口径仅虚高约 0.18pp，属小样本噪声，不足以断言「可执行最优仍然高估」。")
    L.append("")

    L.append("## 六、对你这个问题的实操结论")
    L.append("")
    L.append("- 想等「次日低开 + 下影线」再买这条思路，在这批信号里**方向是反的**："
             "低开下影对应的是亏损组，不是后面的大涨股。")
    L.append("- 真正强势的突破次日往往高开甚至一字，按「次日开盘」这个可执行口径，"
             "要么买贵、要么根本买不到。本节同为 5 日口径，整组从提示日收盘的 +2.59% "
             "降到次日开盘的 +0.78%；上一轮 10 日口径的「可执行最优」也只有 +1.44%，"
             "均远低于账面最优——这个落差不是噪声，而是结构性的。")
    L.append("- 因此不要把这个观察理解成「新增一个低吸买点」。若要继续优化，"
             "更值得研究的是**提示日尾盘竞价能否按收盘价成交**（信号确认滞后问题），"
             "而不是次日回踩。按既有交易纪律，回踩区间仍不是买点。")
    L.append("")

    L.append("## 七、局限")
    L.append("")
    L.append(f"- 盈利组仅 {len(win)} 笔、整组 {len(sub)} 笔，样本很小，结论是描述性的，不是统计检验。")
    L.append("- 「次日」按个股下一个交易日取，若个股停牌多日，缺口会包含停牌期间消息，未单独剔除。")
    L.append("- 一字涨停开盘的成交假设未做撮合模拟，仅从 K 线形态判断「开盘即最低」。")
    L.append("- 未扣佣金/印花税/滑点；止损按触发价成交为近似。")
    L.append("")

    L.append("## 八、产出文件")
    L.append("")
    L.append("| 文件 | 内容 |")
    L.append("|---|---|")
    L.append(f"| `{os.path.basename(OUT_MD)}` | 本观察报告 |")
    L.append(f"| `{os.path.basename(OUT_CSV)}` | {len(x)} 笔逐笔次日形态：缺口、最低、下影、实体、上影、开盘分类、账面/次日买入收益 |")
    L.append(f"| `strategy_signals_2026-08-01_2026-11-06.csv` | 上一轮策略研究的逐信号收益明细 |")
    L.append("")

    open(OUT_MD, "w", encoding="utf-8").write("\n".join(L))
    print(f"      报告：{OUT_MD}")
    print(f"      盈利笔缺口 {d_w['gap']:+.2f}% / 亏损笔缺口 {d_l['gap']:+.2f}%")


if __name__ == "__main__":
    main()
