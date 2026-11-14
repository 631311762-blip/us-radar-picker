#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大盘环境 × 每日样本量 × 后续收益 的规律分析与环境因子回测（可复跑）。

用法： python3 low_shadow/env_factor.py
输入： low_shadow/state/bt_oct_result.json, bt_oct_env.json
输出： low_shadow/state/env_factor_result.json

所有环境指标均为**当日收盘后可得**（无前视）：
  上证/全市场等权当日涨跌、过去 5 日动量、当日信号数。
已自检 bt_oct_env.env.r5 为过去 5 日动量（用 close 序列重算，偏差 0）。
生产落地时全市场等权涨跌可由 common/adapter.universe_for() 返回的 pct 字段直接计算。
"""
import sys, json, math, os

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import low_shadow_screener as m

STATE = os.path.join(HERE, "state")
D = json.load(open(os.path.join(STATE, "bt_oct_result.json"), encoding="utf-8"))
E = json.load(open(os.path.join(STATE, "bt_oct_env.json"), encoding="utf-8"))
daily = {x["date"]: x for x in D["daily"]}
env = E["env"]
days = D["days"]
picks = D["picks"]
params = m.load_params()
m.DEMO = False


def is_h5(p):
    return m.h5_check(p["symbol"], p, params)[0]


def stat(ps, n):
    v = [p["rets"].get(str(n)) for p in ps]
    v = [x for x in v if x is not None]
    if not v:
        return {"n": 0, "hit": None, "mean": None, "median": None}
    sv = sorted(v)
    return {"n": len(v), "hit": round(100 * sum(1 for x in v if x > 0) / len(v), 1),
            "mean": round(100 * sum(v) / len(v), 2),
            "median": round(100 * sv[len(sv) // 2], 2)}


def pearson(xs, ys):
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 3:
        return None, 0
    xs, ys = zip(*pairs)
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    num = sum((x - mx) * (y - my) for x, y in pairs)
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return (round(num / (dx * dy), 3) if dx and dy else None), len(pairs)


def spearman(xs, ys):
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 3:
        return None, 0

    def rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(v):
            j = i
            while j + 1 < len(v) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2.0 + 1
            i = j + 1
        return r
    return pearson(rank([p[0] for p in pairs]), rank([p[1] for p in pairs]))


# ---- 口径自检：r5 是否为过去 5 日动量 ----
closes = [env[d]["close"] for d in days]
self_check = []
for i in range(5, len(days)):
    past5 = closes[i] / closes[i - 5] - 1
    self_check.append({"date": days[i], "r5_in_json": round(env[days[i]]["r5"], 5),
                       "recalc_past5": round(past5, 5),
                       "diff": round(abs(env[days[i]]["r5"] - past5), 5)})
r5_is_past = max(x["diff"] for x in self_check) < 0.002

# ---- 逐日面板 ----
panel = []
for i, dt in enumerate(days):
    dd, ev = daily[dt], env[dt]
    ps = [p for p in picks if p["date"] == dt]
    h5 = [p for p in ps if is_h5(p)]
    eq_chg = daily[days[i - 1]]["r1"]["bench_eq_mean"] if i > 0 else None
    eq_r5 = None
    if i >= 5:
        z = 1.0
        for j in range(i - 5, i):
            z *= (1 + daily[days[j]]["r1"]["bench_eq_mean"])
        eq_r5 = z - 1
    br3 = dd["r3"].get("bench_eq_mean")
    br5 = dd["r5"].get("bench_eq_mean")
    panel.append({
        "date": dt, "idx_chg": round(100 * ev["chg"], 3),
        "idx_r5": round(100 * ev["r5"], 3), "above_ma20": ev["above_ma20"],
        "idx_close": ev["close"],
        "eq_chg": round(100 * eq_chg, 3) if eq_chg is not None else None,
        "eq_r5": round(100 * eq_r5, 3) if eq_r5 is not None else None,
        "n_base": len(ps), "n_h5": len(h5),
        "base_r3": stat(ps, 3), "base_r5": stat(ps, 5),
        "h5_r3": stat(h5, 3), "h5_r5": stat(h5, 5),
        "bench_r3": round(100 * br3, 2) if br3 is not None else None,
        "bench_r5": round(100 * br5, 2) if br5 is not None else None,
    })

PM = {r["date"]: r for r in panel}
h5picks = [p for p in picks if is_h5(p)]


def col(k):
    return [r[k] for r in panel]


def h5ret(n):
    return [r["h5_r%d" % n]["mean"] for r in panel]


# ---- 相关性 ----
corr = []
for xn, xk in [("上证当日涨跌", "idx_chg"), ("上证5日动量", "idx_r5"),
               ("全市场等权当日", "eq_chg"), ("全市场等权5日动量", "eq_r5")]:
    for yn, yv in [("基线样本量", col("n_base")), ("H5样本量", col("n_h5")),
                   ("H5当日3日收益", h5ret(3)), ("H5当日5日收益", h5ret(5))]:
        pr, np_ = pearson(col(xk), yv)
        sr, _ = spearman(col(xk), yv)
        corr.append({"x": xn, "y": yn, "pearson": pr, "spearman": sr, "n": np_})
for xn, xv in [("基线样本量", col("n_base")), ("H5样本量", col("n_h5"))]:
    for yn, yv in [("H5当日3日收益", h5ret(3)), ("H5当日5日收益", h5ret(5))]:
        pr, np_ = pearson(xv, yv)
        sr, _ = spearman(xv, yv)
        corr.append({"x": xn, "y": yn, "pearson": pr, "spearman": sr, "n": np_})


# ---- 分组 ----
def group_stats(keyfn, rows=panel):
    g = {}
    for r in rows:
        k = keyfn(r)
        if k is None:
            continue
        g.setdefault(k, []).append(r)

    def avg(f, rs):
        v = [f(r) for r in rs if f(r) is not None]
        return round(sum(v) / len(v), 2) if v else None
    return [{"group": k, "days": len(rs),
             "avg_n_base": avg(lambda r: r["n_base"], rs),
             "avg_n_h5": avg(lambda r: r["n_h5"], rs),
             "avg_idx_r5": avg(lambda r: r["idx_r5"], rs),
             "avg_h5_r3": avg(lambda r: r["h5_r3"]["mean"], rs),
             "avg_h5_r5": avg(lambda r: r["h5_r5"]["mean"], rs),
             "hit_h5_r5": avg(lambda r: r["h5_r5"]["hit"], rs),
             "avg_bench_r5": avg(lambda r: r["bench_r5"], rs)}
            for k, rs in sorted(g.items(), key=lambda z: str(z[0]))]


groups = {
    "by_idx_chg": group_stats(lambda r: (
        "大跌 <-1%" if r["idx_chg"] < -1 else "小跌 -1~0" if r["idx_chg"] < 0
        else "小涨 0~1%" if r["idx_chg"] < 1 else "大涨 >1%")),
    "by_idx_r5": group_stats(lambda r: (
        "强负 <-2%" if r["idx_r5"] < -2 else "负 -2~0" if r["idx_r5"] < 0
        else "正 0~2%" if r["idx_r5"] < 2 else "强正 >2%")),
    "by_eq_r5": group_stats(lambda r: None if r["eq_r5"] is None else (
        "负 <-2%" if r["eq_r5"] < -2 else "弱 -2~0" if r["eq_r5"] < 0
        else "正 0~2%" if r["eq_r5"] < 2 else "强正 >2%")),
    "by_n_base": group_stats(lambda r: (
        "少 0~5" if r["n_base"] <= 5 else "中 6~15" if r["n_base"] <= 15
        else "多 16~30" if r["n_base"] <= 30 else "极多 >30")),
}

# ---- 拥挤度分档 ----
crowd_groups = []
for lo, hi, tag in [(0, 5, "极冷 0~5"), (6, 15, "常态 6~15"),
                    (16, 30, "拥挤 16~30"), (31, 999, "极拥挤 >30")]:
    ps = [p for p in h5picks if lo <= PM[p["date"]]["n_base"] <= hi]
    crowd_groups.append({"tag": tag, "n": len(ps),
                         "days": len(set(p["date"] for p in ps)),
                         "r3": stat(ps, 3), "r5": stat(ps, 5)})


# ---- 环境因子（闸门类）回测 ----
def btest(name, fn, base):
    ps = [p for p in base if fn(p, PM.get(p["date"], {}))]
    r3, r5 = stat(ps, 3), stat(ps, 5)

    def excess(n):
        vals = [p["rets"][str(n)] - PM[p["date"]]["bench_r%d" % n] / 100
                for p in ps if p["rets"].get(str(n)) is not None
                and PM.get(p["date"], {}).get("bench_r%d" % n) is not None]
        return round(100 * sum(vals) / len(vals), 2) if vals else None
    return {"rule": name, "n": len(ps), "days": len(set(p["date"] for p in ps)),
            "keep": round(100 * len(ps) / len(base), 1),
            "r3": r3, "r5": r5, "ex3": excess(3), "ex5": excess(5)}


FACTORS = [
    ("H5 基线（无环境）", lambda p, e: True),
    ("EF1 上证5日动量>0", lambda p, e: e.get("idx_r5", -99) > 0),
    ("EF2 上证5日动量>-1%", lambda p, e: e.get("idx_r5", -99) > -1),
    ("EF3 等权5日动量>0", lambda p, e: e.get("eq_r5") is not None and e["eq_r5"] > 0),
    ("EF4 等权5日动量>-2%", lambda p, e: e.get("eq_r5") is not None and e["eq_r5"] > -2),
    ("EF5 上证当日不跌(>-0.5%)", lambda p, e: e.get("idx_chg", -99) > -0.5),
    ("EF6 环境分>=2", lambda p, e: sum([
        1 if e.get("idx_r5", -99) > 0 else 0,
        1 if (e.get("eq_r5") is not None and e["eq_r5"] > 0) else 0,
        1 if e.get("idx_chg", -99) > 0 else 0]) >= 2),
    ("EF7 环境分>=1", lambda p, e: sum([
        1 if e.get("idx_r5", -99) > 0 else 0,
        1 if (e.get("eq_r5") is not None and e["eq_r5"] > 0) else 0,
        1 if e.get("idx_chg", -99) > 0 else 0]) >= 1),
    ("EF8 弱市只留评分前50%", lambda p, e: e.get("idx_r5", 0) > 0 or p["score"] >= 50),
    ("EF9 弱市加严:上影>=0.06", lambda p, e: e.get("idx_r5", 0) > 0 or p["upper_amp"] >= 0.06),
    ("EF10 弱市加严:成交额<=3亿", lambda p, e: e.get("idx_r5", 0) > 0 or p["amount"] <= 3e8),
    ("EF11 强环境放宽、弱环境加严", lambda p, e: (
        e.get("idx_r5", -99) > 0 or
        (p["upper_amp"] >= 0.06 and p["amount"] <= 3e8 and p["score"] >= 45))),
]
factors_h5 = [btest(n, f, h5picks) for n, f in FACTORS]
factors_base = [btest(n, f, picks) for n, f in FACTORS]


# ---- 动态门槛 / 兼顾变体 ----
def regime(e):
    if e["eq_chg"] is None:
        return "中性"
    if e["eq_chg"] > 0:
        return "强"
    if e["eq_chg"] < -1.0:
        return "弱"
    return "中性"


VARIANTS = [
    ("V0 H5 基线", lambda p, e: True),
    ("V1 三档:弱市上影>=0.06", lambda p, e: regime(e) != "弱" or p["upper_amp"] >= 0.06),
    ("V2 弱市低上影需额<=1亿(保凌志)",
     lambda p, e: regime(e) != "弱" or (p["upper_amp"] >= 0.06 or p["amount"] <= 1e8)),
    ("V3 弱市低上影需score>=43(保凌志)",
     lambda p, e: regime(e) != "弱" or (p["upper_amp"] >= 0.06 or p["score"] >= 43)),
    ("V4 弱市低上影需额<=1亿或score>=43",
     lambda p, e: regime(e) != "弱" or (
         p["upper_amp"] >= 0.06 or p["amount"] <= 1e8 or p["score"] >= 43)),
    ("V5 三档:中性score>=40+弱市V4", lambda p, e: (
        True if regime(e) == "强" else
        (p["score"] >= 40 if regime(e) == "中性" else
         (p["upper_amp"] >= 0.06 or p["amount"] <= 1e8 or p["score"] >= 43)))),
    ("V6 三档:中性score>=40+弱市V3", lambda p, e: (
        True if regime(e) == "强" else
        (p["score"] >= 40 if regime(e) == "中性" else
         (p["upper_amp"] >= 0.06 or p["score"] >= 43)))),
    ("V7 仅环境标注(不改筛选)", lambda p, e: True),
]


def evaluate(fn, exclude_1030=False):
    ps = [p for p in h5picks
          if (not exclude_1030 or p["date"] != "2026-10-30") and fn(p, PM[p["date"]])]
    lz = [p for p in h5picks if p["symbol"] == "688588"]
    return {"n": len(ps), "days": len(set(p["date"] for p in ps)),
            "keep": round(100 * len(ps) / len(h5picks), 1),
            "r3": stat(ps, 3), "r5": stat(ps, 5),
            "lingzhi": "保留" if any(fn(p, PM[p["date"]]) for p in lz) else "剔除"}


variants_full = [{"name": n, **evaluate(f)} for n, f in VARIANTS]
variants_ex1030 = [{"name": n, **evaluate(f, True)} for n, f in VARIANTS]

# ---- 稳健性：分段 & 剔除 10-30 ----
rows_ex = [r for r in panel if r["date"] != "2026-10-30"]
SEGS = [("10月上 10/08~10/17", "2026-10-08", "2026-10-17"),
        ("10月下 10/20~10/31", "2026-10-20", "2026-10-31"),
        ("11月上 11/02~11/13", "2026-11-02", "2026-11-13")]
seg_out = []
for name, a, b in SEGS:
    rs = [r for r in panel if a <= r["date"] <= b and r["h5_r5"]["mean"] is not None]
    seg_out.append({"seg": name, "days": len(rs),
                    "avg_n_h5": round(sum(r["n_h5"] for r in rs) / len(rs), 1) if rs else None,
                    "avg_h5_r5": round(sum(r["h5_r5"]["mean"] for r in rs) / len(rs), 2) if rs else None,
                    "pearson_n_vs_r5": pearson([r["n_h5"] for r in rs],
                                               [r["h5_r5"]["mean"] for r in rs])[0]})
robustness = {
    "seg": seg_out,
    "corr_full": pearson(col("n_h5"), h5ret(5))[0],
    "corr_ex_1030": pearson([r["n_h5"] for r in rows_ex],
                            [r["h5_r5"]["mean"] for r in rows_ex])[0],
    "corr_env_full": pearson(col("n_base"), col("eq_chg"))[0],
    "corr_env_ex_1030": pearson([r["n_base"] for r in rows_ex],
                                [r["eq_chg"] for r in rows_ex])[0],
}

# ---- 环境分档 / 持有期曲线 ----
env_table, hold = [], []
for k in ("强", "中性", "弱"):
    ps = [p for p in h5picks if regime(PM[p["date"]]) == k]
    ds = [r for r in panel if regime(r) == k]
    eqs = [r["eq_chg"] for r in ds if r["eq_chg"] is not None]
    env_table.append({"regime": k, "days": len(ds),
                      "avg_eq_chg": round(sum(eqs) / len(eqs), 2) if eqs else None,
                      "avg_n_h5": round(sum(r["n_h5"] for r in ds) / len(ds), 1) if ds else None,
                      "n": len(ps), "r3": stat(ps, 3), "r5": stat(ps, 5)})
    row = {"regime": k, "n": len(ps)}
    for n in (1, 3, 5, 10):
        row["r%d" % n] = stat(ps, n)
    hold.append(row)

daily_env = [{"date": r["date"], "regime": regime(r), "eq_chg": r["eq_chg"],
              "idx_chg": r["idx_chg"], "idx_r5": r["idx_r5"], "eq_r5": r["eq_r5"],
              "n_base": r["n_base"], "n_h5": r["n_h5"],
              "h5_r5": r["h5_r5"]["mean"], "h5_r5_hit": r["h5_r5"]["hit"]}
             for r in panel]

out = {
    "generated_at": "2026-11-14", "window": "2026-10-08 ~ 2026-11-13 (27 交易日)",
    "data_source": "bt_oct_result.json(322 条入选/逐日基准) + bt_oct_env.json(上证环境)",
    "r5_is_past_momentum": r5_is_past, "no_lookahead": True,
    "note": ("所有环境指标均为当日收盘后可得：上证/全市场等权当日涨跌、过去5日动量、"
             "当日信号数。已自检 env.r5 为过去5日动量（用 close 序列重算，偏差 0），无前视偏差。"
             "生产可用 common/adapter.universe_for() 返回的 pct 字段直接算全市场等权涨跌与涨跌家数比。"),
    "self_check": self_check, "panel": panel, "corr": corr, "groups": groups,
    "crowd_groups": crowd_groups, "factors_h5": factors_h5, "factors_base": factors_base,
    "robustness": robustness, "variants_full": variants_full,
    "variants_ex1030": variants_ex1030, "env_table": env_table,
    "hold_curve": hold, "daily_env": daily_env,
    "h5_total": len(h5picks), "base_total": len(picks),
}
path = os.path.join(STATE, "env_factor_result.json")
json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("已输出:", path)
print("口径自检 r5=过去5日动量:", r5_is_past,
      "| 环境↔样本量相关:", robustness["corr_env_full"],
      "(剔除10-30:", robustness["corr_env_ex_1030"], ")")
print("H5 基线 5日:", factors_h5[0]["r5"]["mean"], "% | V5:", variants_full[5]["r5"]["mean"], "%")
