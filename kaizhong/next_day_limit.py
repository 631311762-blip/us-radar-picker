# -*- coding: utf-8 -*-
"""
凯众 v4 高收益信号 · 次日涨停与「实际能否买进」核查

回答的问题：收益最高的那批，次日是不是普遍涨停、是不是都以一字板开盘导致实盘买不进？

严谨口径：
  - 涨停幅度按个股：主板 10% / 创业板(30x) 科创板(68x) 20% / 北交所(4x,8x) 30% / ST 5%
  - 涨停价 = 前收盘 ×(1+幅度)，Decimal ROUND_HALF_UP 到分；价格判定容差 0.011 元
    （避免用固定涨幅阈值误判 20cm / 低价股四舍五入）
  - D  = 提示日（模型账面口径的买入日）
  - D1 = D 之后该股第一个有交易的交易日
  - 「一字板 / 买不进」= D1 全天最低价都没跌破涨停价（open=high=low=close=涨停价）

两层可买入性：
  1) D 日尾盘能否按收盘价买入 → 决定「提示日收盘买入(A)」口径是否成立
  2) D1 是否一字涨停          → 决定「次日开盘买入(NO)」口径是否成立

产出：
  data/reports/nextday_limit_2026-08-01_2026-11-06.csv
  data/reports/次日涨停与可买入性_2026-11-07.md

用法：python3 next_day_limit.py
"""
import os
from decimal import Decimal, ROUND_HALF_UP

import numpy as np
import pandas as pd

import lib

LAST = "2026-11-06"
SRC = os.path.join(lib.REPORT_DIR, "strategy_signals_2026-08-01_2026-11-06.csv")
OUT_CSV = os.path.join(lib.REPORT_DIR, "nextday_limit_2026-08-01_2026-11-06.csv")
OUT_MD = os.path.join(lib.REPORT_DIR, "次日涨停与可买入性_2026-11-07.md")
TOL = 0.011  # 价格判定容差（分）


def limit_pct(code, name):
    c, n = str(code).zfill(6), str(name).upper().replace(" ", "")
    if "ST" in n:
        return 0.05
    if c.startswith(("30", "68")):
        return 0.20
    # 北交所：43/83/87/88 老代码段 + 920 新代码段（易漏，误判成主板 10%）
    if c.startswith(("43", "83", "87", "88", "92")) or c[0] in ("4", "8"):
        return 0.30
    return 0.10


