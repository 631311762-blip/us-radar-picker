# -*- coding: utf-8 -*-
"""
缩量二次突破模型 (Shrink-Volume Double Breakout, SVDB) · 黄金样本严格拟合版 v3

来源：用户以凯众股份(603037)、仁信新材(301395)为黄金样本，逐 K 线拟合；
      2026-11-18 用户二次校正："横盘1-3个月最好，试盘日应是横盘期间成交量最大的地方"。

形态链条（★为 v3 相对 v2 的关键收紧）：
  1) ★横盘平台：连续 40~60 交易日（约1-3个月）窄幅横盘，
     ★硬约束：收盘价落在平台均值 ±5% 内的天数占比 ≥ 85%（真横盘，排除单边趋势/V反转）
     压力线 ph = 横盘期「收盘价」p95（滤孤立冲高）；支撑线 sup = 收盘价 p5（滤孤立低点）
     黄金样本：凯众 压力10.54/支撑9.69、仁信 压力12.63≈12.7/支撑11.45≈11.46（与用户口径一致）
  2) ★试盘日（首突 b1）= 横盘期内成交量最大的一天（硬条件），
     最高价 ≥ 压力线×1.02（略过即可），收盘 ≥ 压力线；量 ≥ 平台均量×3.0
  3) ★突破失败并破位（2026-11-18 改）：首突后出现过 收盘 < 支撑线 sup（跌破箱体下沿）
     比旧的"压力线×0.91"更贴合黄金样本：凯众破位日10-15收9.52<9.69、仁信11-03收11.12<11.46
  4) 快速收复 + 洗盘 10~30 日（2026-11-18 定；容纳凯众12日/仁信22日，两黄金样本均命中）
  5) 缩量二次上攻（提示日 T）：最高价 ≥ 首突日高×0.95（达到前高即可关注，2026-11-18 由0.97放宽），
     量 ≤ 首突量×0.55（无需多少量就能涨），收盘 ≥ 压力线×0.98（已站上）
  6) 方向性硬约束：提示日收涨 且 收盘 ≥ 当日最高价×0.90
  提示日 T 即买点，收盘价买入，止损 -5%。

v3 排除效果（用户指出的问题票）：
  · 丽珠集团 000513：9-28 试盘前的"平台"收盘集中度仅 20%，是 V 型反转非横盘 → 排除
  · 日出东方 603366：集中度 50%、趋势 +20% → 排除
  · 出版传媒 601999：集中度 70%、趋势 +24% → 排除
  · 锋龙股份 002931：集中度 40% → 排除

版本历史：
  v1 非黄金样本拟合（日内高压力线 + 1.15 强度），备份 shrink_breakout_v1.py
  v2 收盘压力线 + 放量3.5倍，但横盘仅25日、未要求试盘日最大量，备份 shrink_breakout_v2.py
  v3 ★当前：横盘1-3月 + 集中度≥85% + 试盘日横盘最大量

接口与 stance_hint / v1 / v2 完全对齐（STANCE + scan_one），供 daily_v4.py 无缝调用。
"""
import os
import json
import glob
from datetime import datetime

import numpy as np
import pandas as pd

import lib

# -------- 参数（黄金样本严格拟合口径，勿随意修改）--------
STANCE = {
    # ★横盘平台（1-3个月）
    "plat_days_min": 40,         # 横盘最短交易日（约2个月）
    "plat_days_max": 60,         # 横盘最长交易日（约3个月）
    "plat_concentration": 0.85,  # ★收盘价落在均值±plat_band内的天数占比下限
    "plat_band": 0.05,           # 横盘集中度带宽（±5%）
    "plat_amp_max": 0.25,        # 横盘高低振幅上限（辅助）
    # ★首次放量假突破（试盘日）
    "break_vol_is_max": True,    # ★试盘日必须是横盘期内成交量最大的一天
    "break_high_ratio": 1.02,    # 首突最高价 ≥ 压力线×该值
    "break_close_min": 1.00,     # 首突收盘 ≥ 压力线×该值
    "break_vol_ratio": 3.0,      # 首突量 ≥ 平台均量×该值
    # 破位与收复（2026-11-18 改：破位改用支撑线，不再用压力线×0.91）
    "support_pct": 5,            # ★支撑线 = 横盘期收盘价 p5（滤孤立低点）
    "recover_min": 0.98,         # 提示日收盘 ≥ 压力线×该值
    # 洗盘间隔（2026-11-18 定：10~30 日，容纳凯众12日/仁信22日）
    "min_gap": 10,
    "max_gap": 30,
    # 缩量二次上攻（2026-11-18 放宽达前高 0.97→0.95）
    "prev_high_min": 0.95,       # 提示日最高价 ≥ 首突日最高价×该值
    "shrink": 0.55,              # 提示日量 ≤ 首突量×该值
    # 方向性硬约束
    "require_up_day": True,
    "close_high_min": 0.90,
    # 风控
    "stop_loss": 0.05,
}


