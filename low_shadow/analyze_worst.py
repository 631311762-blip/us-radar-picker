# -*- coding: utf-8 -*-
"""2026 全年满足模型样本 · 亏损最差5只共同特征分析 + 剔除规则模拟。

输入 bt_2026_result.json（当前生产口径 8道+H5，1023条，含前向收益）。
大盘环境用新浪指数日K(HTTP)计算：当日等权近似用上证指数涨跌 + 近5日动量。
输出结构化 JSON 供报告使用。
"""
import sys, os, json, statistics, bisect
import requests, urllib3
from collections import defaultdict

urllib3.disable_warnings()
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(REPO, "low_shadow", "state")
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
     "Referer": "https://finance.sina.com.cn/"}
KL_URL = "http://quotes.sina.cn/cn/api/jsonp_v2.php/var%20t=/CN_MarketDataService.getKLineData"

d = json.load(open(os.path.join(OUT, "bt_2026_result.json"), encoding="utf-8"))
picks = d["picks"]
DAYS = d["days"]


def get_idx():
    r = requests.get(KL_URL, params={"symbol": "sh000001", "scale": 240, "ma": "no",
                     "datalen": 250}, headers=H, timeout=15)
    s = r.text.strip(); i = s.find("([")
    arr = json.loads(s[i + 1:s.rfind(")")])
    return {x["day"]: float(x["close"]) for x in arr}, sorted(x["day"] for x in arr)


IDX, IDX_DAYS = get_idx()


def idx_chg(day):
    """当日指数涨跌%。"""
    if day not in IDX:
        return None
    i = IDX_DAYS.index(day)
    if i < 1:
        return None
    prev = IDX[IDX_DAYS[i - 1]]
    return round((IDX[day] / prev - 1) * 100, 3) if prev > 0 else None


def idx_r5(day):
    """近5日指数动量%（前5交易日收盘→当日收盘）。"""
    if day not in IDX:
        return None
    i = IDX_DAYS.index(day)
    if i < 5:
        return None
    return round((IDX[day] / IDX[IDX_DAYS[i - 5]] - 1) * 100, 3)


def pctile(val, arr):
    """val 在 arr 中的分位（0-100，越大越靠后/越高）。"""
    if val is None or not arr:
        return None
    return round(100.0 * sum(1 for a in arr if a <= val) / len(arr), 1)


# 全体有效样本（5日）特征数组
valid5 = [x for x in picks if x["rets"].get("5") is not None]
arr = {k: [x[k] for x in valid5] for k in
       ("upper_amp", "upper_to_body", "vol_ratio", "pos_pct", "amount", "pct_chg", "score")}


def enrich(x):
    z = x.copy()
    z["idx_chg"] = idx_chg(x["date"])
    z["idx_r5"] = idx_r5(x["date"])
    z["pct"] = {}
    for k in arr:
        z["pct"][k] = pctile(x.get(k), arr[k])
    return z


# 亏损最差5只（5日收益升序，唯一候选口径 = 用户核心持有期 T+3~5）
worst5 = sorted(valid5, key=lambda x: x["rets"]["5"])[:5]
worst5 = [enrich(x) for x in worst5]

# 全体基线统计
def stat(arrv):
    a = [v for v in arrv if v is not None]
    return {"mean": round(statistics.mean(a), 4), "median": round(statistics.median(a), 4),
            "p10": round(sorted(a)[int(0.1 * len(a))], 4),
            "p90": round(sorted(a)[int(0.9 * len(a))], 4)}


base_stats = {k: stat(v) for k, v in arr.items()}

# 最差5只共性：各特征分位 + 方向
common = {}
for k in ("upper_amp", "upper_to_body", "vol_ratio", "pos_pct", "amount", "pct_chg", "score"):
    vals = [w[k] for w in worst5]
    pcts = [w["pct"][k] for w in worst5]
    common[k] = {"vals": vals, "pcts": pcts,
                 "mean": round(statistics.mean(vals), 4),
                 "mean_pct": round(statistics.mean(pcts), 1)}

