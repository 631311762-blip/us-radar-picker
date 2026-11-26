# -*- coding: utf-8 -*-
"""
怡达模型 · 评分信号期后结算台账（v7.0）
=====================================================================
后台每日运行，完成两件事：
  1) record : 把当日所有 D2 新提示（含评分、是否入选Top6、当日排名）落库
  2) settle : 对台账中 signal 已满 3/5 个交易日的样本，用真实行情回填
              c3/c5（收盘收益）、h3/h5（期间最高价收益），标记 settled
  3) report : 输出 Top6组 / 非Top6组 / 全样本 的结算对比，供后续优化评分

结算口径（与回测 tighten_tiers.py 完全一致）：
  买价 = signal日次日(D3)开盘价；k3=signal+3交易日, k5=signal+5交易日
  c3=(C[k3]/buy-1)*100 ; h3=(max(H[D3:k3])/buy-1)*100 （5日同理）

用法：
  python3 yida/settle_scores.py record  --signals <当日信号json> --date 2026-11-26
  python3 yida/settle_scores.py settle                      # 回填所有到期样本
  python3 yida/settle_scores.py report                      # 输出结算对比
  python3 yida/settle_scores.py run --signals <json> --date ...  # record+settle+report
=====================================================================
"""
import os, json, sys, argparse, glob

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # /workspace/yida-model
LEDGER = os.path.join(BASE, "yida", "score_ledger.json")


def _load():
    if os.path.exists(LEDGER):
        return json.load(open(LEDGER, encoding="utf-8"))
    return {"_meta": {"created": "", "version": "v7.0", "desc": "怡达评分信号期后结算台账"}, "records": []}


def _save(d):
    json.dump(d, open(LEDGER, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def _key(r):
    return (r["code"], r["signal"])


def record(signals, date, top6_codes=None):
    """落库当日所有新提示。signals: list of detect dict (含评分p)。top6_codes: 入选Top6的代码集合。"""
    d = _load()
    existing = {_key(r) for r in d["records"]}
    # 当日排名（按评分降序）
    srt = sorted(signals, key=lambda s: -s.get("score_p", 0))
    rank = {s["code"]: i + 1 for i, s in enumerate(srt)}
    top6 = set(top6_codes or [])
    added = 0
    for s in srt:
        k = (s["code"], s.get("signal", date))
        if k in existing:
            continue
        d["records"].append({
            "code": s["code"], "name": s.get("name", ""), "signal": s.get("signal", date),
            "pattern": s.get("pattern", "platform"), "tier": s.get("tier", 2),
            "decline": s.get("decline"), "vol_mult": s.get("vol_mult"),
            "d1_chg": s.get("d1_chg"), "r21": s.get("r21", s.get("d2_vol_ratio")),
            "score_p": s.get("score_p"), "rank_in_day": rank.get(s["code"]),
            "in_top6": s["code"] in top6,
            "buy": s.get("buy"),
            "settled": False, "c3": None, "c5": None, "h3": None, "h5": None,
        })
        added += 1
    if not d["_meta"].get("created"):
        d["_meta"]["created"] = date
    _save(d)
    return added


def _load_bars(code, cache):
    """从缓存取单只股票完整OHLCV。cache: {code:{days,o,h,l,c,v}}"""
    return cache.get(code)


def settle(cache_path=None):
    """对所有未结算且已满5个交易日的样本回填真实收益。"""
    import pickle
    if cache_path is None:
        candidates = [
            "/root/.codebuddy/artifact/2f8a7d30-9c21-4f0b-1d7c-8f3ba79e8000/market_2025_2026.pkl",
            os.path.join(BASE, "market_2025_2026.pkl"),
        ]
        cache_path = next((p for p in candidates if os.path.exists(p)), None)
    if not cache_path:
        print("[settle] 未找到行情缓存，无法结算")
        return 0
    cache = pickle.load(open(cache_path, "rb"))
    d = _load()
    done = 0
    for r in d["records"]:
        if r["settled"]:
            continue
        kd = _load_bars(r["code"], cache)
        if not kd:
            continue
        ds = kd["days"]
        if r["signal"] not in ds:
            continue
        j = ds.index(r["signal"])
        bi = j + 1  # D3
        k3, k5 = j + 3, j + 5
        # 必须满5个交易日才结算（否则h5未定型）
        if k5 >= len(ds):
            continue
        buy = float(kd["o"][bi])
        if buy <= 0:
            continue
        r["buy"] = round(buy, 2)
        r["c3"] = round((float(kd["c"][k3]) / buy - 1) * 100, 2)
        r["c5"] = round((float(kd["c"][k5]) / buy - 1) * 100, 2)
        r["h3"] = round((max(float(kd["h"][t]) for t in range(bi, k3 + 1)) / buy - 1) * 100, 2)
        r["h5"] = round((max(float(kd["h"][t]) for t in range(bi, k5 + 1)) / buy - 1) * 100, 2)
        r["settled"] = True
        r["settle_last_bar"] = ds[k5]
        done += 1
    _save(d)
    return done


def report():
    d = _load()
    rec = [r for r in d["records"] if r["settled"]]
    if not rec:
        print("[report] 暂无已结算样本")
        return
    import numpy as np
    def stat(g, label):
        if not g:
            return
        h3 = np.array([x["h3"] for x in g]); h5 = np.array([x["h5"] for x in g])
        c3 = np.array([x["c3"] for x in g]); c5 = np.array([x["c5"] for x in g])
        print(f"{label:<14} n={len(g):>5} | h3={h3.mean():+6.2f}%(≥5%:{(h3>=5).mean()*100:4.0f}%) "
              f"h5={h5.mean():+6.2f}%(≥10%:{(h5>=10).mean()*100:4.0f}%) | c3={c3.mean():+6.2f}% c5={c5.mean():+6.2f}%")
    print(f"\n=== 怡达评分台账结算报告 (已结算 {len(rec)} / 总 {len(d['records'])}) ===")
    stat(rec, "全样本")
    stat([x for x in rec if x["in_top6"]], "Top6入选")
    stat([x for x in rec if not x["in_top6"]], "非Top6")
    # 按评分区间
    ps = sorted(rec, key=lambda x: -(x["score_p"] or 0))
    q = len(ps) // 3
    if q > 0:
        stat(ps[:q], "评分高1/3")
        stat(ps[-q:], "评分低1/3")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["record", "settle", "report", "run"])
    ap.add_argument("--signals", help="当日信号json文件")
    ap.add_argument("--date", help="信号日期")
    ap.add_argument("--top6", help="入选Top6代码(逗号分隔)")
    a = ap.parse_args()
    if a.cmd in ("record", "run") and not a.signals:
        print("record/run 需要 --signals"); return
    sigs = json.load(open(a.signals, encoding="utf-8")) if a.signals else []
    if isinstance(sigs, dict):
        sigs = sigs.get("signals", sigs.get("all_signals", []))
    t6 = set(a.top6.split(",")) if a.top6 else set()
    if a.cmd in ("record", "run"):
        n = record(sigs, a.date or "", t6)
        print(f"[record] 落库 {n} 条新信号")
    if a.cmd in ("settle", "run"):
        n = settle()
        print(f"[settle] 回填 {n} 条已到期样本")
    if a.cmd in ("report", "run"):
        report()


if __name__ == "__main__":
    main()
