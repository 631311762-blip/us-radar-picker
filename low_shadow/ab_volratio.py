# -*- coding: utf-8 -*-
"""量比门槛 A/B 回测：1.5（生产）vs 1.3（候选），2026 全年。

同一份新浪 HTTP 250根日K、同一套特征，只在 is_low_shadow 判定处改 vol_ratio_min，
其余条件/H5/评分/立信/前向收益完全一致。重点看：
  1) 3日/5日 准确率(胜率)、平均收益、中位数
  2) 新增样本（1.3≤量比<1.5）本身的表现 —— 决定放宽是否值得
  3) 旭升集团 603305 这类形态对但量比略欠的票是否被纳入
  4) 分期稳健性（按季度）
不改任何生产参数文件。
"""
import sys, os, json, time, statistics, requests, urllib3
import concurrent.futures as cf
from collections import defaultdict

urllib3.disable_warnings()
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "low_shadow"))
import low_shadow_screener as m
m.DEMO = False
OUT = os.path.join(REPO, "low_shadow", "state")
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
     "Referer": "https://finance.sina.com.cn/"}
LIST_URL = "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
KL_URL = "http://quotes.sina.cn/cn/api/jsonp_v2.php/var%20t=/CN_MarketDataService.getKLineData"
FWD = [1, 3, 5, 10]
START = "2026-01-01"
KLEN = 250

base = m.load_params()
p15 = dict(base); p15["vol_ratio_min"] = 1.5
p13 = dict(base); p13["vol_ratio_min"] = 1.3


def _parse(t):
    s = t.strip(); i = s.find("([")
    return json.loads(s[i + 1:s.rfind(")")])


def sina_kline(sym, n=KLEN):
    for _ in range(3):
        try:
            r = requests.get(KL_URL, params={"symbol": sym, "scale": 240, "ma": "no",
                             "datalen": n}, timeout=20, verify=False, headers=H)
            if r.status_code != 200 or len(r.content) < 100:
                time.sleep(0.2); continue
            arr = _parse(r.text)
            if not arr:
                time.sleep(0.2); continue
            bars = []
            for x in arr:
                try:
                    o, h, l, c, v = (float(x["open"]), float(x["high"]), float(x["low"]),
                                     float(x["close"]), float(x["volume"]))
                    if o <= 0 or h <= 0 or c <= 0 or v < 0:
                        continue
                    bars.append({"date": x["day"], "open": o, "high": h, "low": l,
                                 "close": c, "vol": v,
                                 "amount": v * ((o + h + l + c) / 4.0)})
                except (KeyError, ValueError, TypeError):
                    pass
            return bars
        except Exception:
            time.sleep(0.2)
    return []


def sina_universe():
    out, seen = [], set()
    for pn in range(1, 100):
        arr = []
        for _ in range(3):
            try:
                r = requests.get(LIST_URL, params={"page": pn, "num": 100, "sort": "symbol",
                                 "asc": 1, "node": "hs_a", "symbol": "", "_s_r_a": "page"},
                                 timeout=20, verify=False, headers=H)
                if r.text.strip().startswith("["):
                    arr = json.loads(r.text)
                break
            except Exception:
                time.sleep(0.3)
        if not arr:
            break
        for x in arr:
            code = str(x.get("code", "")).zfill(6)
            sym = x.get("symbol", "")
            if not code or not sym or code in seen:
                continue
            seen.add(code)
            out.append((sym, code, x.get("name", "")))
        time.sleep(0.04)
    return out


t0 = time.time()
idx = sina_kline("sh000001")
DAYS = [b["date"] for b in idx if b["date"] >= START]
print("交易日 %d | %s→%s" % (len(DAYS), DAYS[0], DAYS[-1]), flush=True)
uni = sina_universe()
print("全市场 %d 只 | %.1fs" % (len(uni), time.time() - t0), flush=True)
lixin, _ = m.load_lixin_exclude()

kl_map = {}
syms = [u[0] for u in uni]
t1 = time.time()
with cf.ThreadPoolExecutor(max_workers=12) as ex:
    futs = {ex.submit(sina_kline, s): s for s in syms}
    for f in cf.as_completed(futs):
        kl_map[futs[f]] = f.result()
print("K线完成 %.1fs" % (time.time() - t1), flush=True)

import bisect
sel15, sel13 = [], []
t2 = time.time(); done = [0]
for (sym, code, name) in uni:
    bars = kl_map.get(sym) or []
    if len(bars) < 21:
        continue
    done[0] += 1
    dates = [b["date"] for b in bars]
    closes = [b["close"] for b in bars]
    d2i = {d: i for i, d in enumerate(dates)}
    bad = set()
    for j in range(1, len(bars)):
        if closes[j - 1] > 0 and abs(closes[j] / closes[j - 1] - 1) > 0.32:
            bad.add(dates[j])
    for di, D in enumerate(DAYS):
        i0 = d2i.get(D)
        if i0 is None or i0 < 20:
            continue
        p0 = closes[i0]
        if p0 <= 0:
            continue
        fwd = {}
        for n in FWD:
            if di + n >= len(DAYS):
                continue
            k = bisect.bisect_left(dates, DAYS[di + n])
            if k >= len(bars):
                continue
            if dates[k] > DAYS[min(di + n + 2, len(DAYS) - 1)]:
                continue
            if any(d in bad for d in dates[i0 + 1:k + 1]):
                continue
            fwd[n] = closes[k] / p0 - 1
        feat = m.extract_last_features(bars[:i0 + 1], base)
        if feat is None:
            continue
        h5ok, _ = m.h5_check(code, feat, base)
        if not h5ok or code in lixin:
            continue
        rec = None
        ok15 = m.is_low_shadow(feat, p15)
        ok13 = m.is_low_shadow(feat, p13)
        if ok15 or ok13:
            rec = {
                "date": D, "symbol": code, "name": name,
                "vol_ratio": round(feat["vol_ratio"], 4),
                "upper_amp": round(feat["upper_amp"], 4),
                "upper_to_body": round(feat["upper_to_body"], 3),
                "pos_pct": round(feat["pos_pct"], 4),
                "amount": round(feat["amount"], 0),
                "pct_chg": round(feat["pct_chg"], 2),
                "score": None,
                "rets": {str(n): (round(fwd[n], 6) if n in fwd else None) for n in FWD},
            }
        if ok15:
            sel15.append(dict(rec))
        if ok13:
            sel13.append(dict(rec))
    if done[0] % 1500 == 0:
        print("  %d/%d | 1.5=%d 1.3=%d | %.0fs" %
              (done[0], len(syms), len(sel15), len(sel13), time.time() - t2), flush=True)
