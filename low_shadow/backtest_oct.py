# -*- coding: utf-8 -*-
"""10 月至今逐日全市场回测（低位上影线模型，当前生产参数）。

严格复用生产模块 low_shadow_screener.py 自身的：
  extract_last_features / is_low_shadow(8道硬条件) / shadow_diagnose(漏斗归因)
  / rank_candidates(分位评分) / load_lixin_exclude(立信一票否决)
不修改任何模型规则。

数据源：新浪 hs_a 全市场代码表 + 新浪日K(不复权, 150根)。
历史成交额 = 成交量(股) × (开+高+低+收)/4，已验证中位误差 +0.21%、
5000万门槛判定 60/60 与真实成交额一致。8道硬条件均为比值/相对量，不复权不影响判定。
前向收益按全市场交易日历对齐（D+n 交易日收盘 / D 收盘 - 1）。
"""
import sys, os, json, csv, time, datetime, bisect, statistics
import concurrent.futures as cf
from collections import defaultdict
import requests, urllib3

urllib3.disable_warnings()
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "low_shadow"))
import low_shadow_screener as m
m.DEMO = False

OUT = os.path.join(REPO, "low_shadow", "state")
os.makedirs(OUT, exist_ok=True)
params = dict(m.DEFAULT_PARAMS)
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36",
     "Referer": "https://finance.sina.com.cn/"}
LIST_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
KL_URL = "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20t=/CN_MarketDataService.getKLineData"
FWD = [1, 3, 5, 10]
START = "2026-10-01"


def sina_kline(sym, n=150):
    for attempt in range(3):
        try:
            r = requests.get(KL_URL, params={"symbol": sym, "scale": 240, "ma": 20,
                             "datalen": n}, timeout=16, verify=False, headers=H)
            if r.status_code != 200 or len(r.content) < 100:
                time.sleep(0.25); continue
            t = r.text; s = t.find("([")
            arr = json.loads(t[s + 1:t.rfind(")")])
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
            time.sleep(0.25)
    return []


def sina_universe():
    out, seen = [], set()
    for pn in range(1, 100):
        arr = []
        for attempt in range(3):
            try:
                r = requests.get(LIST_URL, params={"page": pn, "num": 100, "sort": "symbol",
                                 "asc": 1, "node": "hs_a", "symbol": "", "_s_r_a": "page"},
                                 timeout=16, verify=False, headers=H)
                if r.text.strip().startswith("["):
                    arr = json.loads(r.text)
                break
            except Exception:
                time.sleep(0.3 * (attempt + 1))
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
idx_bars = sina_kline("sh000001", 150)
DAYS = [b["date"] for b in idx_bars if b["date"] >= START]
IDX_CLOSE = {b["date"]: b["close"] for b in idx_bars}
print("回测交易日: %d 天 | %s → %s" % (len(DAYS), DAYS[0], DAYS[-1]), flush=True)
print("参数: upper_amp_min=%s candidate_pool=%s | FWD=%s" %
      (params["upper_amp_min"], params["candidate_pool"], FWD), flush=True)

uni = sina_universe()
print("全市场代码表: %d 只 | 耗时 %.1fs" % (len(uni), time.time() - t0), flush=True)

lixin, loaded = m.load_lixin_exclude()
print("立信排除集: loaded=%s size=%d" % (loaded, len(lixin)), flush=True)

kl_map = {}
syms = [u[0] for u in uni]
t1 = time.time(); done = [0]
with cf.ThreadPoolExecutor(max_workers=12) as ex:
    futs = {ex.submit(sina_kline, s): s for s in syms}
    for f in cf.as_completed(futs):
        s = futs[f]
        try:
            kl_map[s] = f.result()
        except Exception:
            kl_map[s] = []
        done[0] += 1
        if done[0] % 1000 == 0:
            print("  K线进度 %d/%d (%.0fs)" % (done[0], len(syms), time.time() - t1), flush=True)
ok_kl = sum(1 for v in kl_map.values() if len(v) >= 21)
print("K线拉取完成: 有效 %d / %d | 耗时 %.1fs" %
      (ok_kl, len(syms), time.time() - t1), flush=True)

