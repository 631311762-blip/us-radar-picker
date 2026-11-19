# -*- coding: utf-8 -*-
r"""
形态闸门 A/B 回测工具（2026-11-10）
================================================================================
在真实 K 线上逐日重放 v3.1 选股，对比：
  A 组 = 原模型（无闸门）
  B 组 = 修正后（加形态闸门，含补位票）
  C 组 = 原模型选中、但被闸门剔除的票

用法：
  python backtest_shape_gate.py                # 全区间回测 + 汇总
  python backtest_shape_gate.py --days 20     # 只跑最近 20 个交易日（快速自检）
  python backtest_shape_gate.py --json out.json   # 另存逐条明细

只读缓存，不联网、不改任何模型文件 / 权重 / holdout / 状态文件。
口径与实盘 feedback.settle_one 一致（2026-11-10 用 21 条已结算样本交叉验证：
误差 <0.15pp 一致 21/21，平均绝对误差 0.002pp）。

依赖：共同历史行情库 common/state/market_daily、factor_fit.json。
"""
import os, sys, json, math, statistics, argparse
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import features as F
import model2 as M
import shape_gate as SG
import datafetch


def _find(fname):
    p = os.path.join(HERE, fname)
    return p if os.path.exists(p) else os.path.join(ROOT, fname)


CACHE = datafetch.load_market_cache(count=500)
W = json.load(open(os.path.join(HERE, "factor_fit.json"), encoding="utf-8"))["weights"]
FACTORS = list(W.keys())
TOT = sum(W.values())
CODES = list(CACHE.keys())
IDXMAP = {c: {x["date"]: j for j, x in enumerate(b)} for c, b in CACHE.items()}
NAME = {}
_up = _find("universe_cache.json")
if os.path.exists(_up):
    NAME = {p["code"]: p["name"] for p in
            json.load(open(_up, encoding="utf-8"))["stocks"]}

# 交易日历取一只 320 根且完整的票；>=200 根历史 + 留 10 日观察窗
_ref = max((b for b in CACHE.values() if b), key=lambda b: len(b))
CAL = [x["date"] for x in _ref]
DAYS = CAL[199:len(CAL) - 10]


def buyable(r):
    lim = M.board_limit(r["code"])
    cap = 8.0 if lim < 15 else (15.0 if lim < 25 else 22.0)
    return r["day_chg"] < cap


def tier(x):
    d = x.get("dsc", 99)
    return 0 if d <= 1 else (1 if d <= 3 else 2)


def quota(seq):
    mains = [x for x in seq if x.get("board") not in ("创业板", "科创板")]
    cybs = [x for x in seq if x.get("board") in ("创业板", "科创板")]
    final = mains[:3] + cybs[:3]
    if len(final) < 6:
        used = {x["code"] for x in final}
        rest = [x for x in mains[3:] + cybs[3:] if x["code"] not in used]
        final += rest[:6 - len(final)]
    return final


def build_rows(d):
    """基准日 d 的全市场因子行 + 横截面打分（与 每日推荐.build 同源）"""
    rows = []
    for code in CODES:
        b = CACHE[code]
        idx = IDXMAP[code].get(d)
        if idx is None or idx < 199:
            continue
        w = b[max(0, idx - 249):idx + 1]
        try:
            if max(x["high"] for x in w) <= min(x["low"] for x in w) * 1.5:
                continue
        except Exception:
            continue
        f = F.factors_at(b, idx, code)
        if not f:
            continue
        f["code"] = code
        f["name"] = NAME.get(code, "")
        rows.append(f)
    if len(rows) < 1000:
        return []
    maps = {k: sorted(r[k] for r in rows) for k in FACTORS}
    for r in rows:
        r["score"] = round(sum(W[k] * F.percentile(maps[k], r[k])
                               for k in FACTORS) / TOT * 100, 2)
    rows.sort(key=lambda x: -x["score"])
    return rows


def gate_split(rows, d):
    """用 shape_gate.judge 同口径判定；返回 (保留, 剔除)"""
    kept, excluded = [], []
    for r in rows:
        b = CACHE[r["code"]]
        idx = IDXMAP[r["code"]][d]
        bad, reasons, ev = SG.judge(r, b, idx)
        r["shape_bad"] = bad
        r["shape_reasons"] = reasons
        (excluded if bad else kept).append(r)
    return kept, excluded


def select(rows):
    latent = [x for x in rows if buyable(x)]
    fresh = sorted(latent, key=lambda x: (tier(x), -x["score"]))
    return quota(fresh)


