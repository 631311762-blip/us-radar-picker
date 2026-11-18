# -*- coding: utf-8 -*-
"""
缩量二次突破模型 (Shrink-Volume Double Breakout, SVDB) · 独立扫描器

来源：2016-2026 全A股回测验证版本（同一数据同口径对比，10日胜率 63.2%、
平均收益 +4.51%、盈亏比 1.61；同期凯众v4放量突破仅 41.7% / +0.29%）。
用户 2026-11-18 决定用它替换凯众 v4。

形态链条：
  1) 横盘平台（25 日，高低振幅 ≤0.40）
  2) 放量假突破：量 ≥ 平台均量 ×1.8、最高价 ≥ 平台上沿 ×1.015，
     且冲高回落（上影）或数日内收盘跌回平台上沿 = 套人
  3) ★突破强度 high[b1]/平台上沿 ≥ 1.15（回测验证的唯一核心胜负手）
  4) 洗盘 5~45 日
  5) 缩量二次上攻（提示日 T）：最高价 ≥ 平台上沿 ×0.97、收盘 ≥ ×0.90，
     且成交量 ≤ 首次突破量 ×0.60（浮筹洗净，少量资金即可拉升）
  提示日 T 即买点，收盘价买入，止损 -5%。

接口与 stance_hint.py 完全对齐（STANCE 参数 + scan_one），
供 daily_v4.py 无缝调用；台账/复盘/推送/合规剔除等运维框架保持不变。
"""
import os
import json
import glob
from datetime import datetime

import numpy as np
import pandas as pd

import lib

# -------- 参数（回测最优，勿随意修改）--------
STANCE = {
    # 平台
    "plat_days": 25,             # 平台观察窗口（交易日）
    "plat_amp_max": 0.40,        # 平台高低振幅上限 (high-low)/mid
    # 首次放量假突破
    "break_vol_ratio": 1.8,      # 首突量 ≥ 平台均量 × 该值
    "break_high_ratio": 1.015,   # 首突最高价 ≥ 平台上沿 × 该值
    "upper_shadow": 1.035,       # 上影判定 high/close > 该值
    "fail_days": 8,              # 假突破确认窗口（收盘跌回平台上沿）
    "min_strength": 1.15,        # ★核心：突破强度 high[b1]/平台上沿 下限
    # 缩量二次上攻（提示日）
    "min_gap": 5,                # 提示日距首突最小间隔
    "max_gap": 45,               # 提示日距首突最大间隔
    "approach": 0.97,            # 提示日最高价 ≥ 平台上沿 × 该值
    "close_conf": 0.90,          # 提示日收盘 ≥ 平台上沿 × 该值
    "shrink": 0.60,              # ★核心：提示日量 ≤ 首突量 × 该值
    # 风控
    "stop_loss": 0.05,
}