# 日期集中度：最差N只在7月上旬的占比 vs 全体
worst_date_conc = {}
for N in (5, 10, 20, 50):
    sub = sorted(valid5, key=lambda x: x["rets"]["5"])[:N]
    july = sum(1 for x in sub if "2026-07" in x["date"])
    worst_date_conc[str(N)] = {"july": july, "n": N, "pct": round(100.0 * july / N, 1)}
all_july = sum(1 for x in valid5 if "2026-07" in x["date"])
worst_date_conc["all"] = {"july": all_july, "n": len(valid5),
                          "pct": round(100.0 * all_july / len(valid5), 1)}

# 逐日环境：入选数 vs 当日指数涨跌（看7月是否信号暴增 + 暴跌）
daily_env = defaultdict(lambda: {"n": 0, "r5": []})
for x in valid5:
    daily_env[x["date"]]["n"] += 1
    daily_env[x["date"]]["r5"].append(x["rets"]["5"])
env_rows = []
for D in DAYS:
    if D not in IDX:
        continue
    e = daily_env.get(D)
    if not e or e["n"] < 3:
        continue
    env_rows.append({"date": D, "n": e["n"], "idx_chg": idx_chg(D), "idx_r5": idx_r5(D),
                     "r5_mean": round(statistics.mean(e["r5"]) * 100, 2)})

# 环境分档（当日指数涨跌）
def regime(c):
    if c is None:
        return "NA"
    if c <= -2.0:
        return "急跌"
    if c <= -0.5:
        return "下跌"
    if c < 0.5:
        return "平"
    return "上涨"


reg_group = defaultdict(lambda: {"n_picks": 0, "r5": []})
for x in valid5:
    reg = regime(idx_chg(x["date"]))
    reg_group[reg]["n_picks"] += 1
    if x["rets"].get("5") is not None:
        reg_group[reg]["r5"].append(x["rets"]["5"])
reg_summary = {}
for k, v in reg_group.items():
    reg_summary[k] = {"n": v["n_picks"], "r5_mean": round(statistics.mean(v["r5"]) * 100, 3),
                      "r5_hit": round(100.0 * sum(1 for r in v["r5"] if r > 0) / len(v["r5"]), 1),
                      "loss_gt15": sum(1 for r in v["r5"] if r < -0.15)}

# 剔除规则模拟：在 1023 条生产样本上评估各规则对 5日收益/样本数/最差5只命中
def eval_rule(name, fn):
    kept = [x for x in valid5 if not fn(x)]
    removed = [x for x in valid5 if fn(x)]
    r5 = [x["rets"]["5"] for x in kept]
    worst5_syms = {(w["date"], w["symbol"]) for w in worst5}
    removed_worst = sum(1 for x in removed if (x["date"], x["symbol"]) in worst5_syms)
    return {"rule": name, "n_kept": len(kept), "n_removed": len(removed),
            "r5_mean": round(statistics.mean(r5) * 100, 3),
            "r5_hit": round(100.0 * sum(1 for r in r5 if r > 0) / len(r5), 1),
            "removed_worst5": removed_worst,
            "worst_loss_removed": sum(1 for x in removed if x["rets"]["5"] < -0.15)}


rules = [
    ("基线(不剔除)", lambda x: False),
    ("剔除当日指数跌>2%(急跌)", lambda x: (idx_chg(x["date"]) or 0) <= -2.0),
    ("剔除当日指数跌>1%", lambda x: (idx_chg(x["date"]) or 0) <= -1.0),
    ("剔除近5日指数跌>5%(下跌趋势)", lambda x: (idx_r5(x["date"]) or 0) <= -5.0),
    ("剔除急跌 且 近5跌>5%", lambda x: (idx_chg(x["date"]) or 0) <= -2.0 and (idx_r5(x["date"]) or 0) <= -5.0),
    ("剔除成交额>8亿", lambda x: x["amount"] > 8e8),
    ("剔除上影/开<0.05(最小上影)", lambda x: x["upper_amp"] < 0.05),
    ("剔除评分<40(低分)", lambda x: x["score"] < 40),
]
rule_results = [eval_rule(name, fn) for name, fn in rules]

