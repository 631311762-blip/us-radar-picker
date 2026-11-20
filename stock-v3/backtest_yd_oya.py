# -*- coding: utf-8 -*-
r"""
易点「试盘/提示」 + 奥雅「放量突破主升浪」组合回测（2026-11-21）
================================================================
用户诉求：前面有易点模型下试盘，后续有奥雅模型主升浪，回溯准确率与收益率。

无人值守默认口径（在报告中声明）：
  · 奥雅主升浪 = 线上 v5-oya 的 detect() 命中日（涨停纳入，与当前推送口径一致）。
  · 易点前置信号做两套口径对比（因易点归档无完整历史信号、缓存缺换手率）：
      YD-上影试盘：易点 watch_signals 明命名的『上影试盘』(长上影冲高回落)，
                   窗口收紧到突破前 10/20 日（45日过于宽松无区分度，已弃用）。
      YD-提示点近似：易点 hard_filter 可K线化的核心(阳线+涨幅0.5~22%+量比≥1.4
                   +站上MA20×0.95+距MA20≤12%+近5日涨幅≤45%)，缺换手率故为近似。
  · 买点/收益起点 = 奥雅突破日；易点信号作"前置过滤/共振确认"。
  · 口径 = 2026年以来，首推30日去重，T+5/T+10 收益、胜率、最高浮盈、命中率。
  · 参照组：纯奥雅(线上口径) / 纯奥雅(带F方案次日量比1~2确认，即当初回测验证口径)。

只读共同历史行情库，不改任何模型权重 / 状态文件。
"""
import os, sys, json, statistics
from concurrent.futures import ProcessPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import oya_model as O
import backtest_oya as BO

CACHE = BO.CACHE
VOLMA20 = BO.VOLMA20
DAYS = BO.DAYS
IDX = BO.IDX
IDXMAP = BO.IDXMAP
CODES = BO.CODES
NAME = BO.NAME
P = O.PARAMS


def ma(b, n, end):
    """b[end] 之前(含) n 日均线"""
    if end + 1 < n:
        return None
    return sum(x["last"] for x in b[end - n + 1:end + 1]) / n


# ---------- 奥雅突破信号（线上口径）----------
def scan_stock(code):
    b = CACHE[code]; ma20v = VOLMA20[code]; n = len(b); out = []
    for i in range(120, n - 11):
        if b[i]["date"] < "2026-01-01":
            continue
        ev = O.detect(code, b, i, P, ma=ma20v)
        if not ev:
            continue
        sig = dict(code=code, date=b[i]["date"], day=i, close=b[i]["last"],
                   probe_date=ev["oya_probe_date"], is_limit_up=ev["is_limit_up"])
        r = BO.evaluate(sig, P)
        if not r:
            continue
        # 次日量比（F方案确认参照）
        r["nxt_ratio"] = round(b[i + 1]["volume"] / b[i]["volume"], 2) if i + 1 < len(b) else None
        out.append(r)
    return out


# ---------- 易点信号 ----------
def yd_upper_shadow(b, j):
    """易点『上影试盘』：长上影冲高回落"""
    o = b[j]["open"]; c = b[j]["last"]; h = b[j]["high"]; l = b[j]["low"]
    bh = max(o, c)
    if bh <= 0:
        return False
    upper = h - bh; lower = bh - l
    return h >= bh * 1.02 and upper >= lower * 0.8


def yd_point(b, j, ma20v=None):
    """易点『阳线启动买点近似』——直接委托生产 O.yd_start，确保回测与线上口径一致。
    量比=当日量/前5日均量(易点2026-11-20修正口径)。"""
    return O.yd_start(b, j)


def yd_before(code, break_date, kind, back):
    """突破日前 back 日内是否出现易点前置信号，返回 (日期, 间隔日) 或 (None,None)"""
    b = CACHE[code]; ma20v = VOLMA20[code]
    bi = IDXMAP[code].get(break_date)
    if bi is None:
        return None, None
    fn = yd_upper_shadow if kind == "shadow" else (lambda bb, jj: yd_point(bb, jj, ma20v))
    for j in range(max(60, bi - back), bi):
        if fn(b, j):
            return b[j]["date"], bi - j
    return None, None


def eval_from_yd(code, yd_date, break_day):
    """补充：若从易点前置信号日就买入，持有5/10日收益 + 信号→突破涨幅。"""
    b = CACHE[code]
    yi = IDXMAP[code].get(yd_date)
    if yi is None or yi + 10 >= len(b):
        return None
    c0 = b[yi]["last"]
    g5 = (max(x["high"] for x in b[yi + 1:yi + 6]) / c0 - 1) * 100
    g10 = (max(x["high"] for x in b[yi + 1:yi + 11]) / c0 - 1) * 100
    d10 = (min(x["low"] for x in b[yi + 1:yi + 11]) / c0 - 1) * 100
    r5 = (b[yi + 5]["last"] / c0 - 1) * 100
    pre_break = (b[break_day]["last"] / c0 - 1) * 100 if break_day > yi else None
    return dict(g5=round(g5, 2), g10=round(g10, 2), d10=round(d10, 2),
                r5=round(r5, 2), win5=1 if r5 > 0 else 0,
                hit20=1 if g10 >= 20 else 0,
                pre_break=round(pre_break, 2) if pre_break else None)