def _find_plateau(c, v, b1):
    """在 b1 之前找 40~60 日的真横盘平台。

    返回 (ps, pe, ph, pl, pv, concentration, plat_len, sup) 或 None。
    sup = 支撑线 = 横盘期收盘价 p5（滤孤立低点）。

    ★2026-11-18 改：在所有满足集中度约束的 40~60 日窗口中，取「集中度最高、
    窗口最短」的紧致核心平台，而不是最长的窗口。原因：凯众最长合格窗口 L=51
    (集中度86%刚过线)含7月低点8.72，支撑线被拉到9.04；而 L=41~45 的集中度
    98~100%、支撑线9.69~9.79，才是用户说的箱体(支撑9.69)。取紧致核心区更贴合。
    """
    best = None
    for L in range(STANCE["plat_days_max"], STANCE["plat_days_min"] - 1, -1):
        ps, pe = b1 - L, b1
        if ps < 0:
            continue
        seg_c = c[ps:pe]
        seg_v = v[ps:pe]
        if len(seg_c) < STANCE["plat_days_min"]:
            continue
        mean_c = float(seg_c.mean())
        if mean_c <= 0:
            continue
        # ★横盘集中度：收盘价落在均值±5%内的占比
        concentration = float(np.mean(np.abs(seg_c / mean_c - 1) <= STANCE["plat_band"]))
        if concentration < STANCE["plat_concentration"]:
            continue
        # ★压力线用收盘价 95 分位（滤掉孤立冲高，如仁信8-18的13.42）；
        #   黄金样本实测：凯众 p95=10.48、仁信 p95=12.63（≈用户说的12.7）
        ph = float(np.percentile(seg_c, 95))
        pl = float(seg_c.min())
        sup = float(np.percentile(seg_c, STANCE["support_pct"]))  # ★支撑线=收盘p5
        if (ph - pl) / max(ph, 1e-9) > STANCE["plat_amp_max"]:
            continue
        pv = float(seg_v.mean())
        # ★试盘日必须是横盘期最大量
        if STANCE["break_vol_is_max"] and v[b1] <= seg_v.max():
            continue
        # 取集中度最高者；并列时取窗口更短（更紧致）者
        cand = (concentration, -L, ps, pe, ph, pl, pv, concentration, L, sup)
        if best is None or cand[:2] > best[:2]:
            best = cand
    if best is None:
        return None
    _, _, ps, pe, ph, pl, pv, concentration, L, sup = best
    return ps, pe, ph, pl, pv, concentration, L, sup