# ---- 补充：黄金样本对照 / 2x2交叉 / pos网格 / 规则H ----
goldens = {"300030": "2026-10-29", "688095": "2026-10-30",
           "688588": "2026-10-30", "600793": "2026-11-09"}
gold_rows = []
for s, dt in goldens.items():
    x = next((z for z in picks if z["symbol"] == s and z["date"] == dt), None)
    if x:
        gold_rows.append({"symbol": s, "name": x["name"], "date": dt,
                          "pos_pct": x["pos_pct"], "amount_yi": round(x["amount"] / 1e8, 2),
                          "score": x["score"],
                          "r5": round(x["rets"]["5"] * 100, 2) if x["rets"].get("5") is not None else None})


def st2(lst):
    r = [x["rets"]["5"] for x in lst]
    if not r:
        return {"n": 0, "mean": None, "hit": None, "loss10": None}
    return {"n": len(lst), "mean": round(statistics.mean(r) * 100, 2),
            "hit": round(100.0 * sum(1 for v in r if v > 0) / len(r), 1),
            "loss10": round(100.0 * sum(1 for v in r if v < -0.10) / len(r), 1)}


cross = {}
for hi in (False, True):
    for big in (False, True):
        sub = [x for x in valid5 if (x["pos_pct"] > 0.08) == hi and (x["amount"] > 5e8) == big]
        cross["pos>8=%s|amt>5e8=%s" % (hi, big)] = st2(sub)

pos_grid = []
for th in (0.06, 0.07, 0.08, 0.09, 0.10, 0.11, 0.12):
    kept = [x for x in valid5 if x["pos_pct"] <= th]
    rm = [x for x in valid5 if x["pos_pct"] > th]
    s5 = {(w["date"], w["symbol"]) for w in worst5}
    pos_grid.append({"th": th, "n_kept": len(kept), "n_removed": len(rm),
                     "stat": st2(kept), "hit_worst5": sum(1 for x in rm if (x["date"], x["symbol"]) in s5),
                     "gold_killed": sum(1 for g in gold_rows if g["pos_pct"] > th)})

# 当日指数涨跌注入 valid5 供规则H
for x in valid5:
    x["_chg"] = idx_chg(x["date"])

rule_extended = []
rule_defs = [
    ("高位8%+放量5亿", lambda x: x["pos_pct"] > 0.08 and x["amount"] > 5e8),
    ("高位8%+放量5亿+当日跌", lambda x: x["pos_pct"] > 0.08 and x["amount"] > 5e8 and (x["_chg"] or 0) < 0),
    ("高位10%+放量6亿", lambda x: x["pos_pct"] > 0.10 and x["amount"] > 6e8),
    ("高位9%+放量6亿", lambda x: x["pos_pct"] > 0.09 and x["amount"] > 6e8),
    ("高位8%+额8亿", lambda x: x["pos_pct"] > 0.08 and x["amount"] > 8e8),
    ("仅当日跌>1%", lambda x: (x["_chg"] or 0) < -0.01),
    ("H 高位8%+(放量5亿或跌1%)", lambda x: x["pos_pct"] > 0.08 and (x["amount"] > 5e8 or (x["_chg"] or 0) < -0.01)),
]
s5set = {(w["date"], w["symbol"]) for w in worst5}
for name, fn in rule_defs:
    rm = [x for x in valid5 if fn(x)]
    kept = [x for x in valid5 if not fn(x)]
    rule_extended.append({"rule": name, "n_removed": len(rm), "kept": st2(kept),
                          "huge_removed": sum(1 for x in rm if x["rets"]["5"] < -0.15),
                          "hit_worst5": sum(1 for x in rm if (x["date"], x["symbol"]) in s5set),
                          "gold_killed": [g["symbol"] for g in gold_rows
                                          if any(x["symbol"] == g["symbol"] for x in rm)]})