bench = {n: [[] for _ in DAYS] for n in FWD}   # 全市场等权基准（每日期收益列表）
picks = []
funnel_total = defaultdict(int)
funnel_by_day = defaultdict(lambda: defaultdict(int))
n_suspect = 0
t2 = time.time(); processed = [0]

for (sym, code, name) in uni:
    bars = kl_map.get(sym) or []
    if len(bars) < 21:
        continue
    processed[0] += 1
    dates = [b["date"] for b in bars]
    closes = [b["close"] for b in bars]
    d2i = {d: i for i, d in enumerate(dates)}
    bad = set()
    for j in range(1, len(bars)):
        if closes[j - 1] > 0 and abs(closes[j] / closes[j - 1] - 1) > 0.32:
            bad.add(dates[j])
    if bad:
        n_suspect += 1

    for di, D in enumerate(DAYS):
        i0 = d2i.get(D)
        if i0 is None or i0 < 20:
            continue
        p0 = closes[i0]
        if p0 <= 0:
            continue
        # 前向收益（日历对齐）+ 等权基准
        fwd = {}
        for n in FWD:
            if di + n >= len(DAYS):
                continue
            k = bisect.bisect_left(dates, DAYS[di + n])
            if k >= len(bars):
                continue
            if dates[k] > DAYS[min(di + n + 2, len(DAYS) - 1)]:
                continue  # 长期停牌，样本失真
            if any(d in bad for d in dates[i0 + 1:k + 1]):
                continue  # 窗口内疑似除权/异常
            r = closes[k] / p0 - 1
            fwd[n] = r
            bench[n][di].append(r)

        # 严格复用模型判定
        cut = bars[:i0 + 1]
        feat = m.extract_last_features(cut, params)
        if feat is None:
            continue
        if not m.is_low_shadow(feat, params):
            reason = m.shadow_diagnose(feat, params)
            funnel_total[reason] += 1
            funnel_by_day[D][reason] += 1
            continue
        if code in lixin:
            funnel_total["立信审计剔除"] += 1
            funnel_by_day[D]["立信审计剔除"] += 1
            continue
        funnel_total["入选"] += 1
        funnel_by_day[D]["入选"] += 1
        picks.append({
            "date": D, "di": di, "symbol": code, "name": name,
            "close": round(p0, 2),
            "upper_amp": round(feat["upper_amp"], 4),
            "upper_to_body": round(feat["upper_to_body"], 3),
            "vol_ratio": round(feat["vol_ratio"], 3),
            "pos_pct": round(feat["pos_pct"], 4),
            "amount": round(feat["amount"], 0),
            "pct_chg": round(feat["pct_chg"], 2),
            "ma20": round(feat["ma20"], 2),
            "main_path": bool(feat["main_path"]),
            "comp_path": bool(feat["comp_path"]),
            "is_comp": bool(feat["is_comp"]),
            "rets": {str(n): (round(fwd[n], 6) if n in fwd else None) for n in FWD},
        })
    if processed[0] % 1000 == 0:
        print("  回测进度 %d/%d | 已入选 %d | %.0fs" %
              (processed[0], len(syms), len(picks), time.time() - t2), flush=True)

print("回测完成: 累计入选 %d 条 | 耗时 %.1fs" % (len(picks), time.time() - t2), flush=True)

# 每日内按模型分位评分排名
by_day = defaultdict(list)
for p in picks:
    by_day[p["date"]].append(p)
for D, lst in by_day.items():
    m.rank_candidates(lst)
    for rank, p in enumerate(lst, 1):
        p["rank"] = rank
        p["n_day"] = len(lst)


def stat(vals):
    if not vals:
        return {"n": 0, "hit": None, "mean": None, "median": None}
    return {"n": len(vals),
            "hit": round(sum(1 for v in vals if v > 0) / len(vals), 4),
            "mean": round(statistics.mean(vals), 6),
            "median": round(statistics.median(vals), 6)}


