# -*- coding: utf-8 -*-
r"""
奥雅式「试盘→破位→放量突破」主升浪模型 · 设计与回测（2026-11-18）
====================================================================
按用户 6 点要求设计：
  1) 前期横盘箱体；
  2) 放量试盘（压力线=试盘日最高价），失败后最好破位洗盘（加分项）；
  3) 提示日=放量突破箱体：盘中最高价≥试盘压力线，成交量≥试盘日，次日温和放量；
  4) 首推优先（同一波只记首次提示日）+ 后续 5/10 日跟踪；
  5) 吸收现有 v3.1 的 9 因子横截面打分作为排序层（形态准入 + 打分优选）；
  6) 回溯 2026 年全市场，对比多方案的 5/10 日准确率与收益率，看哪个最优。

方案：
  A 现有v3.1        —— 基准（收盘 brk 口径 + 形态闸门 + Top6）
  B 奥雅-严格        —— 试盘+破位+盘中突破+量能确认+打分
  C 奥雅-标准        —— 试盘+盘中突破+打分（破位可选）
  D 奥雅-收盘口径    —— 同 C 但用收盘突破替代盘中突破（验证用户观点）
  E 奥雅-纯形态      —— 纯形态，按突破力度/量能排序（不打分）

口径与 feedback.settle_one 一致：g5/g10=其后 5/10 日最高价相对提示日收盘的最大浮盈%，
r5=持有 5 日实际收益（跌破突破日最低×0.96 止损）。

只读 kline_cache.json / factor_fit.json，不改任何模型文件、权重、holdout、状态文件。
"""
import os, sys, json, math, statistics, argparse
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import model2 as M
import features as F


def _find(fname):
    p = os.path.join(HERE, fname)
    return p if os.path.exists(p) else os.path.join(ROOT, fname)


CACHE = json.load(open(_find("kline_cache.json"), encoding="utf-8"))["data"]
W = json.load(open(os.path.join(HERE, "factor_fit.json"), encoding="utf-8"))["weights"]
FACTORS = list(W.keys())
TOT = sum(W.values())
CODES = list(CACHE.keys())
NAME = {}
_up = _find("universe_cache.json")
if os.path.exists(_up):
    NAME = {p["code"]: p["name"] for p in
            json.load(open(_up, encoding="utf-8"))["stocks"]}

# 预计算每票前20日均量（不含当日），加速量比
VOLMA20 = {}
for c, b in CACHE.items():
    n = len(b)
    ma = [0.0] * n
    s = 0.0
    for j in range(n):
        ma[j] = s / j if j >= 20 else 0.0
        if j >= 20:
            s += b[j - 20]["volume"]
            s -= b[j - 20]["volume"] if False else 0  # 下面用显式窗口重算，避免误差
    # 显式重算（清晰优先）
    for j in range(20, n):
        ma[j] = sum(x["volume"] for x in b[j - 20:j]) / 20.0
    VOLMA20[c] = ma

# 交易日日历（取最长序列）
_ref = max((b for b in CACHE.values() if b), key=lambda b: len(b))
CAL = [x["date"] for x in _ref]
IDX = {d: i for i, d in enumerate(CAL)}
# 回测区间：2026 全年可用（历史窗口 120 根 + 留 10 日观察窗）
DAYS = [d for d in CAL if d >= "2026-01-01" and IDX[d] >= 120 and IDX[d] <= len(CAL) - 11]


# ---------- 形态识别 ----------
def find_probe(b, i, vr_ma, prm):
    """在 i 之前 [look_back, look_fwd] 窗口找放量冲高回落的试盘日，返回最近一次。
    返回 dict(day, high=压力线, vol, fall, vr) 或 None"""
    lo = max(prm["probe_look_back"], 20)
    start = max(lo, i - prm["probe_look_fwd"])
    end = i - prm["probe_min_gap"]
    best = None
    for j in range(end, start - 1, -1):
        if vr_ma[j] <= 0:
            continue
        vr = b[j]["volume"] / vr_ma[j]
        if vr < prm["probe_vr"]:
            continue
        pc = b[j - 1]["last"]
        rise = (b[j]["high"] / pc - 1) * 100          # 盘中冲高
        fall = (b[j]["high"] - b[j]["last"]) / b[j]["high"] * 100  # 回落/上影
        if rise < prm["probe_rise"] or fall < prm["probe_fall"]:
            continue
        best = dict(day=j, date=b[j]["date"], high=b[j]["high"],
                    vol=b[j]["volume"], vr=round(vr, 2),
                    rise=round(rise, 1), fall=round(fall, 1))
        break  # 取最近一次试盘
    return best