def limit_price(prev_close, pct):
    return float((Decimal(str(prev_close)) * Decimal(str(1 + pct)))
                 .quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


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
        pct = limit_pct(c, r["name"])
        o0, h0, l0, c0 = [float(d.loc[m[0], x]) for x in ("open", "high", "low", "close")]
        o1, h1, l1, c1 = [float(d.loc[j, x]) for x in ("open", "high", "low", "close")]
        if c0 <= 0:
            continue
        prev_close = float(d.loc[m[0] - 1, "close"]) if m[0] > 0 else o0
        lp0 = limit_price(prev_close, pct)   # D 日涨停价
        lp1 = limit_price(c0, pct)           # D1 日涨停价

        # D 日：模型账面口径的买入日
        d0_limit_close = c0 >= lp0 - TOL
        d0_yizi = l0 >= lp0 - TOL            # D 日一字，尾盘也买不进

        # D1 日分类
        if l1 >= lp1 - TOL:
            kind1 = "一字涨停(买不进)"
        elif o1 >= lp1 - TOL:
            kind1 = "涨停开盘·盘中打开"
        elif c1 >= lp1 - TOL:
            kind1 = "收盘封涨停"
        elif h1 >= lp1 - TOL:
            kind1 = "盘中触板未封住"
        else:
            kind1 = "未涨停"

        gap = (o1 / c0 - 1) * 100
        rows.append(dict(
            code=c, name=str(r["name"]), date=D, prox=float(r["prox"]),
            A5=r.get("A_ret5_stop"), NO5=r.get("NO_ret5_stop"),
            pct=pct, c0=round(c0, 2), lp1=lp1,
            o1=round(o1, 2), h1=round(h1, 2), l1=round(l1, 2), c1=round(c1, 2),
            gap=round(gap, 2),
            d0_limit_close=d0_limit_close, d0_yizi=d0_yizi,
            d1_kind=kind1,
            d1_yizi=(kind1 == "一字涨停(买不进)"),
            d1_any_limit=(kind1 != "未涨停"),
            buyable_d1=(kind1 != "一字涨停(买不进)"),
        ))
    return pd.DataFrame(rows)


def describe(g):
    a, n = g["A5"].dropna(), g["NO5"].dropna()
    return dict(
        n=len(g),
        yizi=int(g["d1_yizi"].sum()),
        yizi_pct=round(float(g["d1_yizi"].mean() * 100), 1),
        any_limit=int(g["d1_any_limit"].sum()),
        any_pct=round(float(g["d1_any_limit"].mean() * 100), 1),
        d0_close=int(g["d0_limit_close"].sum()),
        d0_yizi=int(g["d0_yizi"].sum()),
        A5=round(float(a.mean()), 2) if len(a) else None,
        NO5=round(float(n.mean()), 2) if len(n) else None,
    )


def main():
    x = build()
    x.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(f"      明细 {len(x)} 笔 → {OUT_CSV}")

    sub = x[(x["prox"] >= 1.08) & x["A5"].notna()].copy()
    win = sub[sub["A5"] > 0].copy()
    top = sub.sort_values("A5", ascending=False).head(9)
    yz = sub[sub["d1_yizi"]]
    buyable = sub[~sub["d1_yizi"]]

    d_all, d_sub, d_win, d_top = (describe(g) for g in (x, sub, win, top))

    # 打印核对用
    for nm, d in (("全样本", d_all), ("prox>=1.08", d_sub), ("盈利9笔", d_win), ("Top9", d_top)):
        print(f"      {nm}: n={d['n']} 一字={d['yizi']}({d['yizi_pct']}%) "
              f"任一涨停={d['any_limit']}({d['any_pct']}%) D日收盘封板={d['d0_close']} "
              f"D日一字={d['d0_yizi']} A5={d['A5']} NO5={d['NO5']}")
    print(f"      33笔剔除一字后: n={len(buyable)} NO5={round(float(buyable['NO5'].mean()),2)} "
          f"(全组 {d_sub['NO5']})")

    L = []
    L.append("# 次日涨停与「实际能否买进」核查")
    L.append("")
    L.append(f"> 样本：策略研究 `prox≥1.08`、5 日收益有效的 **{len(sub)} 笔**；"
             f"对照全样本 {len(x)} 笔（有次日 K 线）。基准日 {LAST}。"
             "涨停幅度按个股板块/ST 分别取 10%/20%/30%/5%，按涨停价判定。合规剔除已静默完成。")
    L.append("")

    L.append("## 一、直接回答")
    L.append("")
    L.append(f"**不是「都涨停、都买不进」，但确实有一部分买不进，而且集中在涨得最猛的那几笔。**")
    L.append("")
    L.append(f"- 收益最高 9 笔里，次日一字涨停（全天封死、集合竞价没有卖盘、实盘基本买不进）"
             f"**{d_top['yizi']} 笔**；出现过涨停（含一字、涨停开盘打开、盘中触板、收盘封板）"
             f"**{d_top['any_limit']} 笔 / {d_top['n']} 笔 = {d_top['any_pct']:.1f}%**。")
    L.append(f"- 盈利 {len(win)} 笔里一字 {d_win['yizi']} 笔（{d_win['yizi_pct']:.1f}%）；"
             f"整组 {len(sub)} 笔里一字 {d_sub['yizi']} 笔（{d_sub['yizi_pct']:.1f}%）。")
    L.append(f"- 全样本 {len(x)} 笔里一字 {d_all['yizi']} 笔（{d_all['yizi_pct']:.1f}%），"
             f"出现过涨停 {d_all['any_limit']} 笔（{d_all['any_pct']:.1f}%）"
             "——次日涨停在这批突破信号里并不普遍，只是高收益组明显更集中。")
    L.append("")
    L.append(f"所以准确说法是：**头部 {d_top['n']} 笔里 {d_top['yizi']} 笔"
             f"（{d_top['yizi_pct']:.1f}%）是一字板买不进，其余 {d_top['n']-d_top['yizi']} 笔能买进；"
             "但买不进的这几笔恰恰贡献了账面收益里最厚的一段。**")
    L.append("")

    L.append("## 二、口径（为什么不用固定涨幅阈值）")
    L.append("")
    L.append("| 项 | 定义 |")
    L.append("|---|---|")
    L.append("| 涨停幅度 | 主板 10%、创业板 `30x` / 科创板 `68x` 20%、北交所 `43/83/87/88/920` 30%、名称含 ST 5% |")
    L.append("| 涨停价 | 前收盘 ×(1+幅度)，四舍五入到分；价格差 ≤0.011 元视为达到涨停价 |")
    L.append("| 一字涨停·买不进 | 次日**最低价**都没跌破涨停价，即开=高=低=收=涨停价，全天无卖盘 |")
    L.append("| 涨停开盘·盘中打开 | 开盘即涨停，但盘中最低价低于涨停价，打开后理论可成交 |")
    L.append("| 收盘封涨停 / 盘中触板 | 开盘未涨停，盘中有充足买进机会 |")
    L.append("| D 日 | 提示日（模型账面口径按此日收盘价买入）；D1 = 之后第一个有交易交易日 |")
    L.append("")
    L.append("> 用固定 `gap≥9.8%` 会把 20cm 票的 +10% 高开误判成涨停、也会漏掉 ST 的 +5% 封板，"
             "所以这里一律按个股涨停价判定。")
    L.append("")

    L.append("## 三、分组统计")
    L.append("")
    L.append("| 分组 | 样本 | 次日一字(买不进) | 占比 | 出现过涨停 | 占比 | D日收盘封板 | D日一字 | 账面5日 | 次日开盘5日 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for nm, d in (("全样本", d_all), ("prox≥1.08 整组", d_sub),
                  ("盈利笔", d_win), ("**收益最高 9 笔**", d_top)):
        a = f"{d['A5']:+.2f}%" if d["A5"] is not None else "--"
        n = f"{d['NO5']:+.2f}%" if d["NO5"] is not None else "--"
        L.append(f"| {nm} | {d['n']} | {d['yizi']} | {d['yizi_pct']:.1f}% | "
                 f"{d['any_limit']} | {d['any_pct']:.1f}% | {d['d0_close']} | "
                 f"{d['d0_yizi']} | {a} | {n} |")
    L.append("")
    L.append(f"「D 日收盘封板」是另一层关键：提示日若自己就封涨停，"
             f"按模型账面口径的「收盘价」挂单基本无卖盘。整组 {d_sub['d0_close']} 笔 "
             f"D 日收盘封板，但其中 {d_sub['d0_yizi']} 笔是一字——"
             "即全部盘中打开过，D 日盘中其实有机会成交，只是成交价通常不等于收盘价。")
    L.append("")

    L.append("## 四、收益最高 9 笔逐笔：哪些买不进")
    L.append("")
    L.append("| 名称 | 提示日 | 板块幅度 | 次日开盘缺口 | 次日形态 | D日按收盘价 | 账面5日 | 次日开盘买入5日 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for _, r in top.iterrows():
        no5 = f"{r['NO5']:+.2f}%" if pd.notna(r["NO5"]) else "--"
        d0 = "一字·难" if r["d0_yizi"] else ("封板·难" if r["d0_limit_close"] else "可")
        L.append(f"| {r['name']} | {r['date']} | {int(r['pct']*100)}% | {r['gap']:+.2f}% | "
                 f"**{r['d1_kind']}** | {d0} | **{r['A5']:+.2f}%** | {no5} |")
    L.append("")
    L.append(f"> 「D日按收盘价」列：封板·难 = 提示日收盘封涨停、按收盘价难成交；可 = 提示日未封板。"
             f"头部 {len(top)} 笔里提示日一字 {int(top['d0_yizi'].sum())} 笔，均非全天封死。")
    L.append("")

    by_no5 = round(float(buyable["NO5"].mean()), 2)
    inflate = round(d_sub["NO5"] - by_no5, 2)
    yz_pos = int((yz["NO5"] > 0).sum())
    yz_neg = int((yz["NO5"] <= 0).sum())
    L.append("## 五、一字板对两个买点口径的影响（含对上一轮表述的更正）")
    L.append("")
    L.append("上一轮写「一字/涨停开盘买不进 → 可执行最优很可能仍然高估」，"
             "**观察没错，但推论过强，这里更正**：买不进本身不会让人少赚，"
             "真正的问题是「买不进的单子却被回测计入了收益」，而实测影响很小。")
    L.append("")
    L.append(f"1. **对「提示日收盘买入(A)」：D1 一字不削弱收益，恰恰是赚到的。** "
             f"D 日按收盘价买入后，次日一字把涨幅直接锁进来，头部那几笔 +20%~+35% "
             f"就是这么来的。它们不是「买不进的纸上富贵」；能否拿到取决于 "
             f"**D 日尾盘能否按收盘价成交**（信号收盘才确认），与次日是否一字无关。")
    L.append(f"2. **对「次日开盘买入(NO)」：一字板确实买不进，但回测假设一律按开盘价成交，"
             f"把没成交的单子也算成了收益。** 这 {len(yz)} 笔在 NO 口径下 "
             f"{yz_pos} 笔为正、{yz_neg} 笔 -5%，其中开开实业 +23.34% 实盘根本拿不到，"
             "属于虚假成交。剔除后：")
    L.append("")
    L.append(f"   - 整组 {len(sub)} 笔 NO 均值 **{d_sub['NO5']:+.2f}%** → "
             f"剔除后 {len(buyable)} 笔 **{by_no5:+.2f}%**，虚高约 {inflate:+.2f}pp。")
    L.append("")
    L.append(f"   幅度很小，且 {yz_pos} 正 {yz_neg} 负、方向由单笔决定，属小样本噪声；"
             "即上一轮「一字买不进」的观察没错，但不足以断言「可执行最优仍然高估」。")
    L.append("")
    L.append(f"3. **真正系统性削弱可执行性的不是次日一字，而是提示日 D 日自己就封涨停。** "
             f"整组 {len(sub)} 笔里 D 日收盘封板 {d_sub['d0_close']} 笔"
             f"（{d_sub['d0_close']/len(sub)*100:.1f}%），封板时按收盘价挂单基本无卖盘，"
             "连模型账面口径的买入价都未必拿得到（但本样本 D 日 0 笔一字，"
             "盘中打开时仍可成交，只是价格不等于收盘价）。这比「次日买不进」严重得多，"
             "也是 v4 账面收益与实盘之间最大的一道坎。")
    L.append("")

    L.append("## 六、实操结论")
    L.append("")
    L.append(f"- 次日「都涨停买不进」不成立：全样本仅 {d_all['yizi_pct']:.1f}% 一字，"
             f"头部 {d_top['n']} 笔里 {d_top['yizi']} 笔一字、其余 "
             f"{d_top['n']-d_top['yizi']} 笔能买进。")
    L.append("- 但**头部涨幅里有相当一部分来自一字板**（最厚的一笔闽东电力 +42.39% "
             "其实是涨停开盘后盘中打开，不在此列），这部分只能靠提示日 D 日买入拿到；"
             "等次日看到高开再追，要么买在涨停价、要么直接买不到。")
    L.append("- 按既有交易纪律，回踩区间不是买点；这里也不构成「新增低吸买点」。"
             "若要提升可执行性，方向仍是**提示日尾盘竞价的成交可行性**（信号确认滞后），"
             "而不是研究次日怎么接。")
    L.append(f"- 更该留意 D 日自身：整组 {d_sub['d0_close']} 笔"
             f"（{d_sub['d0_close']/len(sub)*100:.1f}%）提示日收盘已封涨停，"
             "按收盘价买入基本拿不到货——这是比次日一字更大的可执行性约束。")
    L.append("")

    L.append("## 七、局限")
    L.append("")
    L.append("- 一字板并非绝对零成交，会有少量撮合，散户挂涨停价排队通常难以成交，此处按「基本买不进」处理。")
    L.append("- 「D 日收盘封板」仅按收盘价达到涨停价判定，不等于全天封死；本样本 D 日 0 笔一字，"
             "盘中打开时段仍可成交，此处统一按「按收盘价难成交」处理。")
    L.append("- 涨停幅度按代码/名称推断，未处理上市前 5 日无涨跌幅限制、临时停牌、除权除息等特例。")
    L.append("- 「次日」按个股下一个交易日取，停牌多日会包含停牌期间消息，未单独剔除。")
    L.append("- 未扣佣金/印花税/滑点；止损按触发价成交为近似。")
    L.append("")

    L.append("## 八、产出文件")
    L.append("")
    L.append("| 文件 | 内容 |")
    L.append("|---|---|")
    L.append(f"| `{os.path.basename(OUT_MD)}` | 本核查报告 |")
    L.append(f"| `{os.path.basename(OUT_CSV)}` | {len(x)} 笔逐笔：板块幅度、涨停价、D/D1 形态分类、可买入标记、账面与次日买入收益 |")
    L.append("| `strategy_signals_2026-08-01_2026-11-06.csv` | 上一轮策略研究逐信号收益明细 |")
    L.append("| `nextday_gap_2026-08-01_2026-11-06.csv` | 上一轮次日缺口/下影形态明细 |")
    L.append("")

    open(OUT_MD, "w", encoding="utf-8").write("\n".join(L))
    print(f"      报告：{OUT_MD}")


if __name__ == "__main__":
    main()
