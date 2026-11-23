# -*- coding: utf-8 -*-
"""前置形态研究：跳空低开破位 / 下跌通道 / 企稳 对低位上影线后续收益的影响。

用户观察：圣晖集成 7-02 跳空低开跌停后 7-10 的上影线是下跌中继而非反转；
下跌通道中的上影线难转势，除非出现企稳。本脚本给 2026 全年 1023 只入选样本
打上前置形态标签，量化各标签的 5 日收益/胜率/大亏率，并模拟剔除规则。

复用 backtest_2026 的新浪 HTTP 日K(250根)，每只票只拉一次。
"""
import sys, os, json, time, statistics, requests, urllib3
import concurrent.futures as cf
from collections import defaultdict

urllib3.disable_warnings()
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(REPO, "low_shadow", "state")
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
     "Referer": "https://finance.sina.com.cn/"}
KL_URL = "http://quotes.sina.cn/cn/api/jsonp_v2.php/var%20t=/CN_MarketDataService.getKLineData"

d = json.load(open(os.path.join(OUT, "bt_2026_result.json"), encoding="utf-8"))
picks = d["picks"]
valid = [x for x in picks if x["rets"].get("5") is not None]


def kl(sym):
    for _ in range(3):
        try:
            r = requests.get(KL_URL, params={"symbol": sym, "scale": 240, "ma": "no",
                             "datalen": 250}, headers=H, timeout=20)
            s = r.text.strip(); i = s.find("([")
            if i < 0:
                time.sleep(0.2); continue
            a = json.loads(s[i + 1:s.rfind(")")])
            return [(x["day"], float(x["open"]), float(x["high"]),
                     float(x["low"]), float(x["close"])) for x in a]
        except Exception:
            time.sleep(0.2)
    return []


# 需要的代码 -> 新浪symbol
need = {}
for x in valid:
    code = x["symbol"]
    sym = ("sh" if code[0] == "6" else ("bj" if code[0] in "489" else "sz")) + code
    need[sym] = code
print("需拉K线 %d 只" % len(need), flush=True)
t0 = time.time()
kmap = {}
with cf.ThreadPoolExecutor(max_workers=12) as ex:
    futs = {ex.submit(kl, s): s for s in need}
    for i, f in enumerate(cf.as_completed(futs), 1):
        kmap[futs[f]] = f.result()
        if i % 200 == 0:
            print("  %d/%d (%.0fs)" % (i, len(need), time.time() - t0), flush=True)
print("K线完成 %.1fs" % (time.time() - t0), flush=True)


def tag(x):
    """给样本打前置形态标签。返回 dict。"""
    code = x["symbol"]
    sym = ("sh" if code[0] == "6" else ("bj" if code[0] in "489" else "sz")) + code
    a = kmap.get(sym) or []
    idx = next((i for i, z in enumerate(a) if z[0] == x["date"]), None)
    if idx is None or idx < 21:
        return None
    c = a[idx][4]
    # 1) 前10日是否有跳空低开<=-3% 或 单日跌<=-9.5%（破位事件）
    gap_break = False; gap_days = 0; limit_down = False
    for j in range(max(1, idx - 10), idx):
        pc = a[j - 1][4]
        gap = (a[j][1] / pc - 1) * 100
        chg = (a[j][4] / pc - 1) * 100
        if gap <= -3.0:
            gap_break = True; gap_days += 1
        if chg <= -9.5:
            limit_down = True
    # 2) 下跌通道：收盘距前10日高点回撤
    h10 = max(z[2] for z in a[max(0, idx - 10):idx])
    dd10 = (c / h10 - 1) * 100
    h5 = max(z[2] for z in a[max(0, idx - 5):idx])
    dd5 = (c / h5 - 1) * 100
    # 均线空头排列
    cl = [z[4] for z in a[:idx + 1]]
    ma5 = sum(cl[-5:]) / 5; ma10 = sum(cl[-10:]) / 10; ma20 = sum(cl[-20:]) / 20
    bear_ma = ma5 < ma10 < ma20
    # 3) 企稳信号：前5日已止跌（最近5日跌幅<-3% 且 当日收盘>近5日低点3%以上为反弹未企稳；
    #    这里"企稳"= 前10日没破位 且 MA非空头 且 距前5日高回撤<8%
    low5 = min(z[3] for z in a[max(0, idx - 5):idx + 1])
    rebound_from_low5 = (c / low5 - 1) * 100
    # 企稳定义（用户语义）：未破位 + 非深度下跌通道 + 已接近近期低点（未大幅反弹）
    stabilized = (not gap_break) and (not bear_ma) and dd10 > -12 and rebound_from_low5 < 8
    # 破位未企稳（用户要剔除的核心）
    broken_not_stabilized = (gap_break or limit_down or bear_ma or dd10 <= -12)
    return {"gap_break": gap_break, "gap_days": gap_days, "limit_down": limit_down,
            "dd10": round(dd10, 2), "dd5": round(dd5, 2), "bear_ma": bear_ma,
            "rebound5": round(rebound_from_low5, 2),
            "stabilized": stabilized, "broken": broken_not_stabilized}