def is_box(b, j, prm):
    """试盘日 j 之前是否横盘箱体：振幅(最高-最低)/均价 ≤ 阈值"""
    start = max(0, j - prm["box_len"])
    seg = b[start:j + 1]
    if len(seg) < prm["box_len"] * 0.7:
        return False, None
    hi = max(x["high"] for x in seg); lo = min(x["low"] for x in seg)
    avg = sum(x["last"] for x in seg) / len(seg)
    amp = (hi - lo) / avg * 100
    return amp <= prm["box_amp"], dict(amp=round(amp, 1), hi=round(hi, 2), lo=round(lo, 2))


def had_breakdown(b, pday, i, boxlo, prm):
    """试盘之后、提示日之前是否破位洗盘。
    支撑线取试盘日最低价与箱底中较高者（贴近用户口径：跌破试盘日形成的支撑，
    如奥雅 10/19 最低 21.30 跌破 9/7-9/8 试盘支撑 ~23.6）。"""
    support = max(boxlo, b[pday]["low"])
    low_after = min(b[j]["low"] for j in range(pday + 1, i))
    if low_after < support * (1 - prm["break_tol"] / 100.0):
        return True, round((low_after / support - 1) * 100, 1)
    return False, None


def detect(code, i, prm):
    """判断 b[i] 是否为奥雅式提示日；返回信号 dict 或 None"""
    b = CACHE[code]; vr_ma = VOLMA20[code]
    if i < 120 or i >= len(b) - 1:
        return None
    c0 = b[i]["last"]; prev = b[i - 1]["last"]
    chg = (c0 / prev - 1) * 100
    vr = b[i]["volume"] / vr_ma[i] if vr_ma[i] else 0
    if chg < prm["sig_chg"] or vr < prm["sig_vr"]:
        return None
    # 可买：非涨停
    if chg >= M.board_limit(code) - 0.5:
        return None
    probe = find_probe(b, i, vr_ma, prm)
    if not probe:
        return None
    # 突破判定：盘中 or 收盘
    if prm["break_mode"] == "intraday":
        ok_break = b[i]["high"] >= probe["high"] * (1 - prm["break_tol"] / 100.0)
    else:
        ok_break = c0 >= probe["high"] * (1 - prm["break_tol"] / 100.0)
    if not ok_break:
        return None
    if b[i]["volume"] < probe["vol"] * prm["vol_vs_probe"]:
        return None
    inbox, box = is_box(b, probe["day"], prm)
    if not inbox:
        return None
    bd, bd_pct = had_breakdown(b, probe["day"], i, box["lo"], prm)
    if prm["require_breakdown"] and not bd:
        return None
    # 次日量能确认（若需要且有数据）
    nxt_ratio = None
    if i + 1 < len(b) and vr_ma[i + 1] > 0:
        nxt_ratio = b[i + 1]["volume"] / b[i]["volume"]
        if prm["require_confirm"] and not (prm["conf_lo"] <= nxt_ratio <= prm["conf_hi"]):
            return None
    return dict(code=code, date=b[i]["date"], day=i, close=c0, chg=round(chg, 2),
                vr=round(vr, 2), probe_date=probe["date"], pressure=round(probe["high"], 2),
                probe_vr=probe["vr"], vol_vs_probe=round(b[i]["volume"] / probe["vol"], 2),
                break_pct=round((b[i]["high"] / probe["high"] - 1) * 100, 2),
                box_amp=box["amp"], breakdown=bd, bd_pct=bd_pct,
                nxt_vol_ratio=round(nxt_ratio, 2) if nxt_ratio else None)


# ---------- 评估 ----------
def evaluate(sig, prm):
    code = sig["code"]; b = CACHE[code]; i = sig["day"]
    c0 = b[i]["last"]
    if i + 10 >= len(b):
        return None
    seg5 = b[i + 1:i + 6]; seg10 = b[i + 1:i + 11]
    g5 = (max(x["high"] for x in seg5) / c0 - 1) * 100
    g10 = (max(x["high"] for x in seg10) / c0 - 1) * 100
    d10 = (min(x["low"] for x in seg10) / c0 - 1) * 100
    stop = min(x["low"] for x in b[max(0, i - 2):i + 1]) * 0.96  # 突破日附近最低×0.96
    sell = None
    for j in range(i + 1, i + 6):
        if b[j]["last"] < stop:
            sell = b[j]["last"]; break
    r5 = ((sell if sell else b[i + 5]["last"]) / c0 - 1) * 100
    return dict(**sig, g5=round(g5, 2), g10=round(g10, 2), d10=round(d10, 2),
                r5=round(r5, 2), hit20=1 if g10 >= 20 else 0,
                win5=1 if r5 > 0 else 0)


