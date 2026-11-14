#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复现报告中的改进规则，验证全窗口样本数，并计算 2026-11-13 各方案剩余清单。"""
import json

D = json.load(open("/workspace/us-radar-picker/low_shadow/state/bt_oct_result.json", encoding="utf-8"))
PICKS = D["picks"]


def is_bj(s):
    return s.startswith(("4", "8", "92"))


def is_gem_star(s):
    return s.startswith(("300", "301", "688", "689"))


RULES = {
    "基线（当前模型）": lambda p: True,
    "F1 上影>=0.08": lambda p: p["upper_amp"] >= 0.08,
    "F2 成交额>=2亿": lambda p: p["amount"] >= 2e8,
    "G1 成交额1~10亿": lambda p: 1e8 <= p["amount"] <= 1e9,
    "G3 1~10亿+排除北交所+排除跌>3%+量比<=3": lambda p: (
        1e8 <= p["amount"] <= 1e9 and not is_bj(p["symbol"])
        and p["pct_chg"] > -3.0 and p["vol_ratio"] <= 3.0),
    "G4 3~10亿+排除北交所+排除跌>3%": lambda p: (
        3e8 <= p["amount"] <= 1e9 and not is_bj(p["symbol"])
        and p["pct_chg"] > -3.0),
    "H2 保留0.045+排除北交所+跌>3%+量比>3+额<=10亿": lambda p: (
        not is_bj(p["symbol"]) and p["pct_chg"] > -3.0
        and p["vol_ratio"] <= 3.0 and p["amount"] <= 1e9),
    "H5 =H2+只留创业板/科创板": lambda p: (
        not is_bj(p["symbol"]) and p["pct_chg"] > -3.0
        and p["vol_ratio"] <= 3.0 and p["amount"] <= 1e9
        and is_gem_star(p["symbol"])),
    "C8 上影>=0.08+额>=2亿+排除北交所": lambda p: (
        p["upper_amp"] >= 0.08 and p["amount"] >= 2e8 and not is_bj(p["symbol"])),
}


def stat(ps, n):
    vals = [p["rets"].get(str(n)) for p in ps]
    vals = [v for v in vals if v is not None]
    if not vals:
        return {"n": 0, "hit": None, "mean": None}
    return {
        "n": len(vals),
        "hit": round(100.0 * sum(1 for v in vals if v > 0) / len(vals), 1),
        "mean": round(100.0 * sum(vals) / len(vals), 2),
    }


print("=" * 100)
print("【A】全窗口 2026-10-08 ~ 2026-11-13（27 交易日）各方案样本数校验")
print("=" * 100)
print("%-52s %6s %6s %8s %8s %8s %8s" % ("方案", "样本", "天数", "3日N", "3日命中", "3日均值", "5日均值"))
allres = {}
for tag, fn in RULES.items():
    ps = [p for p in PICKS if fn(p)]
    days = len(set(p["date"] for p in ps))
    r3, r5 = stat(ps, 3), stat(ps, 5)
    allres[tag] = {"n": len(ps), "days": days, "r3": r3, "r5": r5}
    print("%-52s %6d %6d %8d %7s%% %7s%% %7s%%" % (
        tag[:50], len(ps), days, r3["n"], r3["hit"], r3["mean"], r5["mean"]))

print()
print("=" * 100)
print("【B】2026-11-13 昨日 13 只，各改进方案下保留情况")
print("=" * 100)
yes = sorted([p for p in PICKS if p["date"] == "2026-11-13"],
             key=lambda x: x["score"], reverse=True)
print("基线昨日样本：%d 只" % len(yes))
print()
hdr = "%-4s %-8s %-8s %6s %7s %6s %8s %7s | " % (
    "序", "代码", "名称", "评分", "上影", "量比", "成交额", "涨跌")
tags_short = ["基线", "F1", "G3", "G4", "H2", "H5", "C8"]
print(hdr + " ".join("%4s" % t for t in tags_short))
key_map = {
    "基线": "基线（当前模型）",
    "F1": "F1 上影>=0.08",
    "G3": "G3 1~10亿+排除北交所+排除跌>3%+量比<=3",
    "G4": "G4 3~10亿+排除北交所+排除跌>3%",
    "H2": "H2 保留0.045+排除北交所+跌>3%+量比>3+额<=10亿",
    "H5": "H5 =H2+只留创业板/科创板",
    "C8": "C8 上影>=0.08+额>=2亿+排除北交所",
}
yres = {}
for t in tags_short:
    fn = RULES[key_map[t]]
    yres[t] = [p for p in yes if fn(p)]
for i, p in enumerate(yes, 1):
    marks = " ".join("%4s" % ("✓" if any(q["symbol"] == p["symbol"] for q in yres[t]) else "·")
                     for t in tags_short)
    print("%-4d %-8s %-8s %6.2f %7.4f %6.3f %7.2f亿 %6.2f%% | %s" % (
        i, p["symbol"], p["name"], p["score"], p["upper_amp"], p["vol_ratio"],
        p["amount"] / 1e8, p["pct_chg"], marks))
print()
print("%-52s %6s %6s  保留清单" % ("方案", "全窗", "昨日"))
for t in tags_short:
    k = key_map[t]
    names = "、".join("%s %s" % (q["symbol"], q["name"]) for q in yres[t]) or "（空仓）"
    print("%-52s %6d %6d  %s" % (t + "  →  " + k[:28], allres[k]["n"],
                                  len(yres[t]), names))

out = {
    "generated_at": "2026-11-14",
    "window": "2026-10-08 ~ 2026-11-13 (27 交易日)",
    "baseline_total": len(PICKS),
    "all": allres,
    "yesterday_date": "2026-11-13",
    "yesterday_baseline": [
        {k: p[k] for k in ("symbol", "name", "score", "upper_amp", "upper_to_body",
                           "vol_ratio", "pos_pct", "amount", "pct_chg", "close",
                           "ma20", "low_20")} for p in yes],
    "yesterday_rules": {t: [q["symbol"] for q in yres[t]] for t in tags_short},
}
json.dump(out, open("/tmp/verify_improve_result.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
print("\n结果已写 /tmp/verify_improve_result.json")
