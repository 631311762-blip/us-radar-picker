# -*- coding: utf-8 -*-
"""【用户口径】从 2026-01-01 全市场模拟：以"第二次模型提示日"为起点算 3/5/10 收益与胜率。

定义：
  第一次提示 = low_shadow 在 T 日入选（当前生产口径 vol_ratio_min=1.3）。
  第二次提示 = 同一只股票在 [T+1, T+10] 内，其它模型(凯众/奥雅/破位/领湃)再次提示，
               取最早的提示日 R。收益率/准确率一律从 R 日收盘算起（R+3/R+5/R+10）。
  每个模型信号用其判定函数对统一新浪250根日K(不复权)重算全年，不用不完整归档。
  奥雅沿用已落地的全年全市场回测 oya_C_奥雅标准.json(本就是1-1模拟结果)。

输出 state/resonance_full_result.json。
"""
import os, sys, json, time, statistics, importlib.util
import concurrent.futures as cf
from collections import defaultdict, OrderedDict

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(REPO, "low_shadow", "state")
CACHE = os.path.join(OUT, "ohlcv_cache_2026.json")
RES = os.path.join(OUT, "resonance_full_result.json")
sys.path.insert(0, os.path.join(REPO, "low_shadow"))
import low_shadow_screener as m
m.DEMO = False
params = m.load_params()
FWD = [3, 5, 10]
WIN = 10


def load_mod(name, path):
    d = os.path.dirname(os.path.abspath(path))
    if d not in sys.path:
        sys.path.insert(0, d)   # 让模块内的 `import lib` / `import 埋伏点...` 可解析
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


t0 = time.time()
cache = json.load(open(CACHE, encoding="utf-8"))
DAYS = cache["days"]; uni = cache["uni"]; KL = cache["kl"]  # code -> [[day,o,h,l,c,v],...]
idx_of = {d: i for i, d in enumerate(DAYS)}
sym_of = {c: s for c, s, n in uni}
name_of = {c: n for c, s, n in uni}
lixin, _ = m.load_lixin_exclude()
print("数据就绪 交易日%d 股票%d | %.1fs" % (len(DAYS), len(uni), time.time()-t0), flush=True)

# ---------- 1) low_shadow 基准事件（复用已算好的1.3口径）----------
base = json.load(open(os.path.join(OUT, "combo_base_events.json"), encoding="utf-8"))
print("low_shadow基准 %d 条" % len(base), flush=True)

# ---------- 2) 价格/收益工具 ----------
cmap = {}
for code, rows in KL.items():
    cmap[code] = {r[0]: r[4] for r in rows}   # day -> close


def fwd_ret(code, buy, n):
    cm = cmap.get(code, {})
    if buy not in cm:
        return None
    ti = idx_of.get(buy)
    if ti is None or ti + n >= len(DAYS):
        return None
    td = DAYS[ti + n]
    if td not in cm:
        return None
    seg = DAYS[ti:ti+n+1]
    for j in range(1, len(seg)):
        a, b = seg[j-1], seg[j]
        if a in cm and b in cm and cm[a] > 0 and abs(cm[b]/cm[a]-1) > 0.32:
            return None
    return cm[td]/cm[buy] - 1


def stat(v):
    v = [x for x in v if x is not None]
    if not v:
        return {"n": 0, "hit": None, "mean": None, "median": None}
    return {"n": len(v),
            "hit": round(100.0*sum(1 for r in v if r > 0)/len(v), 2),
            "mean": round(statistics.mean(v)*100, 3),
            "median": round(statistics.median(v)*100, 3),
            "loss10": round(100.0*sum(1 for r in v if r < -0.10)/len(v), 2)}


# ---------- 3) 各模型信号重算：code -> set(signal_date) ----------
sig = {k: defaultdict(set) for k in ("kaizhong", "oya", "powei", "lingpei")}
meta = {}

# --- 奥雅：用已落地的全年全市场回测 oya_C（本就是1-1模拟） ---
oya = json.load(open(os.path.join(REPO, "stock-v3", "oya_C_奥雅标准.json"), encoding="utf-8"))
for x in oya:
    c = str(x["code"])[-6:].zfill(6)
    if x.get("date"):
        sig["oya"][c].add(x["date"])
