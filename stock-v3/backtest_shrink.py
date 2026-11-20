# -*- coding: utf-8 -*-
r"""
「中间缩量」规则影子回测（2026-11-20 用户要求）
====================================================================
研究起源：奥雅/冰山冷热/果麦文化三只票在【试盘日→突破日】中间，
无论涨跌成交量都很低（均量比 0.82~0.96，缩到试盘/突破日的 1/4），
疑似「抛压枯竭、筹码锁定」的共同规律。本脚本检验把它做成过滤条件
是否能提升 5/10 日收益与胜率。

口径：复用 backtest_oya 的历史行情库/形态识别/评估函数，对齐最新线上模型
（放开涨停闸门、require_breakdown=True、不做次日确认前置）。
所有方案在同一次扫描的信号上做子集过滤，保证公平对比。
严格离线：只读行情与因子文件，不改任何模型/权重/状态文件。
"""
import os, sys, json, statistics, argparse
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import backtest_oya as BT

CACHE = BT.CACHE
VOLMA20 = BT.VOLMA20
DAYS = BT.DAYS
IDX = BT.IDX
IDXMAP = BT.IDXMAP
CODES = BT.CODES
BASE = BT.BASE

# 线上首推口径 = BASE + 要求破位 + 不要求次日确认（次日量能只做跟踪）
ONLINE = dict(BASE, require_breakdown=True, require_confirm=False)


def mid_volume_stats(b, pday, i, vr_ma):
    """试盘日 pday 之后至提示日前一日的中间段量能特征（T日收盘全部可得）"""
    if i - pday < 4:            # 中间至少留 3 根K线
        return None
    vrs = [b[j]['volume'] / vr_ma[j] if vr_ma[j] else 0 for j in range(pday + 1, i)]
    vols = [b[j]['volume'] for j in range(pday + 1, i)]
    if not vrs:
        return None
    return dict(
        mid_vr=round(statistics.mean(vrs), 2),                       # 中间段平均量比
        mid_vs_probe=round(statistics.mean(vols) / b[pday]['volume'], 2),  # 中间均量/试盘量
        low_ratio=round(sum(1 for v in vrs if v < 1.5) / len(vrs), 2),     # 量比<1.5占比
        mid_days=len(vrs),
   )


def detect_online(code, i, prm):
    """对齐线上口径的提示日识别 + 中间缩量指标（放开涨停闸门）"""
    b = CACHE[code]; vr_ma = VOLMA20[code]
    if i < 120 or i >= len(b) - 1:
        return None
    c0 = b[i]['last']; prev = b[i - 1]['last']
    chg = (c0 / prev - 1) * 100
    vr = b[i]['volume'] / vr_ma[i] if vr_ma[i] else 0
    if chg < prm['sig_chg'] or vr < prm['sig_vr']:
        return None
    # 2026-11-20 新规：涨停突破日照样纳入，不在此剔除
    probe = BT.find_probe(b, i, vr_ma, prm)
    if not probe:
        return None
    if prm['break_mode'] == 'intraday':
        ok = b[i]['high'] >= probe['high'] * (1 - prm['break_tol'] / 100.0)
    else:
        ok = c0 >= probe['high'] * (1 - prm['break_tol'] / 100.0)
    if not ok:
        return None
    if b[i]['volume'] < probe['vol'] * prm['vol_vs_probe']:
        return None
    inbox, box = BT.is_box(b, probe['day'], prm)
    if not inbox:
        return None
    bd, bd_pct = BT.had_breakdown(b, probe['day'], i, box['lo'], prm)
    if prm['require_breakdown'] and not bd:
        return None
    mv = mid_volume_stats(b, probe['day'], i, vr_ma)
    if not mv:
        return None
    # 次日量能比（T+1 才可得，回测中可算；供 F 方案确认口径过滤）
    nxt_ratio = None
    if i + 1 < len(b) and b[i]['volume']:
        nxt_ratio = b[i + 1]['volume'] / b[i]['volume']
    return dict(code=code, date=b[i]['date'], day=i, close=c0, chg=round(chg, 2),
                vr=round(vr, 2), probe_date=probe['date'], pressure=round(probe['high'], 2),
                probe_vr=probe['vr'], vol_vs_probe=round(b[i]['volume'] / probe['vol'], 2),
                break_pct=round((b[i]['high'] / probe['high'] - 1) * 100, 2),
                box_amp=box['amp'], breakdown=bd, bd_pct=bd_pct,
                nxt_ratio=round(nxt_ratio, 2) if nxt_ratio else None, **mv)


def scan_day(args):
    d, prm = args
    sigs = []
    for code in CODES:
        idx = IDXMAP[code].get(d)
        if idx is None:
            continue
        s = detect_online(code, idx, prm)
        if s:
            sigs.append(s)
    return sigs


# ---------- 缩量规则（子集过滤） ----------
RULES = {
    "S0_线上基准": lambda s: True,
    "S1_均量比<1.2": lambda s: s['mid_vr'] < 1.2,
    "S2_中间比试盘<0.45": lambda s: s['mid_vr'] < 1.2 and s['mid_vs_probe'] < 0.45,
    "S3_三条件全上": lambda s: s['mid_vr'] < 1.2 and s['mid_vs_probe'] < 0.45 and s['low_ratio'] > 0.80,
    "S4_仅地量占比>80%": lambda s: s['low_ratio'] > 0.80,
}


