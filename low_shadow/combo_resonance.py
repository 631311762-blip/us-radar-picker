# -*- coding: utf-8 -*-
"""跨模型共振研究：low_shadow 信号后 10 日内再出现凯众/奥雅/破位/领湃，
统计共振组的 3/5/10 日前向收益，与 low_shadow 单独基准对比。

方法论要点：
  1) low_shadow 基准用当前生产参数(vol_ratio_min=1.3)重算 2026 全年信号。
  2) 共振 = 同一只股票在 [T+1, T+10] 内又触发其它模型，取最早共振日 R。
  3) 无前视口径：共振组以 R 收盘为买点算 R+3/5/10；基准组以 T 收盘算 T+3/5/10。
     另附"事后分组参考口径"(T 买点，按未来是否共振分组)仅供诊断，含前视。
  4) 新浪 HTTP 250 根日K(不复权)，>32% 跳变视为除权并剔除该段收益。
不改任何生产参数文件；结果落 state/combo_resonance_result.json。
"""
import sys, os, json, time, statistics, requests, urllib3
import concurrent.futures as cf
from collections import defaultdict
import bisect

urllib3.disable_warnings()
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "low_shadow"))
import low_shadow_screener as m
m.DEMO = False
OUT = os.path.join(REPO, "low_shadow", "state")
CACHE = os.path.join(OUT, "kl_cache_2026.json")
RES = os.path.join(OUT, "combo_resonance_result.json")

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
     "Referer": "https://finance.sina.com.cn/"}
LIST_URL = "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
KL_URL = "http://quotes.sina.cn/cn/api/jsonp_v2.php/var%20t=/CN_MarketDataService.getKLineData"
START = "2026-01-01"
KLEN = 250
FWD = [3, 5, 10]
WIN = 10  # 共振观察窗口 T+1..T+10

params = m.load_params()


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


# ---------- Phase 1: 拉数据 + 缓存 + 重算 low_shadow ----------
t0 = time.time()
use_cache = os.path.exists(CACHE)
if use_cache:
    cache = json.load(open(CACHE, encoding="utf-8"))
    DAYS = cache["days"]; uni = cache["uni"]; kl = cache["kl"]
    print("复用缓存 | 交易日 %d 全市场 %d | %.1fs" %
          (len(DAYS), len(uni), time.time() - t0), flush=True)
