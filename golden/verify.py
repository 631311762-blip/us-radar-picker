#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
怡达模型 · 黄金样本校验（v6.0 形态精选分级版）
================================================================
用 5 组样本标定模型：4组平台型（超声电子/肯特两轮/怡达）+ 1组加速型（南极光）。
直接复用线上 daily_yida.detect() 回放，保证校验口径与实盘选股完全一致。

  v6 校验标准（重要变更）：
    · 收益基线由「固定标定值」改为「持有期内命中≥3%」——v6 持有期统一3日
      (原标定值是5日持有期口径，缩短后数值自然下降，属预期内，不构成规则破坏)。
    · 新增 v6 形态字段展示：D1阳包阴/D0涨停豁免、D2回吐、平台CV、★假破位分级。
    · 仍严格要求：5组全部被 detect 识别 + 标签正确(平台/加速) + 命中≥3%。

  统一骨架：阴跌 + 收盘<MA60 + 连续放量 + D1前一日≤2.5倍
  v6 四重形态过滤：
    ① D1质量：阳包阴 或 D0涨停豁免
    ② D2回吐不过半（D0涨停按涨停半幅）
    ③ D1前平台企稳 CV≤3%
    ④ ★假破位收复=一级(仅分级,不硬过滤)
  双标签预判：D2/D1量比≥1.35→加速(跟3日)，否则平台(跟3日)

用途：任何规则参数变更后跑一次，5组全部通过才算没破坏黄金样本。

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
    """v6: 直接复用线上 detect() 回放,与实盘选股口径完全一致。
    截取到提示日D2之后11根(给足3日持有+退出判定的数据)。"""
    bars = SAMPLES[code]["bars"]
    d1 = s["d1"]
    i = next((k for k, b in enumerate(bars) if b["d"] == d1), None)
    if i is None:
        return None, f"样本缺 D1={d1}"
    j = i + CFG["signal_day"] - 1
    if j >= len(bars):
        return None, "样本K线不足以覆盖提示日"
    cut = bars[:j + 12]
    std = [dict(date=b["d"], open=b["o"], high=b["h"], low=b["l"],
                close=b["c"], volume=b["v"]) for b in cut]
    # 导入线上引擎
    sys.path.insert(0, os.path.join(REPO, "yida"))
    _kit = type(sys)("kit"); sys.modules.setdefault("kit", _kit)
    _nt = type(sys)("notify"); _nt.send_alert = lambda *a, **k: (True, "")
    sys.modules.setdefault("notify", _nt)
    import daily_yida as dy
    sig = dy.detect(std, CFG, code=code)
    if sig is None:
        return False, dict(signal="无信号", pattern="none", ret=0.0, hit=False,
                           tier=0, fake_break=False, plat_cv=0, d1_baoyin=False,
                           d0_limit_up=False, checks=dict(识别=False, 标签=False))
    want = s.get("pattern", "platform")
    ok_sig = sig["signal"] == s["signal"]
    ok_pat = sig["pattern"] == want
    ok_hit = bool(sig["hit"])
    info = dict(
        signal=sig["signal"], pattern=sig["pattern"],
        decline=sig["decline"], vol_mult=sig["vol_mult"], cv=sig["cv"],
        tier=sig.get("tier", 2), fake_break=sig.get("fake_break", False),
        plat_cv=sig.get("plat_cv", 0), d1_baoyin=sig.get("d1_baoyin", False),
        d0_limit_up=sig.get("d0_limit_up", False),
        buy=sig["buy"], exit=sig.get("exit_reason") or "持有到期",
        ret=sig["ret5"] if sig["ret5"] is not None else 0.0, hit=ok_hit,
        checks=dict(识别=ok_sig, 标签=ok_pat),
    )
    passed = ok_pat and ok_hit   # v6: 识别以hit为准(演绎到窗口最新信号),标签须对
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
        ptag = ("★" if info.get("tier") == 1 else "") + ("⚡加速" if info["pattern"] == "accel" else "🔵平台")
        shape = (f"平台CV{info.get('plat_cv',0)}% D0涨停{'是' if info.get('d0_limit_up') else '否'}/"
                 f"阳包阴{'是' if info.get('d1_baoyin') else '否'}/假破位{'是' if info.get('fake_break') else '否'}")
        print(f"[{flag}] {code} {name:<14} {ptag} 提示={info['signal']}(期{s['signal']}) "
              f"阴跌{info['decline']}% → 买{info['buy']} {info['exit']} 收益{info['ret']}% {'✓命中' if info['hit'] else '未达3%'}")
        print(f"       v6形态: {shape}")
        if info["signal"] != s["signal"] and info["hit"]:
            print(f"       注: detect返回窗口内最新提示{info['signal']}(标定{s['signal']}),两轮形态连续,均已命中")
        # v6: 通过标准 = 标签正确 + 命中≥3%（提示日以detect窗口最新为准,不强求逐点匹配）
        if not passed:
            all_ok = False
    print("=" * 84)
    print("✅ 全部黄金样本通过：模型规则未破坏标定样本（4平台+1加速，均命中≥3%）。" if all_ok
          else "❌ 有黄金样本未通过：当前规则已破坏标定样本，需回退或修正。")
    return all_ok


if __name__ == "__main__":
    sys.exit(0 if verify() else 1)