def _signal_at_last_bar(df, as_of=None):
    """站在最后一根K线，判断当日是否为缩量二次突破提示日。返回特征 dict 或 None。

    严格复刻回测 detect_signals 在 t=当日 的判定：平台上沿不含首突当日；
    从最近的候选首突 b1 往回找，取第一个满足「假突破 + 强度 + 缩量二次上攻」者。
    """
    df = df.reset_index(drop=True)
    df["date"] = df["date"].astype(str)
    if as_of:
        df = df[df["date"] <= str(as_of)]
    if len(df) < 70:
        return None
    df = df.tail(160).reset_index(drop=True)

    c = df["close"].values.astype(float)
    v = df["volume"].values.astype(float)
    h = df["high"].values.astype(float)
    lo = df["low"].values.astype(float)
    d = df["date"].values
    n = len(df)
    t = n - 1                      # 今日（提示日候选）

    if c[t] < lib.MIN_PRICE:
        return None
    if float((c[-20:] * v[-20:]).mean()) < lib.MIN_AMOUNT:
        return None

    plat = STANCE["plat_days"]
    # b1 ∈ [max(plat, t-max_gap), t-min_gap]，从近到远
    start = t - STANCE["min_gap"]
    end = max(plat, t - STANCE["max_gap"])
    for b1 in range(start, end - 1, -1):
        ps, pe = b1 - plat, b1
        ph = float(h[ps:pe].max())       # 平台上沿（不含首突当日）
        pl = float(lo[ps:pe].min())
        pv = float(v[ps:pe].mean())
        pmid = (ph + pl) / 2
        amp = (ph - pl) / max(pmid, 1e-9)
        if amp > STANCE["plat_amp_max"]:
            continue
        # 首次放量假突破
        if not (h[b1] > ph * STANCE["break_high_ratio"]
                and v[b1] > pv * STANCE["break_vol_ratio"]):
            continue
        strength = float(h[b1] / ph)
        if strength < STANCE["min_strength"]:
            continue
        # 假突破确认：上影 或 数日内收盘跌回平台上沿
        us = h[b1] / max(c[b1], 1e-9) > STANCE["upper_shadow"]
        fb = bool(np.any(c[b1:min(b1 + STANCE["fail_days"], n)] < ph))
        if not (us or fb):
            continue
        # 提示日 T：缩量二次上攻至平台上沿附近
        if h[t] < ph * STANCE["approach"]:
            continue
        if c[t] < ph * STANCE["close_conf"]:
            continue
        vr = float(v[t] / v[b1])
        if vr > STANCE["shrink"]:
            continue

        # ---- 命中：组装与 stance_hint 对齐的返回字段 ----
        plat_win = f"{d[ps]}~{d[pe - 1]}"
        sw_range = round(ph / max(pl, 1e-9), 3)          # 平台上沿/下沿（高低价比）
        sw_std = round(float(c[ps:pe].std() / max(c[ps:pe].mean(), 1e-9)), 4)
        vol_x = round(float(v[t] / max(pv, 1e-9)), 2)    # 提示日量/平台均量
        chg = round(float((c[t] / c[t - 1] - 1) * 100), 2) if t > 0 else 0.0
        # 排序分（仅用于并列时排序，方向依据回测：强度越高、越缩量越优）
        score = round(0.55 * min((strength - 1.0) / 0.30, 1.0)
                      + 0.45 * (1.0 - min(vr / STANCE["shrink"], 1.0)), 4)
        return dict(
            date=str(d[t]),
            last_close=round(float(c[t]), 2),            # 买入价 = 提示日收盘
            platform_top=round(ph, 2),
            trigger=round(ph * STANCE["break_high_ratio"], 2),   # 展示用：平台上沿
            req_vol=int(v[t]),                            # 提示日实际量（展示用）
            req_vol_wan=round(float(v[t] / 1e6), 2),     # 万手（新浪 volume 单位=股）
            stop=round(float(c[t]) * (1 - STANCE["stop_loss"]), 2),
            sw_win=plat_win,
            sw_range=sw_range,
            sw_std=sw_std,
            vol_x=vol_x,
            chg=chg,
            score=score,
            # —— 缩量模型特有字段（报告/台账展示）——
            b1_date=str(d[b1]),
            break_high=round(float(h[b1]), 2),
            strength=round(strength, 3),
            vol_ratio=round(vr, 3),
            gap=t - b1,
            plat_low=round(pl, 2),
        )
    return None


def scan_one(code, df, as_of=None):
    """与 stance_hint.scan_one 同签名：站在 as_of 收盘判断当日是否提示。"""
    try:
        r = _signal_at_last_bar(df, as_of=as_of)
        if r:
            r["code"] = code
        return r
    except Exception:
        return None


def main():
    """独立调试：全市场扫描当日缩量二次突破提示（复用 lib 的数据层）。"""
    import sys
    as_of = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y-%m-%d")
    top = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    f10 = lib.load_f10()
    names = {}
    nf = os.path.join(lib.DATA, "names.json")
    if os.path.exists(nf):
        try:
            names = json.load(open(nf, encoding="utf-8"))
        except Exception:
            pass
    hits = []
    for f in sorted(glob.glob(os.path.join(lib.KLINE_DIR, "*.csv"))):
        code = os.path.basename(f)[:6]
        row = f10.get(code, {})
        name = names.get(code) or row.get("name") or code
        if lib.blocked_by_rule(name, code, row):
            continue
        try:
            df = pd.read_csv(f)
            r = scan_one(code, df, as_of=as_of)
            if r:
                r["name"] = name
                r["industry"] = row.get("industry", "")
                r["concepts"] = row.get("concepts", "")
                hits.append(r)
        except Exception:
            continue
    hits.sort(key=lambda x: -x["score"])
    L = [f"# 缩量二次突破提示 · {as_of}", "",
         "> 形态：横盘 → 放量假突破(强度≥1.15) → 洗盘 → 缩量二次上攻；提示即买点。", "",
         f"全市场命中 **{len(hits)}** 只，按评分取前 {min(top, len(hits))} 只：", "",
         "| 名称 | 代码 | 首突日 | 强度 | 量比 | 间隔 | 平台上沿 | 现价 | 止损 | 评分 |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for r in hits[:top]:
        L.append(f"| {r['name']} | {r['code']} | {r['b1_date']} | {r['strength']} | "
                 f"{r['vol_ratio']} | {r['gap']}日 | {r['platform_top']} | "
                 f"{r['last_close']} | {r['stop']} | {r['score']} |")
    rp = os.path.join(lib.REPORT_DIR, f"shrink_{as_of}.md")
    open(rp, "w", encoding="utf-8").write("\n".join(L))
    print(f"命中 {len(hits)} 只 → 报告 {rp}")
    for r in hits[:top]:
        print(f"  {r['name']}({r['code']}) 强度{r['strength']} 量比{r['vol_ratio']} "
              f"间隔{r['gap']}日 评分{r['score']}")
    return hits


if __name__ == "__main__":
    main()