def stat(name, recs):
    if not recs:
        return dict(name=name, n=0)
    return dict(name=name, n=len(recs),
                r5=round(statistics.mean(x["r5"] for x in recs), 2),
                win5=round(sum(x["win5"] for x in recs) / len(recs) * 100, 1),
                g5=round(statistics.mean(x["g5"] for x in recs), 2),
                g10=round(statistics.mean(x["g10"] for x in recs), 2),
                hit20=round(sum(x["hit20"] for x in recs) / len(recs) * 100, 1),
                d10=round(statistics.mean(x["d10"] for x in recs), 2))


def main():
    print(f"数据: {len(CACHE)}只 | 回测 {DAYS[0]}~{DAYS[-1]} 共{len(DAYS)}日", flush=True)
    print("扫描奥雅突破信号...", flush=True)
    with ProcessPoolExecutor(max_workers=4) as ex:
        allsigs = []
        for res in ex.map(scan_stock, CODES):
            allsigs.extend(res)
    allsigs.sort(key=lambda s: (s["code"], s["date"]))
    seen = {}; sigs = []
    for s in allsigs:
        last = seen.get(s["code"])
        if last and (IDX[s["date"]] - IDX[last]) <= 30:
            continue
        seen[s["code"]] = s["date"]
        sigs.append(s)
    print(f"奥雅突破信号(去重后) {len(sigs)} 条", flush=True)

    # 参照组
    f_conf = [s for s in sigs if s["nxt_ratio"] is not None and 1.0 <= s["nxt_ratio"] <= 2.0]
    groups = []
    groups.append(stat("0-奥雅突破(线上全部)", sigs))
    groups.append(stat("1-奥雅突破(F确认1~2)", f_conf))

    # 组合方案：(名称, kind, 窗口)
    schemes = [
        ("2-上影试盘≤10日", "shadow", 10),
        ("3-上影试盘≤20日", "shadow", 20),
        ("4-易点提示点≤10日", "point", 10),
        ("5-易点提示点≤20日", "point", 20),
    ]
    detail_all = {}
    yd_buy_summary = {}
    for name, kind, back in schemes:
        combo = []
        for s in sigs:
            yd, gap = yd_before(s["code"], s["date"], kind, back)
            if yd:
                s["_yd"] = yd; s["_gap"] = gap
                combo.append(s)
        groups.append(stat(name, combo))
        # 前置信号日买入的补充收益（仅提示点口径最有意义）
        yd_buy = []
        for s in combo:
            v = eval_from_yd(s["code"], s["_yd"], s["day"])
            if v:
                yd_buy.append(v)
        yd_buy_summary[name] = stat(name + " [前置日买]", yd_buy)
        gaps = [s["_gap"] for s in combo]
        detail_all[name] = dict(
            n=len(combo),
            gap_median=statistics.median(gaps) if gaps else None,
            detail=[dict(code=s["code"], name=NAME.get(s["code"], ""), date=s["date"],
                         yd=s["_yd"], gap=s["_gap"], close=s["close"],
                         r5=s["r5"], g10=s["g10"], win5=s["win5"],
                         is_limit_up=s.get("is_limit_up"))
                    for s in sorted(combo, key=lambda x: -x["r5"])],
        )

    # 打印主表（突破日买入）
    print("\n" + "=" * 100)
    print("【买点 = 奥雅突破日】")
    print(f"{'方案':<26}{'N':<6}{'5日收益%':<11}{'5日胜率%':<11}{'g5%':<9}{'g10%':<9}{'命中率%':<10}{'d10%':<9}")
    print("=" * 100)
    for x in groups:
        if x["n"] == 0:
            print(f"{x['name']:<26}0"); continue
        print(f"{x['name']:<26}{x['n']:<6}{x['r5']:<11}{x['win5']:<11}"
              f"{x['g5']:<9}{x['g10']:<9}{x['hit20']:<10}{x['d10']:<9}")
    print("=" * 100)
    # 打印补充表（前置日买入）
    print("\n【买点 = 易点前置信号日（对照）】")
    print(f"{'方案':<26}{'N':<6}{'5日收益%':<11}{'5日胜率%':<11}{'g5%':<9}{'g10%':<9}{'命中率%':<10}{'d10%':<9}")
    print("=" * 100)
    for k, x in yd_buy_summary.items():
        if x["n"] == 0:
            print(f"{k:<26}0"); continue
        print(f"{k:<26}{x['n']:<6}{x['r5']:<11}{x['win5']:<11}"
              f"{x['g5']:<9}{x['g10']:<9}{x['hit20']:<10}{x['d10']:<9}")
    print("=" * 100)

    out = dict(window=f"{DAYS[0]}~{DAYS[-1]}", base_date=DAYS[-1],
               total_sigs=len(sigs), groups=groups, yd_buy_summary=yd_buy_summary,
               detail_all=detail_all,
               disclaimer="奥雅=v5-oya线上detect(涨停纳入);易点上影试盘=长上影冲高回落;易点提示点=阳线+量比≥1.4+站上MA20等(近似,缺换手率);买点突破日;首推30日去重;T+5/T+10。")
    json.dump(out, open(os.path.join(HERE, "yd_oya_combo_result.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\n明细/统计已写入 yd_oya_combo_result.json", flush=True)


if __name__ == "__main__":
    main()