# ---------- 打分排序（吸收 v3.1 优点）----------
def attach_score(sigs, d):
    """对某日信号集合挂 9 因子横截面百分位打分（用当日全市场分布）"""
    if not sigs:
        return sigs
    rows = []
    for code in CODES:
        b = CACHE[code]
        idx = None
        for s in sigs:
            if s["code"] == code:
                idx = _day_index(code, d); break
        if idx is None or idx < 150:
            continue
        f = F.factors_at(b, idx, code)
        if not f:
            continue
        rows.append(f)
    if len(rows) < 500:
        for s in sigs:
            s["score"] = None
        return sigs
    maps = {k: sorted(r[k] for r in rows) for k in FACTORS}
    fmap = {}
    for code in set(s["code"] for s in sigs):
        b = CACHE[code]; idx = _day_index(code, d)
        if idx and idx >= 150:
            f = F.factors_at(b, idx, code)
            if f:
                fmap[code] = sum(W[k] * F.percentile(maps[k], f[k]) for k in FACTORS) / TOT * 100
    for s in sigs:
        s["score"] = round(fmap.get(s["code"], 0), 2)
    return sigs


IDXMAP = {c: {x["date"]: j for j, x in enumerate(b)} for c, b in CACHE.items()}
def _day_index(code, d):
    return IDXMAP[code].get(d)


# ---------- 方案参数 ----------
BASE = dict(
    box_len=25, box_amp=28.0,
    probe_look_back=25, probe_look_fwd=45, probe_min_gap=3,
    probe_vr=2.0, probe_rise=3.0, probe_fall=2.0,
    sig_chg=5.0, sig_vr=2.0, vol_vs_probe=0.9, break_tol=1.0,
    require_breakdown=False, require_confirm=False, conf_lo=0.7, conf_hi=2.2,
    break_mode="intraday", use_score=True,
)
SCHEMES = {
    "B_奥雅严格": dict(BASE, require_breakdown=True, require_confirm=True),
    "C_奥雅标准": dict(BASE),
    "D_奥雅收盘口径": dict(BASE, break_mode="close"),
    "E_奥雅纯形态": dict(BASE, use_score=False),
    # F=最优：次日温和放量确认收紧到 1.0~2.0 倍（用户第3点"放量但没放太大"）
    "F_奥雅最优": dict(BASE, require_breakdown=True, require_confirm=True,
                   conf_lo=1.0, conf_hi=2.0),
}


def scan_day(args):
    d, prm = args
    sigs = []
    for code in CODES:
        idx = _day_index(code, d)
        if idx is None:
            continue
        s = detect(code, idx, prm)
        if s:
            sigs.append(s)
    return sigs


def baseline_v31(days):
    """方案A：现有 v3.1 主升浪（收盘 brk 口径 + 形态闸门 + 新鲜度分层 + Top6），同口径基准"""
    recs = []
    import shape_gate as SG
    for d in days:
        rows = []
        for code in CODES:
            idx = _day_index(code, d)
            if idx is None or idx < 199:
                continue
            b = CACHE[code]
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
            rows.append(f)
        if len(rows) < 1000:
            continue
        maps = {k: sorted(r[k] for r in rows) for k in FACTORS}
        for r in rows:
            r["score"] = sum(W[k] * F.percentile(maps[k], r[k]) for k in FACTORS) / TOT * 100
            lim = M.board_limit(r["code"])
            cap = 8.0 if lim < 15 else (15.0 if lim < 25 else 22.0)
            r["buyable"] = r["day_chg"] < cap
        kept = []
        for r in rows:
            bad, _, _ = SG.judge(r, CACHE[r["code"]], _day_index(r["code"], d))
            if not bad:
                kept.append(r)
        def tier(x):
            ds = x.get("dsc", 99)
            return 0 if ds <= 1 else (1 if ds <= 3 else 2)
        cand = sorted([r for r in kept if r["buyable"]], key=lambda x: (tier(x), -x["score"]))
        mains = [x for x in cand if x.get("board") not in ("创业板", "科创板")]
        cybs = [x for x in cand if x.get("board") in ("创业板", "科创板")]
        top = mains[:3] + cybs[:3]
        for p in top:
            _bi = _day_index(p["code"], d)
            ev = evaluate(dict(code=p["code"], day=_bi, date=d,
                               close=CACHE[p["code"]][_bi]["last"]), BASE)
            if ev:
                recs.append(ev)
    return recs