tagged = []
for x in valid:
    tg = tag(x)
    if tg is None:
        continue
    z = dict(x); z.update(tg)
    tagged.append(z)
print("打标完成 %d / %d" % (len(tagged), len(valid)), flush=True)


def st(lst):
    r = [x["rets"]["5"] for x in lst]
    if not r:
        return {"n": 0, "mean": None, "hit": None, "loss10": None, "loss15": None}
    return {"n": len(lst), "mean": round(statistics.mean(r) * 100, 2),
            "hit": round(100.0 * sum(1 for v in r if v > 0) / len(r), 1),
            "loss10": round(100.0 * sum(1 for v in r if v < -0.10) / len(r), 1),
            "loss15": sum(1 for v in r if v < -0.15)}


# 分组统计
groups = {}
def addg(name, fn):
    groups[name] = st([x for x in tagged if fn(x)])

addg("全部", lambda x: True)
addg("破位(跳空低开/跌停/空头/深跌)", lambda x: x["broken"])
addg("未破位", lambda x: not x["broken"])
addg("前10日跳空低开≤-3%", lambda x: x["gap_break"])
addg("前10日无跳空低开", lambda x: not x["gap_break"])
addg("前10日有跌停", lambda x: x["limit_down"])
addg("均线空头排列", lambda x: x["bear_ma"])
addg("距前10日高≤-12%(深跌通道)", lambda x: x["dd10"] <= -12)
addg("企稳上影线", lambda x: x["stabilized"])
addg("破位且未企稳", lambda x: x["broken"] and not x["stabilized"])

# 三维交叉：破位 x 反弹幅度
cross = {}
for bk in (False, True):
    for rb in (False, True):
        sub = [x for x in tagged if x["broken"] == bk and x["rebound5"] >= 8 == rb] if False else [
            x for x in tagged if x["broken"] == bk and (x["rebound5"] >= 8) == rb]
        cross["broken=%s|rebound8=%s" % (bk, rb)] = st(sub)

# 最差5只标签
worst5 = sorted(valid, key=lambda x: x["rets"]["5"])[:5]
worst5_tag = []
for x in worst5:
    tg = next((z for z in tagged if z["symbol"] == x["symbol"] and z["date"] == x["date"]), {})
    worst5_tag.append({"date": x["date"], "symbol": x["symbol"],
                       "r5": round(x["rets"]["5"] * 100, 2),
                       "gap_break": tg.get("gap_break"), "limit_down": tg.get("limit_down"),
                       "bear_ma": tg.get("bear_ma"), "dd10": tg.get("dd10"),
                       "rebound5": tg.get("rebound5"), "broken": tg.get("broken"),
                       "stabilized": tg.get("stabilized")})

# 剔除规则模拟
s5 = {(x["date"], x["symbol"]) for x in worst5}
gold = {("2026-10-29", "300030"), ("2026-10-30", "688095"),
        ("2026-10-30", "688588"), ("2026-11-09", "600793")}


def rule(name, badfn):
    rm = [x for x in tagged if badfn(x)]
    kept = [x for x in tagged if not badfn(x)]
    ks = st(kept)
    return {"rule": name, "n_removed": len(rm), "kept": ks,
            "huge_removed": sum(1 for x in rm if x["rets"]["5"] < -0.15),
            "hit_worst5": sum(1 for x in rm if (x["date"], x["symbol"]) in s5),
            "gold_killed": sorted({x["symbol"] for x in rm
                                   if (x["date"], x["symbol"]) in gold})}


rules = [
    rule("R1 破位(跳空/跌停/空头/深跌)", lambda x: x["broken"]),
    rule("R2 前10日跳空低开≤-3%", lambda x: x["gap_break"]),
    rule("R3 均线空头排列", lambda x: x["bear_ma"]),
    rule("R4 距前10日高≤-12%", lambda x: x["dd10"] <= -12),
    rule("R5 破位 且 距近5日低反弹≥8%", lambda x: x["broken"] and x["rebound5"] >= 8),
    rule("R6 仅保留企稳上影线(剔非企稳)", lambda x: not x["stabilized"]),
    rule("R7 破位未企稳", lambda x: x["broken"] and not x["stabilized"]),
]