else:
    idx = sina_kline("sh000001")
    DAYS = [b["date"] for b in idx if b["date"] >= START]
    uni = sina_universe()
    print("交易日 %d | %s→%s | 全市场 %d | %.1fs" %
          (len(DAYS), DAYS[0], DAYS[-1], len(uni), time.time() - t0), flush=True)
    syms = [u[0] for u in uni]
    kl = {}
    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(sina_kline, s): s for s in syms}
        for f in cf.as_completed(futs):
            kl[futs[f]] = f.result()
    # 只缓存 date->close 映射 + 每条K线的date序列（收益计算够用）
    klclose = {}
    for sym, bars in kl.items():
        klclose[sym] = [[b["date"], round(b["close"], 4)] for b in bars]
    json.dump({"days": DAYS, "uni": uni, "kl": klclose},
              open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    print("K线完成并缓存 %.1fs" % (time.time() - t0), flush=True)

# 从缓存重建 bars（date->close dict）
close_map = {}
for sym, pairs in kl.items():
    if not pairs:
        close_map[sym] = {}
        continue
    first = pairs[0]
    if isinstance(first, dict):                      # 完整 bars（首次运行内存）
        close_map[sym] = {b["date"]: b["close"] for b in pairs}
    elif isinstance(first, (list, tuple)) and len(first) == 2:  # [date, close] 缓存
        close_map[sym] = {d: c for d, c in pairs}
    else:
        close_map[sym] = {}

idx_of = {d: i for i, d in enumerate(DAYS)}
lixin, _ = m.load_lixin_exclude()

# 重算 low_shadow（需要完整 bars 算特征；缓存只有 close，所以重拉特征用 bar）
# 为省事：若首次运行用内存 bars；缓存复用时特征已在 base_events 缓存
BASE_CACHE = os.path.join(OUT, "combo_base_events.json")
if os.path.exists(BASE_CACHE):
    base_events = json.load(open(BASE_CACHE, encoding="utf-8"))
    print("复用 low_shadow 基准事件 %d | %.1fs" % (len(base_events), time.time() - t0), flush=True)
else:
    # 需要完整 bars 重算特征
    if not kl or any(not isinstance(v, list) or (v and isinstance(v[0], list)) for v in list(kl.values())[:3]):
        # 当前 kl 来自缓存(只有 [date,close])，需重拉
        syms = [u[0] for u in uni]
        fullkl = {}
        with cf.ThreadPoolExecutor(max_workers=12) as ex:
            futs = {ex.submit(sina_kline, s): s for s in syms}
            for f in cf.as_completed(futs):
                fullkl[futs[f]] = f.result()
        kl = fullkl
    base_events = []
    done = 0
    for (sym, code, name) in uni:
        bars = kl.get(sym) or []
        if len(bars) < 21:
            continue
        done += 1
        dates = [b["date"] for b in bars]
        d2i = {d: i for i, d in enumerate(dates)}
        closes = [b["close"] for b in bars]
        bad = set()
        for j in range(1, len(bars)):
            if closes[j-1] > 0 and abs(closes[j]/closes[j-1] - 1) > 0.32:
                bad.add(dates[j])
        for D in DAYS:
            i0 = d2i.get(D)
            if i0 is None or i0 < 20:
                continue
            feat = m.extract_last_features(bars[:i0+1], params)
            if feat is None:
                continue
            h5ok, _ = m.h5_check(code, feat, params)
            if not h5ok or code in lixin:
                continue
            if m.is_low_shadow(feat, params):
                base_events.append({"date": D, "symbol": code, "name": name,
                                    "close": round(feat["close"], 4),
                                    "score_raw": round(feat["upper_amp"]*0.30 + feat["vol_ratio"]*0.01, 4)})
        if done % 1500 == 0:
            print("  特征 %d/%d | base=%d | %.0fs" % (done, len(uni), len(base_events), time.time()-t0), flush=True)
    json.dump(base_events, open(BASE_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    print("low_shadow 重算 %d | %.1fs" % (len(base_events), time.time()-t0), flush=True)

print("基准事件 %d 条 | 唯一股票 %d | %s→%s" %
      (len(base_events), len(set(e["symbol"] for e in base_events)),
       min(e["date"] for e in base_events), max(e["date"] for e in base_events)), flush=True)


# ---------- Phase 2: 加载4模型信号，统一 (code -> {signal_dates}) ----------
def norm_code(c):
    c = str(c).strip()
    if c[:2].lower() in ("sh", "sz", "bj"):
        c = c[2:]
    return c.zfill(6)


sig = {k: defaultdict(set) for k in ("kaizhong", "oya", "powei", "lingpei")}
span = {}

# 凯众 v4
kz = json.load(open(os.path.join(REPO, "kaizhong", "state", "v4_history.json"), encoding="utf-8"))
for x in kz:
    if x.get("date"):
        sig["kaizhong"][norm_code(x["code"])].add(x["date"])
span["kaizhong"] = [min(x["date"] for x in kz if x.get("date")),
                    max(x["date"] for x in kz if x.get("date")), len(kz)]

# 奥雅 oya_C 标准版（全年1854）
oya = json.load(open(os.path.join(REPO, "stock-v3", "oya_C_奥雅标准.json"), encoding="utf-8"))
for x in oya:
    if x.get("date"):
        sig["oya"][norm_code(x["code"])].add(x["date"])
span["oya"] = [min(x["date"] for x in oya if x.get("date")),
               max(x["date"] for x in oya if x.get("date")), len(oya)]

# 破位 history.jsonl（sig_day=信号日）
pw = [json.loads(l) for l in open(os.path.join(REPO, "powei", "state", "history.jsonl"), encoding="utf-8") if l.strip()]
for x in pw:
    d = x.get("sig_day") or x.get("date")
    if d:
        sig["powei"][norm_code(x["code"])].add(d)
span["powei"] = [min((x.get("sig_day") or x["date"]) for x in pw),
                 max((x.get("sig_day") or x["date"]) for x in pw), len(pw)]

# 领湃 V33台账（date=推荐日）
lp = json.load(open(os.path.join(REPO, "lingpei", "lingpei", "state", "领湃_V33跟踪台账.json"), encoding="utf-8"))
for x in lp["samples"]:
    if x.get("date"):
        sig["lingpei"][norm_code(x["code"])].add(x["date"])
span["lingpei"] = [min(x["date"] for x in lp["samples"] if x.get("date")),
                   max(x["date"] for x in lp["samples"] if x.get("date")), len(lp["samples"])]

print("\n模型信号归档:")
for k in sig:
    print("  %-9s %s→%s | %d条 | 股票%d" % (k, span[k][0], span[k][1], span[k][2], len(sig[k])))


# ---------- Phase 3: 收益计算 ----------
def fwd_ret(code, buy_date, n, sym_of=None):
    """以 buy_date 收盘买入，n 个交易日后收盘卖出。返回小数收益或 None。"""
    cm = None
    # close_map 用 sym 作键，这里 code->sym 反查
    key = code
    if key not in close_map and sym_of:
        key = sym_of.get(code, code)
    if key not in close_map:
        key = next((s for s, c in sym_of.items() if c == code), code) if sym_of else code
    cmap = close_map.get(key)
    if not cmap or buy_date not in cmap:
        return None
    ti = idx_of.get(buy_date)
    if ti is None or ti + n >= len(DAYS):
        return None
    td = DAYS[ti + n]
    if td not in cmap:
        return None
    # 除权过滤：区间内单日跳变>32%
    seg = DAYS[ti:ti+n+1]
    for j in range(1, len(seg)):
        a, b = seg[j-1], seg[j]
        if a in cmap and b in cmap and cmap[a] > 0 and abs(cmap[b]/cmap[a]-1) > 0.32:
            return None
    return cmap[td] / cmap[buy_date] - 1


sym_of = {code: sym for (sym, code, name) in uni}


def stat(rets):
    v = [r for r in rets if r is not None]
    if not v:
        return {"n": 0, "hit": None, "mean": None, "median": None}
    return {"n": len(v),
            "hit": round(100.0*sum(1 for r in v if r > 0)/len(v), 2),
            "mean": round(statistics.mean(v)*100, 3),
            "median": round(statistics.median(v)*100, 3),
            "loss10": round(100.0*sum(1 for r in v if r < -0.10)/len(v), 2)}


# 基准组：low_shadow T 日买点，T+3/5/10
base_rets = {n: [] for n in FWD}
for e in base_events:
    for n in FWD:
        base_rets[n].append(fwd_ret(e["symbol"], e["date"], n, sym_of))
base_stat = {str(n): stat(base_rets[n]) for n in FWD}

# 共振匹配 + 共振组收益（无前视：R 买点）
reson = {k: [] for k in sig}  # 每条共振事件
for e in base_events:
    code, T = e["symbol"], e["date"]
    ti = idx_of.get(T)
    if ti is None:
        continue
    for k in sig:
        dates = sig[k].get(code)
        if not dates:
            continue
        # 窗口 T+1..T+10
        lo, hi = DAYS[ti+1] if ti+1 < len(DAYS) else "9999", DAYS[min(ti+WIN, len(DAYS)-1)]
        cand = sorted(d for d in dates if lo <= d <= hi)
        if not cand:
            continue
        R = cand[0]
        ev = {"date_T": T, "code": code, "name": e["name"], "model": k,
              "reson_date": R, "gap_days": idx_of.get(R, ti) - ti}
        ev["rets"] = {str(n): fwd_ret(code, R, n, sym_of) for n in FWD}
        # 事后分组参考：T 买点
        ev["rets_T"] = {str(n): fwd_ret(code, T, n, sym_of) for n in FWD}
        reson[k].append(ev)

reson_stat = {}
for k in sig:
    reson_stat[k] = {"n_events": len(reson[k]),
                     "n_stocks": len(set(e["code"] for e in reson[k])),
                     "gap_median": statistics.median([e["gap_days"] for e in reson[k]]) if reson[k] else None,
                     **{("r%d" % n): stat([e["rets"][str(n)] for e in reson[k]]) for n in FWD},
                     **{("r%d_T" % n): stat([e["rets_T"][str(n)] for e in reson[k]]) for n in FWD}}

# 任意模型共振（去重：同一 base 事件只要任一模型共振）
seen_base = set()
any_reson = []
for k in sig:
    for e in reson[k]:
        key = (e["date_T"], e["code"])
        if key in seen_base:
            continue
        seen_base.add(key)
        any_reson.append(e)
any_stat = {"n_events": len(any_reson), "n_stocks": len(set(e["code"] for e in any_reson)),
            **{("r%d" % n): stat([e["rets"][str(n)] for e in any_reson]) for n in FWD},
            **{("r%d_T" % n): stat([e["rets_T"][str(n)] for e in any_reson]) for n in FWD}}

# 共振覆盖率
n_base = len(base_events)
any_cover = round(100.0*len(any_reson)/n_base, 2) if n_base else 0

# 前瞻共振检验（可实盘口径）：T 日之前 10 日内该股是否已触发过其它模型
prior_stat = {k: {str(n): [] for n in FWD} for k in list(sig)+["any_hit", "no_hit"]}
n_prior = 0
for e in base_events:
    code, T = e["symbol"], e["date"]
    ti = idx_of.get(T)
    if ti is None:
        continue
    lo, hi = DAYS[max(0, ti-WIN)], (DAYS[ti-1] if ti >= 1 else "")
    prior_models = [k for k in sig if any(lo <= d <= hi for d in sig[k].get(code, ()))]
    hit = bool(prior_models)
    if hit:
        n_prior += 1
    grp = "any_hit" if hit else "no_hit"
    for n in FWD:
        r = fwd_ret(code, T, n, sym_of)
        prior_stat[grp][str(n)].append(r)
        for mk in prior_models:
            prior_stat[mk][str(n)].append(r)
prior_out = {k: {"n_prior_events": n_prior,
                 "coverage_pct": round(100.0*n_prior/n_base, 2) if n_base else 0,
                 **{("r%d" % n): stat(prior_stat[k][str(n)]) for n in FWD}}
             for k in prior_stat}

out = {
    "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    "range": [DAYS[0], DAYS[-1]], "n_days": len(DAYS),
    "params": {"vol_ratio_min": params["vol_ratio_min"]},
    "base": {"n_events": n_base, "n_stocks": len(set(e["symbol"] for e in base_events)),
             "stats": base_stat},
    "model_span": span,
    "reson": reson_stat,
    "any_reson": any_stat,
    "coverage_pct": any_cover,
    "prior_reson": prior_out,
    "reson_samples": {k: reson[k][:60] for k in sig},  # 明细前60条备查
}
json.dump(out, open(RES, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("\n保存", RES, flush=True)
print("\n=== 结果摘要 ===")
print("基准 low_shadow(1.3): %d事件 | T买点 3日%s%%/%s%% 5日%s%%/%s%% 10日%s%%/%s%% (胜率/均收益)" %
      (n_base, base_stat["3"]["hit"], base_stat["3"]["mean"],
       base_stat["5"]["hit"], base_stat["5"]["mean"], base_stat["10"]["hit"], base_stat["10"]["mean"]))
for k in sig:
    s = reson_stat[k]
    print("  %-9s 共振%3d事件(%d股) 中位间隔%s日 | R买点 5日 胜率%s%% 均%s%% (n=%d)" %
          (k, s["n_events"], s["n_stocks"], s["gap_median"],
           s["r5"]["hit"], s["r5"]["mean"], s["r5"]["n"]))
print("  任意模型共振 %d事件 覆盖%.2f%% | R买点 5日 胜率%s%% 均%s%% (n=%d)" %
      (len(any_reson), any_cover, any_stat["r5"]["hit"], any_stat["r5"]["mean"], any_stat["r5"]["n"]))