def welch_t(a, b):
    """Welch t 检验：返回 (t, 近似p值)，比较两组均值差异"""
    if len(a) < 2 or len(b) < 2:
        return 0, 1
    ma, mb = statistics.mean(a), statistics.mean(b)
    va = statistics.variance(a) / len(a)
    vb = statistics.variance(b) / len(b)
    if va + vb == 0:
        return 0, 1
    t = (ma - mb) / math_sqrt(va + vb)
    # 正态近似双尾 p（样本量足够时近似）
    p = math_erfc(abs(t) / math_sqrt(2))
    return round(t, 3), round(p, 6)


def math_sqrt(x):
    return x ** 0.5


def math_erfc(x):
    # Abramowitz-Stegun 近似 erfc
    import math
    return math.erfc(x)


def build_recs(allsig, keep, confirm=None):
    """从全量信号里按规则过滤（+可选次日确认）→ 首推去重(30日) → 评估"""
    sigs = [s for s in allsig if keep(s)]
    if confirm is not None:
        lo, hi = confirm
        sigs = [s for s in sigs if s['nxt_ratio'] is not None and lo <= s['nxt_ratio'] <= hi]
    sigs.sort(key=lambda s: (s['code'], s['date']))
    seen = {}
    first = []
    for s in sigs:
        last = seen.get(s['code'])
        if last and (IDX[s['date']] - IDX[last]) <= 30:
            continue
        seen[s['code']] = s['date']
        first.append(s)
    recs = []
    for s in first:
        r = BT.evaluate(s, ONLINE)
        if r:
            recs.append(r)
    return recs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0, help="只跑最近 N 个交易日")
    args = ap.parse_args()

    days = DAYS[-args.days:] if args.days else DAYS
    print(f"回溯 {len(days)} 个交易日：{days[0]} ~ {days[-1]}（全市场 {len(CODES)} 只）", flush=True)
    print("形态口径：放开涨停 / 要求破位 / 盘中突破；对比两套：")
    print("  【A】线上现行：次日量能只跟踪不前置")
    print("  【B】F最优：次日量比 1.0~2.0 确认（T+1 回填）\n", flush=True)

    allsig = []
    with ProcessPoolExecutor(max_workers=6) as ex:
        for sigs in ex.map(scan_day, [(d, ONLINE) for d in days]):
            allsig.extend(sigs)
    print(f"全量扫描命中（含重复）{len(allsig)} 条\n", flush=True)

    # 两套口径 × 各缩量规则
    CONTEXTS = {
        "A_线上无确认": None,
        "B_F确认1.0-2.0": (1.0, 2.0),
    }
    results = {}
    for ctx, confirm in CONTEXTS.items():
        print(f"\n########## 口径 {ctx} ##########", flush=True)
        for name, keep in RULES.items():
            key = f"{ctx}|{name}"
            recs = build_recs(allsig, keep, confirm=confirm)
            results[key] = recs
            st = BT.stat(key, recs)
            if st['n']:
                print(f"{name:<22} N={st['n']:<5} r5={st['r5']:>6}% win5={st['win5']:>5}% "
                      f"g5={st['g5']:>6}% g10={st['g10']:>6}% hit20={st['hit20']:>5}% d10={st['d10']:>6}%", flush=True)
            else:
                print(f"{name:<22} N=0", flush=True)

    print("\n" + "=" * 122)
    print(f"{'口径|方案':<40}{'N':<7}{'5日收益%':<12}{'5日胜率%':<12}{'g5%':<10}{'g10%':<10}{'命中率%':<11}{'d10%':<10}")
    print("=" * 122)
    for ctx in CONTEXTS:
        for name in RULES:
            key = f"{ctx}|{name}"
            recs = results[key]
            st = BT.stat(key, recs)
            if not st['n']:
                continue
            print(f"{key:<40}{st['n']:<7}{st['r5']:<12}{st['win5']:<12}{st['g5']:<10}"
                  f"{st['g10']:<10}{st['hit20']:<11}{st['d10']:<10}")
        print("-" * 122)

    # 显著性：各口径内 S0 基准 vs 缩量方案
    print("\n显著性检验（各口径内 vs S0基准，Welch t / 双尾p）：")
    for ctx in CONTEXTS:
        base = results[f"{ctx}|S0_线上基准"]
        print(f"  [{ctx}]")
        for name in RULES:
            if name == "S0_线上基准":
                continue
            recs = results[f"{ctx}|{name}"]
            if not recs or not base:
                continue
            t5, p5 = welch_t([x['r5'] for x in recs], [x['r5'] for x in base])
            t10, p10 = welch_t([x['g10'] for x in recs], [x['g10'] for x in base])
            print(f"    {name:<22} r5: t={t5:>7} p={p5:<9} | g10: t={t10:>7} p={p10}")

    # 落盘全量结果供报告引用
    dump = {k: results[k] for k in results}
    json.dump(dump, open(os.path.join(HERE, "shrink_result.json"), "w", encoding="utf-8"),
              ensure_ascii=False)

    # 三只目标票的中间缩量指标
    print("\n目标样本中间缩量指标：")
    for code, d in [('sz300949', '2026-10-21'), ('sz000530', '2026-10-13'), ('sz301052', '2026-10-12')]:
        sig = [s for s in allsig if s['code'] == code and s['date'] == d]
        if sig:
            s = sig[0]
            print(f"  {s['date']} {code}: 试盘{s['probe_date']} 中间均量比={s['mid_vr']} "
                  f"中间/试盘={s['mid_vs_probe']} 地量占比={s['low_ratio']} 次日量比={s['nxt_ratio']}")
        else:
            print(f"  {d} {code}: 未命中形态口径")


if __name__ == "__main__":
    main()