def _signal_at_last_bar(df, as_of=None):
    """站在最后一根K线，判断当日是否为缩量二次突破提示日。返回特征 dict 或 None。"""
    df = df.reset_index(drop=True)
    df["date"] = df["date"].astype(str)
    if as_of:
        df = df[df["date"] <= str(as_of)]
    # 横盘最长60日 + 洗盘最长45日 = 至少105日
    if len(df) < 110:
        return None
    df = df.tail(200).reset_index(drop=True)

    c = df["close"].values.astype(float)
    v = df["volume"].values.astype(float)
    h = df["high"].values.astype(float)
    lo = df["low"].values.astype(float)
    d = df["date"].values
    n = len(df)
    t = n - 1

    if c[t] < lib.MIN_PRICE:
        return None
    if float((c[-20:] * v[-20:]).mean()) < lib.MIN_AMOUNT:
        return None

    # b1 ∈ [max(plat_max+5, t-max_gap), t-min_gap]，从近到远
    start = t - STANCE["min_gap"]
    end = max(STANCE["plat_days_max"] + STANCE["min_gap"], t - STANCE["max_gap"])
    for b1 in range(start, end - 1, -1):
        plat = _find_plateau(c, v, b1)
        if plat is None:
            continue
        ps, pe, ph, pl, pv, concentration, plat_len, sup = plat
        # 首次放量假突破
        if not (h[b1] >= ph * STANCE["break_high_ratio"]
                and c[b1] >= ph * STANCE["break_close_min"]
                and v[b1] >= pv * STANCE["break_vol_ratio"]):
            continue
        strength = float(h[b1] / ph)
        # 破位环节（2026-11-18 改：收盘跌破支撑线=箱体下沿，而非压力线×0.91）
        if float(c[b1:t].min()) >= sup:
            continue
        # 提示日：达前高 + 缩量 + 站上压力线
        reach = float(h[t] / h[b1])
        if reach < STANCE["prev_high_min"]:
            continue
        vr = float(v[t] / v[b1])
        if vr > STANCE["shrink"]:
            continue
        if c[t] < ph * STANCE["recover_min"]:
            continue
        # 方向性硬约束
        if STANCE["require_up_day"] and c[t] <= c[t - 1]:
            continue
        if c[t] < h[t] * STANCE["close_high_min"]:
            continue

        # ---- 命中 ----
        plat_win = f"{d[ps]}~{d[pe - 1]}"
        sw_range = round(ph / max(pl, 1e-9), 3)
        sw_std = round(float(c[ps:pe].std() / max(c[ps:pe].mean(), 1e-9)), 4)
        vol_x = round(float(v[t] / max(pv, 1e-9)), 2)
        chg = round(float((c[t] / c[t - 1] - 1) * 100), 2) if t > 0 else 0.0
        break_vol_x = float(v[b1] / max(pv, 1e-9))
        # 试盘日量排名（横盘期内，1=最大）
        rank = int((v[ps:pe] > v[b1]).sum()) + 1
        score = round(0.40 * min((reach - STANCE["prev_high_min"])
                                 / (1 - STANCE["prev_high_min"]), 1.0)
                      + 0.40 * (1.0 - min(vr / STANCE["shrink"], 1.0))
                      + 0.20 * concentration, 4)
        return dict(
            date=str(d[t]),
            last_close=round(float(c[t]), 2),
            platform_top=round(ph, 2),
            support=round(sup, 2),
            trigger=round(ph, 2),
            req_vol=int(v[t]),
            req_vol_wan=round(float(v[t] / 1e6), 2),
            stop=round(float(c[t]) * (1 - STANCE["stop_loss"]), 2),
            sw_win=plat_win,
            sw_range=sw_range,
            sw_std=sw_std,
            vol_x=vol_x,
            chg=chg,
            score=score,
            b1_date=str(d[b1]),
            break_high=round(float(h[b1]), 2),
            strength=round(strength, 3),
            vol_ratio=round(vr, 3),
            gap=t - b1,
            plat_low=round(pl, 2),
            break_vol_x=round(break_vol_x, 2),
            break_depth=round(float(c[b1:t].min() / sup - 1) * 100, 2),  # 相对支撑线的破位幅度
            reach=round(reach, 3),
            plat_len=plat_len,
            plat_conc=round(concentration, 3),
            b1_vol_rank=rank,
            ph_basis="close",
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
    """独立调试：全市场扫描当日缩量二次突破提示。"""
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
         "> 形态：横盘(1-3月,集中度≥85%) → 试盘(横盘最大量) → 深跌破位后收复 → 缩量达前高；提示即买点。", "",
         f"全市场命中 **{len(hits)}** 只，按评分取前 {min(top, len(hits))} 只：", "",
         "| 名称 | 代码 | 平台 | 试盘日 | 集中度 | 放量 | 破位 | 达前高 | 量比 | 现价 | 评分 |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in hits[:top]:
        L.append(f"| {r['name']} | {r['code']} | {r['sw_win']}({r['plat_len']}日) | {r['b1_date']} | "
                 f"{r['plat_conc']} | {r['break_vol_x']}倍 | {r['break_depth']}% | {r['reach']} | "
                 f"{r['vol_ratio']} | {r['last_close']} | {r['score']} |")
    rp = os.path.join(lib.REPORT_DIR, f"shrink_{as_of}.md")
    open(rp, "w", encoding="utf-8").write("\n".join(L))
    print(f"命中 {len(hits)} 只 → 报告 {rp}")
    for r in hits[:top]:
        print(f"  {r['name']}({r['code']}) 平台{r['plat_len']}日 集中度{r['plat_conc']} "
              f"放量{r['break_vol_x']}倍 达前高{r['reach']} 评分{r['score']}")
    return hits


if __name__ == "__main__":
    main()