july_in = {"high_vol": st2([x for x in valid5 if "2026-07" in x["date"] and x["pos_pct"] > 0.08 and x["amount"] > 5e8]),
           "rest": st2([x for x in valid5 if "2026-07" in x["date"] and not (x["pos_pct"] > 0.08 and x["amount"] > 5e8)]),
           "july_all": st2([x for x in valid5 if "2026-07" in x["date"]]),
           "non_july": st2([x for x in valid5 if "2026-07" not in x["date"]])}

out = {
    "gold_rows": gold_rows, "cross": cross, "pos_grid": pos_grid,
    "rule_extended": rule_extended, "july_in": july_in,
    "generated_at": d["generated_at"],
    "universe": d["universe"], "days": len(DAYS),
    "range": [DAYS[0], DAYS[-1]],
    "n_prod": len(picks), "n_valid5": len(valid5),
    "base_stats": base_stats,
    "worst5": worst5,
    "common": common,
    "worst_date_conc": worst_date_conc,
    "env_rows": env_rows,
    "reg_summary": reg_summary,
    "rule_results": rule_results,
    "worst5_detail": [{
        "date": x["date"], "symbol": x["symbol"], "name": x["name"], "score": x["score"],
        "r1": round(x["rets"]["1"] * 100, 2) if x["rets"].get("1") is not None else None,
        "r3": round(x["rets"]["3"] * 100, 2) if x["rets"].get("3") is not None else None,
        "r5": round(x["rets"]["5"] * 100, 2),
        "r10": round(x["rets"]["10"] * 100, 2) if x["rets"].get("10") is not None else None,
        "upper_amp": x["upper_amp"], "upper_to_body": x["upper_to_body"],
        "vol_ratio": x["vol_ratio"], "pos_pct": x["pos_pct"],
        "amount_yi": round(x["amount"] / 1e8, 2), "pct_chg": x["pct_chg"],
        "idx_chg": x["idx_chg"], "idx_r5": x["idx_r5"], "pct": x["pct"],
    } for x in worst5],
}
jpath = os.path.join(OUT, "worst5_analysis.json")
json.dump(out, open(jpath, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("已保存", jpath, flush=True)

print("\n=== 最差5只 ===", flush=True)
for x in out["worst5_detail"]:
    print("%s %s %s | 5日%+.2f%% 10日%s | 上影%.4f 量比%.2f 距低%.3f 额%.2f亿 涨跌%+.2f%% | 指数当日%s%% 近5日%s%%" %
          (x["date"], x["symbol"], x["name"], x["r5"], x["r10"], x["upper_amp"],
           x["vol_ratio"], x["pos_pct"], x["amount_yi"], x["pct_chg"], x["idx_chg"], x["idx_r5"]), flush=True)

print("\n=== 最差5只特征分位(全体0-100) ===", flush=True)
for k, v in common.items():
    print("  %-16s 均值%.4f 平均分位%.1f 分位列表%s" %
          (k, v["mean"], v["mean_pct"], v["pcts"]), flush=True)

print("\n=== 日期集中度 ===", flush=True)
for k, v in worst_date_conc.items():
    print("  最差%s只: 7月 %d/%d (%.1f%%)" % (k, v["july"], v["n"], v["pct"]), flush=True)

print("\n=== 环境分档(当日指数涨跌) ===", flush=True)
for k, v in sorted(reg_summary.items()):
    print("  %-6s n=%4d 5日均%+.2f%% 胜率%.1f%% 亏损>15%%=%d" %
          (k, v["n"], v["r5_mean"], v["r5_hit"], v["loss_gt15"]), flush=True)

print("\n=== 剔除规则模拟 ===", flush=True)
for r in rule_results:
    print("  %-32s 留%4d 剔%4d 5日%+.3f%% 胜率%.1f%% 命中最差5=%d 剔除巨亏=%d" %
          (r["rule"], r["n_kept"], r["n_removed"], r["r5_mean"], r["r5_hit"],
           r["removed_worst5"], r["worst_loss_removed"]), flush=True)
