# -*- coding: utf-8 -*-
"""回溯口径统一规范 v2.0 —— 所有模型共用一把尺子。

口径（用户 2026-11-26 确立，见 /workspace/回溯口径统一规范_v2_2026-11-26.md）：
  - 买入：D+1（提示日后第一根K线）**开盘价** base = bars[idx]["open"]；当天不可卖。
  - 卖出：1/3/5日 -> D+2/D+4/D+6，各算【最高价】和【收盘价】两套；
          10日 -> D+11，只算【收盘价】。卖出日 j = idx + h。
  - 收益率 R = (卖出价 - base)/base * 100。
  - 胜率：R > 3% 才算胜（WIN_THRESHOLD=0.03）。
  - 复权：前复权(qfq)。
"""
from __future__ import annotations

WIN_THRESHOLD = 0.03                 # 收益率 > 3% 才算胜
HORIZONS = (1, 3, 5, 10)
DUAL_PRICE_H = (1, 3, 5)             # 这几个周期算 high+close 双口径
CLOSE_ONLY_H = (10,)                 # 只算收盘价


def _date_key(b):
    return str(b.get("date"))[:10]


def find_d1_index(bars, signal_date):
    """定位 D+1：提示日 D0 之后第一根 K 线的下标。signal_date 允许 YYYY-MM-DD / YYYYMMDD。"""
    sd = str(signal_date).replace("-", "")[:8]
    for i, b in enumerate(bars):
        if _date_key(b).replace("-", "")[:8] == sd:
            # 找到 D0，返回其后一根（D+1）
            return i + 1
    return None


def evaluate_signal(signal_date, code, bars, win_threshold=WIN_THRESHOLD):
    """单信号按 v2 口径回溯。返回 dict 或 None。"""
    if not bars:
        return None
    idx = find_d1_index(bars, signal_date)   # idx 即 D+1 买入日
    if idx is None or idx >= len(bars):
        return None
    buy = bars[idx]
    base = float(buy.get("open") or 0)
    if base <= 0:
        return None
    out = {
        "code": str(code),
        "signal_date": _date_key(bars[idx - 1]),
        "buy_date": _date_key(buy),
        "buy_price": round(base, 4),
    }
    for h in HORIZONS:
        j = idx + h                          # 1->D+2, 3->D+4, 5->D+6, 10->D+11
        if j >= len(bars):
            for suf in (("high", "close") if h in DUAL_PRICE_H else ("close",)):
                out[f"r{h}_{suf}"] = None
                out[f"sell_date_{h}"] = None
            continue
        sell = bars[j]
        out[f"sell_date_{h}"] = _date_key(sell)
        prices = {}
        if h in DUAL_PRICE_H:
            prices["high"] = float(sell.get("high") or 0)
            prices["close"] = float(sell.get("close") or 0)
        else:  # h == 10
            prices["close"] = float(sell.get("close") or 0)
        for suf, px in prices.items():
            out[f"r{h}_{suf}"] = round((px - base) / base * 100.0, 4) if px > 0 else None
    return out


def _stat(vals, win_threshold):
    vals = [v for v in vals if v is not None]
    if not vals:
        return {"n": 0, "wins": 0, "win_rate": None, "avg_ret": None}
    wins = sum(1 for v in vals if v / 100.0 > win_threshold)
    return {
        "n": len(vals),
        "wins": wins,
        "win_rate": round(wins / len(vals) * 100.0, 2),
        "avg_ret": round(sum(vals) / len(vals), 4),
    }


def summarize(records, win_threshold=WIN_THRESHOLD):
    """批量统计。返回 7 组指标：t1_high/t1_close/.../t10_close。"""
    keys = []
    for h in HORIZONS:
        keys += [f"r{h}_high", f"r{h}_close"] if h in DUAL_PRICE_H else [f"r{h}_close"]
    out = {}
    for key in keys:
        out[key] = _stat([r.get(key) for r in records], win_threshold)
    return out


if __name__ == "__main__":
    import json, sys
    sys.path.insert(0, "/tmp/bt_env")
    import panel
    p = panel.load_panel()
    res = json.load(open("/tmp/bt_env/all_results.json"))
    for code_key in ("powei", "low_shadow2"):
        recs = []
        for ds, code in res[code_key]["signals"]:
            bars = p.get(code)
            if not bars:
                continue
            r = evaluate_signal(ds, code, bars)
            if r:
                recs.append(r)
        s = summarize(recs)
        print(f"=== {code_key} 样本={len(recs)} ===")
        for k, v in s.items():
            print("  %-12s n=%-5d win=%-6s avg=%s" % (k, v["n"], v["win_rate"], v["avg_ret"]))
