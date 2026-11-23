# -*- coding: utf-8 -*-
"""2026 全年逐日全市场回测（低位上影线模型 · 当前生产口径 = 8道硬条件 + H5精选含主板）。

严格复用生产模块 low_shadow_screener.py 自身函数，不改任何规则：
  extract_last_features / is_low_shadow / shadow_diagnose / h5_check
  / rank_candidates / load_lixin_exclude / load_params
数据源：新浪 HTTP 通道 hs_a 全市场代码表 + 日K(不复权, 250根, 覆盖2025-11至今)。
历史成交额 = 成交量 × (开+高+低+收)/4。前向收益按全市场交易日历对齐。
窗口内单日涨跌>32%视为疑似除权剔除。
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
params = m.load_params()          # 当前生产参数（含 H5 白名单、param_lock 等）
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36",
     "Referer": "https://finance.sina.com.cn/"}
LIST_URL = "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
KL_URL = "http://quotes.sina.cn/cn/api/jsonp_v2.php/var%20t=/CN_MarketDataService.getKLineData"
FWD = [1, 3, 5, 10]
START = "2026-01-01"
KLEN = 250


def _parse_jsonp(t):
    s = t.strip()
    i = s.find("([")
    if i < 0:
        i = s.find("(")
        j = s.rfind(")")
        return json.loads(s[i + 1:j]) if i >= 0 and j > i else None
    return json.loads(s[i + 1:s.rfind(")")])


def sina_kline(sym, n=KLEN):
    for attempt in range(3):
        try:
            r = requests.get(KL_URL, params={"symbol": sym, "scale": 240, "ma": "no",
                             "datalen": n}, timeout=20, verify=False, headers=H)
            if r.status_code != 200 or len(r.content) < 100:
                time.sleep(0.25); continue
            arr = _parse_jsonp(r.text)
            if not arr:
                time.sleep(0.25); continue
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
                                 timeout=20, verify=False, headers=H)
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
idx_bars = sina_kline("sh000001")
DAYS = [b["date"] for b in idx_bars if b["date"] >= START]
if len(DAYS) < 30:
    print("FAIL: 交易日不足 %d" % len(DAYS), flush=True)
    sys.exit(1)
IDX_CLOSE = {b["date"]: b["close"] for b in idx_bars}
print("回测交易日: %d 天 | %s → %s" % (len(DAYS), DAYS[0], DAYS[-1]), flush=True)
print("生产参数: upper_amp_min=%s h5_enabled=%s h5_board_prefix=%s" %
      (params["upper_amp_min"], params["h5_enabled"], params["h5_board_prefix"]), flush=True)

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

bench = {n: [[] for _ in DAYS] for n in FWD}
picks = []                       # 当前生产口径（8道 + H5）最终入选
base_picks = []                  # 仅 8 道硬条件（用于对比 H5 影响）
funnel_total = defaultdict(int)
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
            bench[n][di].append(fwd[n])

        cut = bars[:i0 + 1]
        feat = m.extract_last_features(cut, params)
        if feat is None:
            continue
        if not m.is_low_shadow(feat, params):
            funnel_total[m.shadow_diagnose(feat, params)] += 1
            continue
        if code in lixin:
            funnel_total["立信审计剔除"] += 1
            continue
        # 8 道硬条件通过（含立信）
        base_picks.append({
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
        # H5 精选层（当前生产口径）
        h5ok, h5why = m.h5_check(code, feat, params)
        if not h5ok:
            funnel_total["H5_" + h5why] += 1
            continue
        funnel_total["入选"] += 1
        d = base_picks[-1].copy()
        d["h5"] = True
        picks.append(d)
    if processed[0] % 1000 == 0:
        print("  回测进度 %d/%d | 生产入选 %d | %.0fs" %
              (processed[0], len(syms), len(picks), time.time() - t2), flush=True)

print("回测完成: 8道 %d 条 → 生产口径(H5) %d 条 | 耗时 %.1fs" %
      (len(base_picks), len(picks), time.time() - t2), flush=True)

for label, lst in (("生产", picks), ("基线", base_picks)):
    by_day = defaultdict(list)
    for p in lst:
        by_day[p["date"]].append(p)
    for D, dl in by_day.items():
        m.rank_candidates(dl)
        for rank, p in enumerate(dl, 1):
            p["rank"] = rank
            p["n_day"] = len(dl)


def stat(vals):
    if not vals:
        return {"n": 0, "hit": None, "mean": None, "median": None}
    return {"n": len(vals),
            "hit": round(sum(1 for v in vals if v > 0) / len(vals), 4),
            "mean": round(statistics.mean(vals), 6),
            "median": round(statistics.median(vals), 6)}


summary = {}
for label, lst in (("prod", picks), ("base", base_picks)):
    sm = {}
    for n in FWD:
        vals = [p["rets"][str(n)] for p in lst if p["rets"].get(str(n)) is not None]
        sm["r%d" % n] = stat(vals)
    summary[label] = {"n": len(lst), **sm}

payload = {
    "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "data_source": "sina_http_hs_a_list + sina_daily_kline(不复权,250根,截至%s)" % DAYS[-1],
    "note": "2026全年逐日全市场回测；当前生产口径=8道硬条件+H5精选(含主板/排除北交所)；"
            "判定/评分/H5/立信100%复用生产函数；成交额=成交量×典型价；前向收益按交易日历对齐；"
            "窗口内单日涨跌>32%疑似除权剔除。",
    "params": {k: params.get(k) for k in ("upper_amp_min", "upper_to_body_min", "vol_ratio_min",
                                          "low_pos_max_pct", "min_amount", "body_abs_max_pct",
                                          "comp_shadow_min", "h5_enabled", "h5_board_prefix",
                                          "h5_max_drop_pct", "h5_vol_ratio_max", "h5_amount_max",
                                          "candidate_pool", "param_lock")},
    "days": DAYS, "fwd": FWD, "start": START,
    "universe": len(uni), "kline_ok": ok_kl, "lixin_size": len(lixin),
    "stocks_with_suspect_bar": n_suspect,
    "funnel_total": dict(funnel_total),
    "summary": summary,
    "n_picks_prod": len(picks), "n_picks_base": len(base_picks),
    "picks": picks, "base_picks": base_picks,
}
jpath = os.path.join(OUT, "bt_2026_result.json")
json.dump(payload, open(jpath, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("已保存:", jpath, flush=True)

pcols = ["date", "rank", "n_day", "symbol", "name", "score", "close", "upper_amp",
         "upper_to_body", "vol_ratio", "pos_pct", "amount", "pct_chg", "ma20",
         "main_path", "comp_path", "is_comp", "r1", "r3", "r5", "r10"]
pcsv = os.path.join(OUT, "bt_2026_picks.csv")
with open(pcsv, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=pcols); w.writeheader()
    for p in sorted(picks, key=lambda x: (x["date"], x.get("rank", 999))):
        row = {k: p.get(k, "") for k in pcols}
        for n in FWD:
            v = p["rets"].get(str(n))
            row["r%d" % n] = round(v * 100, 2) if v is not None else ""
        w.writerow(row)
print("已保存:", pcsv, flush=True)
print("\n=== 汇总 ===", flush=True)
for label, sm in summary.items():
    print("  %s n=%d | 5日 hit=%s mean=%s | 10日 mean=%s" %
          (label, sm["n"], sm["r5"]["hit"],
           round((sm["r5"]["mean"] or 0) * 100, 3),
           round((sm["r10"]["mean"] or 0) * 100, 3)), flush=True)
print("BT2026_DONE", flush=True)
