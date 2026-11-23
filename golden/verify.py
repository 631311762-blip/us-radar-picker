#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
怡达模型 · 黄金样本校验（v3 单锚点核验）
================================================================
用超声电子/肯特股份/怡达股份（含肯特两轮形态）作为标定样本，
按当前 config.json 的 v3 规则逐条核验每个 D1 锚点：
  ① 阴跌≥decline_min 且收盘<MA60
  ② D1~D4 放量倍数在 [vol_lo, vol_hi]，或 D1 涨停触发豁免
  ③ CV < cv_max
  ④ D1前一日量 ≤ 2.5 倍
  ⑤ D2 提示后按退出机制结算，关注期收益 ≥ target

用途：任何规则参数变更后跑一次，三只全部通过才算没破坏黄金样本。
判定逻辑与 yida/daily_yida.py 的 detect() 保持一致（同一套 v3 规则）。

用法：
  python3 golden/verify.py
"""
import os, sys, json
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CFG = json.load(open(os.path.join(REPO, "yida", "config.json"), encoding="utf-8"))
BENCH = json.load(open(os.path.join(HERE, "benchmark.json"), encoding="utf-8"))
SAMPLES = json.load(open(os.path.join(HERE, "samples.json"), encoding="utf-8"))


def limit_thr(code):
    s = str(code).zfill(6)
    if s.startswith(("300", "301", "688", "689")):
        return CFG["limit_up_growth"]
    if s.startswith(("8", "4", "92")):
        return CFG["limit_up_bj"]
    return CFG["limit_up_main"]


def verify_one(code, s):
    bars = SAMPLES[code]["bars"]
    d1 = s["d1"]
    i = next((k for k, b in enumerate(bars) if b["d"] == d1), None)
    if i is None:
        return None, f"样本缺 D1={d1}"
    o = np.array([b["o"] for b in bars]); h = np.array([b["h"] for b in bars])
    c = np.array([b["c"] for b in bars]); v = np.array([b["v"] for b in bars])
    bw, lb = CFG["base_win"], CFG["lookback"]
    base = np.mean(v[i - bw:i]); peak = np.max(h[i - lb:i]); ma60 = np.mean(c[i - lb:i])
    prev = c[i - 1]
    dec = prev / peak - 1
    c1 = dec <= -CFG["decline_min"] and prev < ma60
    vols = v[i:i + 4]; ratios = vols / base
    d1lu = c[i] / c[i - 1] - 1 >= limit_thr(code)
    c2 = ((d1lu or CFG["vol_lo"] <= ratios[0] <= CFG["vol_hi"])
          and np.all((ratios[1:] >= CFG["vol_lo"]) & (ratios[1:] <= CFG["vol_hi"])))
    cv = np.std(vols) / np.mean(vols); c3 = cv <= CFG["cv_max"]
    cpre = v[i - 1] / base <= 2.5
    j = i + CFG["signal_day"] - 1
    buy = c[j]
    bl = (o[i] + c[i]) / 2; sl = np.mean(vols) * CFG["shrink_ratio"]
    maxp = h[j]; exit_reason = None
    for k in range(j + 1, min(j + 1 + CFG["hold_days"], len(c))):
        maxp = max(maxp, h[k])
        if c[k] < bl:
            exit_reason = "破位"; break
        if v[k] < sl:
            exit_reason = "缩量"; break
    ret = (maxp / buy - 1) * 100
    hit = ret >= CFG["target"] * 100
    info = dict(
        signal=bars[j]["d"], d1_chg=round(float(c[i] / c[i - 1] - 1) * 100, 1),
        decline=round(float(dec) * 100, 1), vol_mult=[round(float(x), 2) for x in ratios],
        cv=round(float(cv) * 100, 1), d1lu=d1lu, buy=round(float(buy), 2),
        exit=exit_reason or "持有到期", ret=round(float(ret), 1), hit=hit,
        checks=dict(阴跌=c1, 放量=c2, CV=c3, 前置量=cpre),
    )
    passed = c1 and c2 and c3 and cpre and hit
    return passed, info


def verify():
    print("=" * 78)
    print(f"怡达模型黄金样本校验 · {CFG.get('_version', '未标注')}")
    print(f"参数: 阴跌≥{CFG['decline_min']*100:.0f}%  放量{CFG['vol_lo']}~{CFG['vol_hi']}x  "
          f"CV<{CFG['cv_max']*100:.0f}%  D{CFG['signal_day']}提示  持有{CFG['hold_days']}日  命中≥{CFG['target']*100:.0f}%")
    print("=" * 78)
    all_ok = True
    for s in BENCH["samples"]:
        code, name = s["code"], s["name"]
        passed, info = verify_one(code, s)
        if info is None:
            print(f"[×] {code} {name:<12} {info if False else ''}{''}")
            print(f"    → {passed}")  # passed 此时是错误信息
            all_ok = False
            continue
        flag = "✔" if passed else "×"
        print(f"[{flag}] {code} {name:<12} D1={s['d1']} 提示={info['signal']}(期望{s['signal']}) "
              f"阴跌{info['decline']}% 量{info['vol_mult']} CV{info['cv']}% "
              f"→ 买{info['buy']} {info['exit']} 收益{info['ret']}% (标定{s['hold_ret_pct']}%)")
        checks = info["checks"]
        miss = [k for k, ok in checks.items() if not ok]
        if miss:
            print(f"       未通过项: {', '.join(miss)}"
                  f"{' + 未达命中线' if not info['hit'] else ''}")
        if info["signal"] != s["signal"] or abs(info["ret"] - s["hold_ret_pct"]) >= 0.5:
            print(f"       提示日/收益与标定不符！")
            all_ok = False
        if not passed:
            all_ok = False
    print("=" * 78)
    print("✅ 全部黄金样本通过：模型规则未破坏三只标定样本。" if all_ok
          else "❌ 有黄金样本未通过：当前规则已破坏标定样本，需回退或修正。")
    return all_ok


if __name__ == "__main__":
    sys.exit(0 if verify() else 1)