# rebound5 网格（量化"除非已经企稳/贴近低点"）
rebound_grid = []
for th in (5, 6, 7, 8, 9, 10, 12):
    gg = [x for x in tagged if x["rebound5"] < th]   # 保留贴近低点的
    rm = [x for x in tagged if x["rebound5"] >= th]
    ss = st(gg)
    rebound_grid.append({"th": th, "n_kept": len(gg), "kept": ss,
                         "huge_removed": sum(1 for x in rm if x["rets"]["5"] < -0.15),
                         "hit_worst5": sum(1 for x in rm if (x["date"], x["symbol"]) in s5),
                         "gold_killed": sorted({x["symbol"] for x in rm
                                                if (x["date"], x["symbol"]) in gold})})

# 组合规则：破位+反弹>=8% 叠加 上轮规则H(距低>8% 且(额>5亿 or 跌1%))
# 需要 amount / pos_pct / 当日指数涨跌 —— 从 bt_2026 picks 已带 amount/pos_pct
amt_map = {x["symbol"]: x["amount"] for x in valid}
pos_map = {x["symbol"]: x["pos_pct"] for x in valid}
combo = [
    rule("C1 rebound≥8% 且 额>5亿", lambda x: x["rebound5"] >= 8 and amt_map.get(x["symbol"], 0) > 5e8),
    rule("C2 rebound≥8% 或 (额>5亿且pos>8%)", lambda x: x["rebound5"] >= 8 or
         (amt_map.get(x["symbol"], 0) > 5e8 and pos_map.get(x["symbol"], 0) > 0.08)),
    rule("C3 rebound≥8%+破位 (核心)", lambda x: x["rebound5"] >= 8 and x["broken"]),
    rule("C4 rebound≥8%+破位 或 巨亏形态(额>8亿&pos>8%)", lambda x:
         (x["rebound5"] >= 8 and x["broken"]) or
         (amt_map.get(x["symbol"], 0) > 8e8 and pos_map.get(x["symbol"], 0) > 0.08)),
]

# 导出明细供报告复算
details = [{"date": x["date"], "symbol": x["symbol"], "name": x["name"],
            "r5": round(x["rets"]["5"] * 100, 2),
            "rebound5": x["rebound5"], "dd10": x["dd10"], "dd5": x["dd5"],
            "gap_break": x["gap_break"], "limit_down": x["limit_down"],
            "bear_ma": x["bear_ma"], "broken": x["broken"], "stabilized": x["stabilized"],
            "amount_yi": round(amt_map.get(x["symbol"], 0) / 1e8, 2),
            "pos_pct": pos_map.get(x["symbol"])} for x in tagged]

out = {"generated_at": d["generated_at"], "range": [d["days"][0], d["days"][-1]],
       "n_tagged": len(tagged), "groups": groups, "cross": cross,
       "worst5_tag": worst5_tag, "rules": rules, "rebound_grid": rebound_grid,
       "combo": combo, "details": details}
jpath = os.path.join(OUT, "prepattern_analysis.json")
json.dump(out, open(jpath, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("saved", jpath, flush=True)

print("\n=== 分组统计（5日收益）===", flush=True)
for k, v in groups.items():
    print("  %-34s n=%4d 5日%+.2f%% 胜率%.1f%% 亏>10%%=%.1f%% 巨亏=%d" %
          (k, v["n"], v["mean"] or 0, v["hit"] or 0, v["loss10"] or 0, v["loss15"]), flush=True)
print("\n=== 破位 x 反弹交叉 ===", flush=True)
for k, v in cross.items():
    print("  %-28s n=%4d 5日%+.2f%% 胜率%.1f%% 亏>10%%=%.1f%%" %
          (k, v["n"], v["mean"] or 0, v["hit"] or 0, v["loss10"] or 0), flush=True)
print("\n=== 最差5只前置标签 ===", flush=True)
for x in worst5_tag:
    print("  %s %s 5日%+.2f%% | 跳空=%s 跌停=%s 空头=%s 距10高%.1f%% 反弹%.1f%% 破位=%s 企稳=%s" %
          (x["date"], x["symbol"], x["r5"], x["gap_break"], x["limit_down"], x["bear_ma"],
           x["dd10"], x["rebound5"], x["broken"], x["stabilized"]), flush=True)
print("\n=== 剔除规则 ===", flush=True)
for r in rules:
    print("  %-32s 剔%4d 留5日%+.2f%% 胜率%.1f%% 巨亏剔%d 命中最差5=%d/5 误杀黄金:%s" %
          (r["rule"], r["n_removed"], r["kept"]["mean"] or 0, r["kept"]["hit"] or 0,
           r["huge_removed"], r["hit_worst5"], ",".join(r["gold_killed"]) or "无"), flush=True)