meta["oya"] = {"source": "oya_C_奥雅标准.json(全年全市场回测)", "n_raw": len(oya),
               "range": [min(x["date"] for x in oya), max(x["date"] for x in oya)]}

# 需要重算的股票集合：low_shadow 涉及的股票（只需判断这些股票在其窗口内是否二次提示）
need = sorted(set(e["symbol"] for e in base))
print("需重算股票 %d 只" % len(need), flush=True)

# --- 凯众 v4（旧主力，v4_history来源）：stance_hint.scan_one(code, df, as_of) ---
try:
    import pandas as pd
    kz = load_mod("kz_stance", os.path.join(REPO, "kaizhong", "stance_hint.py"))
    kz_ok = 0
    for code in need:
        rows = KL.get(code) or []
        if len(rows) < 70:
            continue
        df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
        # 逐交易日判定（只在 low_shadow 事件后的窗口日期判定，省时）
        want = set()
        for e in base:
            if e["symbol"] != code:
                continue
            ti = idx_of.get(e["date"])
            if ti is None:
                continue
            want.update(DAYS[ti+1:ti+1+WIN] if ti+1 < len(DAYS) else [])
        for D in sorted(want):
            try:
                r = kz.scan_one(code, df, as_of=D)
                if r:
                    sig["kaizhong"][code].add(D)
            except Exception:
                pass
        kz_ok += 1
    meta["kaizhong"] = {"source": "stance_hint.scan_one(v4口径)", "n_stocks_scanned": kz_ok}
    print("凯众重算完成 %d 只 | 信号股 %d | %.1fs" %
          (kz_ok, len(sig["kaizhong"]), time.time()-t0), flush=True)
except Exception as ex:
    meta["kaizhong"] = {"error": str(ex)}
    print("凯众失败", ex, flush=True)

# --- 破位：powei.run.detect 的 sig_day(收复日)需事后约1个月数据才能识别(0.618失效/站稳确认)，
#     属回溯信号，sig_day 当天实盘不可知，用它做"第二次提示日买入"必然前视。
#     故改用生产实际推送日 = state/history.jsonl 的 date 字段(扫描归档日，无前视)，
#     代价是归档仅覆盖11月，样本极少，报告须标注。 ---
try:
    pw_hist = [json.loads(l) for l in open(os.path.join(REPO, "powei", "state", "history.jsonl"),
                                           encoding="utf-8") if l.strip()]
    pw_events = 0
    for x in pw_hist:
        c = str(x.get("code", "")).zfill(6)
        d = x.get("date")   # 生产推送/扫描日
        if d:
            sig["powei"][c].add(d)
            pw_events += 1
    meta["powei"] = {"source": "powei.state/history.jsonl 的 date(生产推送日,无前视; sig_day为事后回溯信号不可用)",
                     "n_events": pw_events,
                     "range": [min(x["date"] for x in pw_hist), max(x["date"] for x in pw_hist)]}
    print("破位(生产推送日) 事件%d 信号股%d | %.1fs" %
          (pw_events, len(sig["powei"]), time.time()-t0), flush=True)
except Exception as ex:
    meta["powei"] = {"error": str(ex)}
    print("破位失败", ex, flush=True)

# --- 领湃 detect_coil_v2：逐交易日截断判定（d/o/c/h/l/v） ---
try:
    lp = load_mod("lp_model", os.path.join(REPO, "lingpei", "回踩长期均线企稳模型.py"))
    lp_ok = 0
    for code in need:
        rows = KL.get(code) or []
        if len(rows) < 65:
            continue
        want = set()
        for e in base:
            if e["symbol"] != code:
                continue
            ti = idx_of.get(e["date"])
            if ti is None:
                continue
            want.update(DAYS[ti+1:ti+1+WIN] if ti+1 < len(DAYS) else [])
        for D in sorted(want):
            cmi = next((i for i, r in enumerate(rows) if r[0] == D), None)
            if cmi is None or cmi < 64:
                continue
            sub = rows[:cmi+1]
            bars = [{"d": r[0], "o": r[1], "c": r[4], "h": r[2], "l": r[3], "v": r[5]} for r in sub]
            try:
                r = lp.detect_coil_v2(bars)
                if r and r.get("signal"):
                    sig["lingpei"][code].add(D)
            except Exception:
                pass
        lp_ok += 1
    meta["lingpei"] = {"source": "回踩长期均线企稳模型.detect_coil_v2(V3.4.1)", "n_stocks_scanned": lp_ok}
    print("领湃重算完成 %d 只 | 信号股 %d | %.1fs" %
          (lp_ok, len(sig["lingpei"]), time.time()-t0), flush=True)