def evaluate(p, d, group):
    b = CACHE[p["code"]]
    idx = IDXMAP[p["code"]][d]
    if idx + 10 >= len(b):
        return None
    c0 = b[idx]["last"]
    seg = b[idx + 1:idx + 11]
    g10 = (max(x["high"] for x in seg) / c0 - 1) * 100
    d10 = (min(x["low"] for x in seg) / c0 - 1) * 100
    g5 = (max(x["high"] for x in b[idx + 1:idx + 6]) / c0 - 1) * 100
    gap = (b[idx + 1]["open"] / c0 - 1) * 100
    can_buy = gap <= 7.0
    dsc = p.get("dsc", 99)
    bi = idx - dsc if dsc < 99 else idx
    stop = b[max(0, bi)]["low"] * 0.96
    sell, stopped = None, False
    for j in range(idx + 1, idx + 6):
        if b[j]["last"] < stop:
            sell, stopped = b[j]["last"], True
            break
    if sell is None:
        sell = b[min(idx + 5, len(b) - 1)]["last"]
    return dict(date=d, group=group, code=p["code"], name=p.get("name", ""),
                score=p["score"], dsc=dsc, g10=round(g10, 2), d10=round(d10, 2),
                g5=round(g5, 2), hit=1 if g10 >= 20 else 0,
                can_buy=can_buy, gap=round(gap, 2), stopped=stopped,
                r5=round((sell / c0 - 1) * 100, 2))


def run_day(d):
    rows = build_rows(d)
    if not rows:
        return []
    kept, excluded = gate_split(rows, d)
    reason_map = {x["code"]: x["shape_reasons"] for x in excluded}
    a = select(rows)                       # 原模型
    b = select(kept)                       # 修正后（含补位）
    a_codes, b_codes = {x["code"] for x in a}, {x["code"] for x in b}
    out = []
    for p in a:
        r = evaluate(p, d, "A")
        if r:
            r["in_B"] = p["code"] in b_codes
            r["shape_reasons"] = reason_map.get(p["code"], [])
            out.append(r)
    for p in b:
        if p["code"] not in a_codes:
            r = evaluate(p, d, "B_only")
            if r:
                r["in_B"] = True
                out.append(r)
    for p in a:
        if p["code"] not in b_codes:
            r = evaluate(p, d, "C")
            if r:
                r["in_B"] = False
                r["shape_reasons"] = reason_map.get(p["code"], [])
                out.append(r)
    return out


def st(name, g):
    if not g:
        return None
    r5 = [x["r5"] for x in g]
    return dict(group=name, n=len(g),
                r5=round(statistics.mean(r5), 2),
                r5_med=round(statistics.median(r5), 2),
                win=round(sum(1 for v in r5 if v > 0) / len(r5) * 100, 1),
                g10=round(statistics.mean(x["g10"] for x in g), 2),
                hit20=round(sum(x["hit"] for x in g) / len(g) * 100, 1),
                d10=round(statistics.mean(x["d10"] for x in g), 2),
                stopped=round(sum(1 for x in g if x["stopped"]) / len(g) * 100, 1))


def welch(a, b):
    se = math.sqrt(statistics.variance(a) / len(a) + statistics.variance(b) / len(b))
    t = (statistics.mean(a) - statistics.mean(b)) / se if se else 0
    return t, math.erfc(abs(t) / math.sqrt(2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0, help="只跑最近 N 个交易日")
    ap.add_argument("--json", default="", help="逐条明细另存路径")
    a = ap.parse_args()
    days = DAYS[-a.days:] if a.days else DAYS
    print(f"回溯 {len(days)} 个交易日：{days[0]} ~ {days[-1]}", flush=True)
    recs = []
    with ProcessPoolExecutor(max_workers=6) as ex:
        for i, r in enumerate(ex.map(run_day, days), 1):
            recs.extend(r)
            if i % 25 == 0:
                print(f"  {i}/{len(days)} 天，累计 {len(recs)} 条", flush=True)
    if a.json:
        json.dump(recs, open(a.json, "w", encoding="utf-8"), ensure_ascii=False)
    A = [x for x in recs if x["group"] == "A"]
    B = [x for x in recs if (x["group"] == "A" and x["in_B"]) or x["group"] == "B_only"]
    C = [x for x in recs if x["group"] == "C"]
    BO = [x for x in recs if x["group"] == "B_only"]
    print("\n" + "=" * 96)
    print(f"{'组别':<20}{'N':<7}{'5日收益%':<11}{'中位%':<10}{'胜率%':<9}"
          f"{'g10%':<10}{'命中率%':<10}{'d10%':<10}")
    print("=" * 96)
    for n, g in [("A 原模型", A), ("B 修正后", B), ("C 被剔除", C), ("补位票", BO)]:
        s = st(n, g)
        print(f"{n:<20}{s['n']:<7}{s['r5']:<11}{s['r5_med']:<10}{s['win']:<9}"
              f"{s['g10']:<10}{s['hit20']:<10}{s['d10']:<10}")
    print("=" * 96)
    if A and B:
        t, p = welch([x["r5"] for x in B], [x["r5"] for x in A])
        print(f"B−A 5日收益 {st('B',B)['r5']-st('A',A)['r5']:+.2f}pp  "
              f"Welch t={t:.2f} p={p:.4f}（{'显著' if p<0.05 else '不显著'}）")
    byday = defaultdict(lambda: dict(A=[], B=[]))
    for x in A:
        byday[x["date"]]["A"].append(x["r5"])
    for x in B:
        byday[x["date"]]["B"].append(x["r5"])
    changed = sum(1 for v in byday.values() if v["A"] != v["B"])
    print(f"主榜被闸门改动 {changed}/{len(byday)} 天（{changed/len(byday)*100:.1f}%）")
    print("\n注：结论与限制详见 回测_形态闸门_2026-11-10.md")


if __name__ == "__main__":
    main()