daily = []
for di, D in enumerate(DAYS):
    lst = by_day.get(D, [])
    rec = {"date": D, "n_picks": len(lst), "funnel": dict(funnel_by_day.get(D, {}))}
    for n in FWD:
        s = stat([p["rets"][str(n)] for p in lst if p["rets"][str(n)] is not None])
        b = stat(bench[n][di])
        s["bench_eq_mean"] = b["mean"]
        s["bench_eq_median"] = b["median"]
        s["bench_eq_n"] = b["n"]
        if di + n < len(DAYS) and D in IDX_CLOSE and DAYS[di + n] in IDX_CLOSE:
            s["bench_idx"] = round(IDX_CLOSE[DAYS[di + n]] / IDX_CLOSE[D] - 1, 6)
        rec["r%d" % n] = s
    daily.append(rec)

payload = {
    "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "data_source": "sina_hs_a_list(全市场代码表) + sina_daily_kline(不复权,150根,截至%s)" % (DAYS[-1]),
    "note": "东财push2行情网关502全族不可用期间的备用源；口径自检(10-30五只代表票)与东财前复权归档逐位一致；"
            "历史成交额=成交量×(开+高+低+收)/4(中位误差+0.21%)；前向收益按交易日历对齐；"
            "窗口内单日涨跌>32%视为疑似除权剔除。判定/评分/立信排除100%复用生产模型函数，规则未改。",
    "params": {k: params[k] for k in ("upper_amp_min", "upper_to_body_min", "vol_ratio_min",
                                      "low_pos_max_pct", "min_amount", "body_abs_max_pct",
                                      "comp_shadow_min", "comp_upper_to_body_min",
                                      "comp_low_pos_max_pct", "below_ma20", "candidate_pool")},
    "days": DAYS, "fwd": FWD,
    "universe": len(uni), "kline_ok": ok_kl, "lixin_size": len(lixin),
    "stocks_with_suspect_bar": n_suspect,
    "funnel_total": dict(funnel_total),
    "n_picks_total": len(picks),
    "daily": daily,
    "picks": picks,
}
jpath = os.path.join(OUT, "bt_oct_result.json")
json.dump(payload, open(jpath, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("已保存:", jpath, flush=True)

pcols = ["date", "rank", "n_day", "symbol", "name", "score", "close", "upper_amp",
         "upper_to_body", "vol_ratio", "pos_pct", "amount", "pct_chg", "ma20",
         "main_path", "comp_path", "is_comp", "r1", "r3", "r5", "r10"]
pcsv = os.path.join(OUT, "bt_oct_picks.csv")
with open(pcsv, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=pcols); w.writeheader()
    for p in sorted(picks, key=lambda x: (x["date"], x.get("rank", 999))):
        row = {k: p.get(k, "") for k in pcols}
        for n in FWD:
            v = p["rets"].get(str(n))
            row["r%d" % n] = round(v * 100, 2) if v is not None else ""
        w.writerow(row)
print("已保存:", pcsv, flush=True)

dcols = ["date", "n_picks"] + ["r%d_%s" % (n, k) for n in FWD
                                for k in ("n", "hit", "mean", "median", "bench_eq_mean", "bench_idx")]
dcsv = os.path.join(OUT, "bt_oct_daily.csv")
with open(dcsv, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=dcols); w.writeheader()
    for d in daily:
        row = {"date": d["date"], "n_picks": d["n_picks"]}
        for n in FWD:
            s = d["r%d" % n]
            for k in ("n", "hit", "mean", "median", "bench_eq_mean", "bench_idx"):
                v = s.get(k)
                row["r%d_%s" % (n, k)] = (round(v * 100, 2) if v is not None and k in
                                          ("mean", "median", "bench_eq_mean", "bench_idx") else v)
        w.writerow(row)
print("已保存:", dcsv, flush=True)

print("\n=== 漏斗总计(各硬条件淘汰数) ===", flush=True)
for k, v in sorted(funnel_total.items(), key=lambda x: -x[1]):
    print("  %-14s %d" % (k, v), flush=True)
print("\n=== 逐日样本数 ===", flush=True)
for d in daily:
    print("  %s %3d只 | 3日 %s | 5日 %s" %
          (d["date"], d["n_picks"],
           ("%s/%s" % (d["r3"]["n"], round((d["r3"]["mean"] or 0) * 100, 2))),
           ("%s/%s" % (d["r5"]["n"], round((d["r5"]["mean"] or 0) * 100, 2)))), flush=True)
print("BT_DONE", flush=True)
