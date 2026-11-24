#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
怡达模型 · 黄金样本校验（v5 双标签版）
================================================================
用 5 组样本标定模型：4组平台型（超声电子/肯特两轮/怡达）+ 1组加速型（南极光）。
按当前 config.json 的 v5 规则逐条核验每个 D1 锚点：

  统一骨架：阴跌 + 收盘<MA60 + 连续4天≥2倍放量 + D1前一日≤2.5倍
  双标签分流（互斥）：
    · platform 平台型：CV<20%，阴跌≥30%，D1涨停豁免/2~7倍，D2~D4严格2~7倍，跟踪5日破位退出
    · accel 加速型：CV≥20% 且四天逐日递增，阴跌≥20%，跟踪3日固定止盈

用途：任何规则参数变更后跑一次，5组全部通过才算没破坏黄金样本。
判定逻辑与 yida/daily_yida.py 的 detect() 保持一致（同一套 v5 规则）。

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
    prev = c[i - 1]; dec = prev / peak - 1
    vols = v[i:i + 4]; ratios = vols / base
    cv = np.std(vols) / np.mean(vols)
    accel = bool(vols[0] < vols[1] < vols[2] < vols[3])
    d1lu = c[i] / c[i - 1] - 1 >= limit_thr(code)
    cpre = v[i - 1] / base <= 2.5
    # 统一骨架：空头排列 + 前置量；放量下限在分支里判定（平台型D1涨停可豁免）
    c_bone = prev < ma60 and cpre

    # 双标签分流核验
    want = s.get("pattern", "platform")
    if cv < CFG["cv_max"]:
        pat = "platform"
        c_pat = (dec <= -CFG["decline_min"] and
                 (d1lu or CFG["vol_lo"] <= ratios[0] <= CFG["vol_hi"]) and
                 np.all((ratios[1:] >= CFG["vol_lo"]) & (ratios[1:] <= CFG["vol_hi"])))
        pat_hold = CFG["hold_days"]
    elif accel:
        pat = "accel"
        c_pat = (dec <= -CFG.get("accel_decline_min", 0.20)
                 and np.all(ratios >= CFG["vol_lo"]))
        pat_hold = CFG.get("accel_hold_days", 3)
    else:
        pat, c_pat, pat_hold = "none", False, CFG["hold_days"]

    j = i + CFG["signal_day"] - 1
    buy = c[j]
    bl = (o[i] + c[i]) / 2; sl = np.mean(vols) * CFG["shrink_ratio"]
    # v5.2：直接复用线上 exit_sim，保证收益口径与 detect 完全一致
    try:
        sys.path.insert(0, os.path.join(REPO, "yida"))
        _kit = type(sys)("kit"); sys.modules.setdefault("kit", _kit)
        _nt = type(sys)("notify"); _nt.send_alert = lambda *a, **k: (True, "")
        sys.modules.setdefault("notify", _nt)
        import daily_yida as dy
        ret, exit_reason, _, _, maxp = dy.exit_sim(
            o, h, c, v, [b["d"] for b in bars], j, buy,
            "accel" if pat == "accel" else "platform", pat_hold, CFG,
            bl, sl, len(c))
    except Exception:
        # 回退：旧口径（最高价收益）
        maxp = h[j]; exit_reason = None
        for k in range(j + 1, min(j + 1 + pat_hold, len(c))):
            maxp = max(maxp, h[k])
            if pat == "platform":
                if c[k] < bl: exit_reason = "破位"; break
                if v[k] < sl: exit_reason = "缩量"; break
        ret = (maxp / buy - 1) * 100
    hit = ret >= CFG["target"] * 100
    info = dict(
        signal=bars[j]["d"], pattern=pat,
        d1_chg=round(float(c[i] / c[i - 1] - 1) * 100, 1),
        decline=round(float(dec) * 100, 1), vol_mult=[round(float(x), 2) for x in ratios],
        cv=round(float(cv) * 100, 1), accel=accel, d1lu=d1lu, buy=round(float(buy), 2),
        exit=exit_reason or "持有到期", ret=round(float(ret), 1), hit=hit,
        checks=dict(骨架=c_bone, 分支=c_pat, 标签=(pat == want)),
    )
    passed = c_bone and c_pat and (pat == want) and hit
    return passed, info


def verify():
    print("=" * 84)
    print(f"怡达模型黄金样本校验 · {CFG.get('_version', '未标注')}")
    print(f"平台型: 阴跌≥{CFG['decline_min']*100:.0f}% CV<{CFG['cv_max']*100:.0f}% 持有{CFG['hold_days']}日破位退出 ｜ "
          f"加速型: 阴跌≥{CFG.get('accel_decline_min',0.2)*100:.0f}% 递增 持有{CFG.get('accel_hold_days',3)}日 ｜ "
          f"命中≥{CFG['target']*100:.0f}%")
    print("=" * 84)
    all_ok = True
    for s in BENCH["samples"]:
        code, name = s["code"], s["name"]
        passed, info = verify_one(code, s)
        if info is None:
            print(f"[×] {code} {name:<12} {passed}")
            all_ok = False
            continue
        flag = "✔" if passed else "×"
        ptag = "⚡加速" if info["pattern"] == "accel" else "🔵平台"
        print(f"[{flag}] {code} {name:<14} {ptag} D1={s['d1']} 提示={info['signal']}(期望{s['signal']}) "
              f"阴跌{info['decline']}% CV{info['cv']}% → 买{info['buy']} {info['exit']} "
              f"收益{info['ret']}% (标定{s['hold_ret_pct']}%)")
        miss = [k for k, ok in info["checks"].items() if not ok]
        if miss:
            print(f"       未通过项: {', '.join(miss)}{' + 未达命中线' if not info['hit'] else ''}")
        if info["signal"] != s["signal"] or abs(info["ret"] - s["hold_ret_pct"]) >= 0.5:
            print(f"       提示日/收益与标定不符！")
            all_ok = False
        if not passed:
            all_ok = False
    print("=" * 84)
    print("✅ 全部黄金样本通过：模型规则未破坏标定样本（4平台+1加速）。" if all_ok
          else "❌ 有黄金样本未通过：当前规则已破坏标定样本，需回退或修正。")
    return all_ok


if __name__ == "__main__":
    sys.exit(0 if verify() else 1)