def stat(label, recs):
    if not recs:
        return dict(label=label, n=0)
    return dict(label=label, n=len(recs),
                r5=round(statistics.mean(x["r5"] for x in recs), 2),
                win5=round(sum(x["win5"] for x in recs) / len(recs) * 100, 1),
                g5=round(statistics.mean(x["g5"] for x in recs), 2),
                g10=round(statistics.mean(x["g10"] for x in recs), 2),
                hit20=round(sum(x["hit20"] for x in recs) / len(recs) * 100, 1),
                d10=round(statistics.mean(x["d10"] for x in recs), 2))


def diag_oya():
    prm = BASE
    print("=== 奥雅股份 sz300949 形态诊断 ===")
    for d in ("2026-09-07", "2026-10-19", "2026-10-21", "2026-10-22"):
        idx = _day_index("sz300949", d)
        s = detect("sz300949", idx, prm)
        if s:
            print(f"  {d} 命中提示日: chg={s['chg']}% vr={s['vr']} 压力线={s['pressure']} "
                  f"突破={s['break_pct']}% 量/试盘={s['vol_vs_probe']} 破位={s['breakdown']} "
                  f"箱振={s['box_amp']}% 次日量比={s['nxt_vol_ratio']}")
        else:
            b = CACHE["sz300949"]; c0 = b[idx]["last"]
            vr = b[idx]["volume"] / VOLMA20["sz300949"][idx]
            print(f"  {d} 未命中: 收盘{c0} 量比{vr:.2f} (试盘/突破形态未齐)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--diag", action="store_true", help="只做奥雅单票诊断")
    ap.add_argument("--days", type=int, default=0, help="只跑最近 N 个交易日")
    args = ap.parse_args()

    diag_oya()
    if args.diag:
        return

    days = DAYS[-args.days:] if args.days else DAYS
    print(f"\n回溯 {len(days)} 个交易日：{days[0]} ~ {days[-1]}（全市场 {len(CODES)} 只）", flush=True)

    results = {}
    print("\n--- 扫描方案 A_现有v3.1（基准）...", flush=True)
    recs_a = baseline_v31(days)
    results["A_现有v3.1"] = recs_a
    json.dump(recs_a, open(os.path.join(HERE, "oya_A_现有v3.1.json"), "w", encoding="utf-8"),
              ensure_ascii=False)
    sa = stat("A_现有v3.1", recs_a)
    print(f"  信号 {sa['n']} 条 | 5日收益 {sa['r5']}% 胜率{sa['win5']}% | "
          f"g10 {sa['g10']}% 命中率{sa['hit20']}%", flush=True)

    for sname, prm in SCHEMES.items():
        print(f"\n--- 扫描方案 {sname} ...", flush=True)
        allsig = []
        with ProcessPoolExecutor(max_workers=6) as ex:
            for d, sigs in zip(days, ex.map(scan_day, [(dd, prm) for dd in days])):
                for s in sigs:
                    s["_d"] = d
                allsig.extend(sigs)
        # 首推去重：同一只票 30 日内只保留首次提示日
        allsig.sort(key=lambda s: (s["code"], s["date"]))
        seen = {}
        first = []
        for s in allsig:
            last = seen.get(s["code"])
            if last and (IDX[s["date"]] - IDX[last] ) <= 30:
                continue
            seen[s["code"]] = s["date"]
            first.append(s)
        # 评估
        recs = []
        for s in first:
            r = evaluate(s, prm)
            if r:
                recs.append(r)
        if prm["use_score"]:
            byday = defaultdict(list)
            for r in recs:
                byday[r["date"]].append(r)
            for d, rs in byday.items():
                attach_score(rs, d)
        results[sname] = recs
        json.dump(recs, open(os.path.join(HERE, f"oya_{sname}.json"), "w", encoding="utf-8"),
                  ensure_ascii=False)
        s = stat(sname, recs)
        print(f"  信号 {s['n']} 条 | 5日收益 {s['r5']}% 胜率{s['win5']}% | "
              f"g10 {s['g10']}% 命中率{s['hit20']}%", flush=True)

    print("\n" + "=" * 108)
    print(f"{'方案':<18}{'N':<7}{'5日收益%':<11}{'5日胜率%':<11}{'g5%':<9}"
          f"{'g10%':<9}{'命中率%':<10}{'d10%':<9}")
    print("=" * 108)
    for name, recs in results.items():
        s = stat(name, recs)
        print(f"{name:<18}{s['n']:<7}{s['r5']:<11}{s['win5']:<11}{s['g5']:<9}"
              f"{s['g10']:<9}{s['hit20']:<10}{s['d10']:<9}")
    print("=" * 108)


if __name__ == "__main__":
    main()