print("入选 1.5=%d 1.3=%d | 回测 %.1fs" % (len(sel15), len(sel13), time.time() - t2), flush=True)

# 评分（各自口径内按分位排名，与生产一致）
for label, lst in (("1.5", sel15), ("1.3", sel13)):
    bd = defaultdict(list)
    for x in lst:
        bd[x["date"]].append(x)
    for D, dl in bd.items():
        m.rank_candidates(dl)
        for rank, x in enumerate(dl, 1):
            x["rank"] = rank; x["n_day"] = len(dl)


def stat(lst, n):
    v = [x["rets"][str(n)] for x in lst if x["rets"].get(str(n)) is not None]
    if not v:
        return {"n": 0, "hit": None, "mean": None, "median": None}
    return {"n": len(v),
            "hit": round(100.0 * sum(1 for r in v if r > 0) / len(v), 2),
            "mean": round(statistics.mean(v) * 100, 3),
            "median": round(statistics.median(v) * 100, 3),
            "loss10": round(100.0 * sum(1 for r in v if r < -0.10) / len(v), 2)}


# 新增样本集合（1.3入选但1.5落选）
key15 = {(x["date"], x["symbol"]) for x in sel15}
added = [x for x in sel13 if (x["date"], x["symbol"]) not in key15]

summary = {}
for label, lst in (("v15", sel15), ("v13", sel13), ("added", added)):
    summary[label] = {"n": len(lst), "r3": stat(lst, 3), "r5": stat(lst, 5),
                      "r1": stat(lst, 1), "r10": stat(lst, 10)}

# 分期稳健性（按季度）
qsummary = {}
for q, months in (("Q1", ("2026-01", "2026-02", "2026-03")),
                  ("Q2", ("2026-04", "2026-05", "2026-06")),
                  ("Q3", ("2026-07", "2026-08", "2026-09")),
                  ("Q4", ("2026-10", "2026-11", "2026-12"))):
    qsummary[q] = {}
    for label, lst in (("v15", sel15), ("v13", sel13), ("added", added)):
        sub = [x for x in lst if x["date"][:7] in months]
        qsummary[q][label] = {"n": len(sub), "r1": stat(sub, 1),
                              "r3": stat(sub, 3), "r5": stat(sub, 5)}

# 旭升集团在1.3下
xs = [x for x in sel13 if x["symbol"] == "603305" and x["date"] == "2026-11-20"]

out = {
    "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    "range": [DAYS[0], DAYS[-1]], "n_days": len(DAYS), "universe": len(uni),
    "v15": {"vol_ratio_min": 1.5, "n": len(sel15)},
    "v13": {"vol_ratio_min": 1.3, "n": len(sel13)},
    "summary": summary, "qsummary": qsummary,
    "xs_1120_in_13": xs,
    "added_top": sorted(added, key=lambda x: -(x["rets"]["5"] or -9))[:20],
    "added_worst": sorted([x for x in added if x["rets"].get("5") is not None],
                          key=lambda x: x["rets"]["5"])[:20],
}
jpath = os.path.join(OUT, "ab_volratio_result.json")
json.dump(out, open(jpath, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("saved", jpath, flush=True)

print("\n=== 3日/5日 对比 ===", flush=True)
for label in ("v15", "v13", "added"):
    s = summary[label]
    print("  %-6s n=%4d | 3日 胜率%s%% 均%s%% 中位%s%% | 5日 胜率%s%% 均%s%% 中位%s%% | 5日亏>10%%=%s%%" %
          (label, s["n"], s["r3"]["hit"], s["r3"]["mean"], s["r3"]["median"],
           s["r5"]["hit"], s["r5"]["mean"], s["r5"]["median"], s["r5"]["loss10"]), flush=True)
print("\n=== 分期（3日/5日 均收益）===", flush=True)
for q, qs in qsummary.items():
    print("  %s:" % q, flush=True)
    for label in ("v15", "v13", "added"):
        x = qs[label]
        print("    %-6s n=%4d 3日%s%% 5日%s%%" %
              (label, x["n"], x["r3"]["mean"], x["r5"]["mean"]), flush=True)
print("\n=== 旭升 11-20 在1.3下 ===", flush=True)
print(" ", xs if xs else "仍未入选", flush=True)
print("AB_DONE", flush=True)