except Exception as ex:
    meta["lingpei"] = {"error": str(ex)}
    print("领湃失败", ex, flush=True)

print("\n各模型信号股票数:", {k: len(v) for k, v in sig.items()}, flush=True)

# ---------- 4) 第二次提示日匹配 + 收益 ----------
res = {k: [] for k in sig}
for e in base:
    code, T = e["symbol"], e["date"]
    ti = idx_of.get(T)
    if ti is None:
        continue
    lo = DAYS[ti+1] if ti+1 < len(DAYS) else "9999"
    hi = DAYS[min(ti+WIN, len(DAYS)-1)]
    for k in sig:
        cand = sorted(d for d in sig[k].get(code, ()) if lo <= d <= hi)
        if not cand:
            continue
        R = cand[0]
        ev = {"T": T, "code": code, "name": name_of.get(code, e.get("name", "")),
              "model": k, "R": R, "gap": idx_of.get(R, ti)-ti}
        ev["rets"] = {str(n): fwd_ret(code, R, n) for n in FWD}
        res[k].append(ev)

# 基准（low_shadow 单独，T买点）
base_rets = {str(n): [fwd_ret(e["symbol"], e["date"], n) for e in base] for n in FWD}
base_stat = {str(n): stat(base_rets[str(n)]) for n in FWD}

# 各模型统计（R买点=第二次提示日）
res_stat = {}
for k in sig:
    res_stat[k] = {"n_events": len(res[k]), "n_stocks": len(set(e["code"] for e in res[k])),
                   "gap_median": statistics.median([e["gap"] for e in res[k]]) if res[k] else None,
                   **{("r%d" % n): stat([e["rets"][str(n)] for e in res[k]]) for n in FWD}}

# 任意模型（去重：同一(T,code)取最早R）
seen = {}
for k in sig:
    for e in res[k]:
        key = (e["T"], e["code"])
        if key not in seen or e["R"] < seen[key]["R"]:
            seen[key] = e
any_events = sorted(seen.values(), key=lambda x: (x["R"], x["code"]))
any_stat = {"n_events": len(any_events), "n_stocks": len(set(e["code"] for e in any_events)),
            **{("r%d" % n): stat([e["rets"][str(n)] for e in any_events]) for n in FWD}}
coverage = round(100.0*len(any_events)/len(base), 2) if base else 0

out = {
    "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    "definition": "low_shadow(T)后[T+1,T+10]内其它模型再次提示的最早日R；收益从R收盘算R+3/5/10",
    "range": [DAYS[0], DAYS[-1]], "n_days": len(DAYS), "universe": len(uni),
    "params": {"vol_ratio_min": params["vol_ratio_min"]},
    "model_meta": meta,
    "signal_stocks": {k: len(v) for k, v in sig.items()},
    "base": {"n_events": len(base), "stats": base_stat},
    "reson": res_stat,
    "any": any_stat,
    "coverage_pct": coverage,
    "any_samples": any_events[:80],
}
json.dump(out, open(RES, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("\n保存", RES, flush=True)
print("\n=== 结果：以第二次提示日R为买点 ===")
print("基准 low_shadow(T买点):", {n: "%s%%/%s%%" % (base_stat[n]["hit"], base_stat[n]["mean"]) for n in ("3","5","10")})
for k in sig:
    s = res_stat[k]
    print("  %-9s 二次提示%3d(股%d) 间隔中位%s | 5日 胜%s%% 均%s%% n=%d" %
          (k, s["n_events"], s["n_stocks"], s["gap_median"],
           s["r5"]["hit"], s["r5"]["mean"], s["r5"]["n"]))
print("  任意 %d 覆盖%.2f%% | 5日 胜%s%% 均%s%% n=%d" %
      (len(any_events), coverage, any_stat["r5"]["hit"], any_stat["r5"]["mean"], any_stat["r5"]["n"]))
