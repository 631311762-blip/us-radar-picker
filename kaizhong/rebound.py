# -*- coding: utf-8 -*-
"""
反包阳线·前期入选模型 (Rebound-Yang, RY) · 内核
================================================================
2026-11-21 按用户口径上线：把「提示日」从 V3 缩量二次上攻日，
提前到「破位后的反包阳线当日」，作为微信「前期入选样本」跟踪。

形态链条（前半段与 shrink_breakout V3 完全一致，仅后半段改变）：
  1) 横盘平台 40~60 日，收盘集中度≥85%，压力线=收盘 p95，支撑线=收盘 p5
  2) 试盘日 = 横盘期成交量最大日，最高≥压力×1.02，收盘≥压力，量≥平台均量×3
  3) 破位：首突后收盘跌破支撑线，破位日 = 破位区间收盘最低那天
  4) ★反包阳线（新提示日 T）：破位后 1~5 日内第一根「阳线 + 收盘站回支撑线
     + 收盘吞没破位日实体(≥破位日开盘)」——作为前期入选样本

黄金分割线（基于提示日 T 当日 K 线最高/最低）：
  0.5 中位线 = H - (H-L)×0.5   ← 主出局线（收盘跌破即结束跟踪）
  另给 0.382 / 0.618 供研究参考。

升级信号（重点标识，不在本内核判定，由 daily_v4 跟踪层完成）：
  跟踪期内若出现 shrink_breakout.scan_one 命中（V3 缩量二次上攻提示日），
  重点标识「V3 确认」——反包入选 → V3 是买点升级链条。

接口：STANCE + scan_one(code, df, as_of) 与 shrink_breakout 对齐。
"""
import os
import json
import glob
from datetime import datetime

import numpy as np
import pandas as pd

import lib

# 前半段完全沿用 V3 参数（从 shrink_breakout.STANCE 读取，避免两处漂移）
import shrink_breakout as sh

STANCE = dict(sh.STANCE)
STANCE.update({
    "rebound_max_gap": 5,     # 破位后几日内出现反包阳线算入选
    "gold_main": 0.5,         # ★主出局黄金分割位（提示日单根K线）
})
# 出局线档位（供报告/跟踪展示）
GOLD_LEVELS = (0.382, 0.5, 0.618)


def _find_plateau(c, v, b1):
    """与 shrink_breakout._find_plateau 完全一致（复用保证口径不漂移）。"""
    return sh._find_plateau(c, v, b1)


def _gold_lines(h, lo):
    """基于单根 K 线高/低算黄金分割线（回撤位：H - (H-L)*level）。"""
    H, L = float(h), float(lo)
    return {f"gold_{int(lv*1000)}": round(H - (H - L) * lv, 2) for lv in GOLD_LEVELS}


def _signal_at_last_bar(df, as_of=None):
    """站在最后一根 K 线，判断当日是否为反包阳线前期入选日。"""
    df = df.reset_index(drop=True)
    df["date"] = df["date"].astype(str)
    if as_of:
        df = df[df["date"] <= str(as_of)]
    if len(df) < 110:
        return None
    df = df.tail(200).reset_index(drop=True)

    o = df["open"].values.astype(float)
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

    # 试盘日 b1 ∈ [t - 45, t - 1]（平台在前，反包在后；留足破位+反包窗口）
    start = t - 1
    end = max(sh.STANCE["plat_days_max"] + 2, t - 45)
    for b1 in range(start, end - 1, -1):
        if b1 < sh.STANCE["plat_days_max"]:
            continue
        plat = _find_plateau(c, v, b1)
        if plat is None:
            continue
        ps, pe, ph, pl, pv, concentration, plat_len, sup = plat
        # 试盘日条件（与 V3 一致）
        if not (h[b1] >= ph * sh.STANCE["break_high_ratio"]
                and c[b1] >= ph * sh.STANCE["break_close_min"]
                and v[b1] >= pv * sh.STANCE["break_vol_ratio"]):
            continue
        # 破位日 bd = 首突后收盘首次跌破支撑线起，破位区间收盘最低那天
        j0 = None
        for j in range(b1 + 1, n):
            if c[j] < sup:
                j0 = j
                break
        if j0 is None:
            continue
        jend = j0
        while jend < n and c[jend] < sup:
            jend += 1
        bd = j0 + int(np.argmin(c[j0:jend]))
        # ★反包阳线提示日：必须是当日 t，且在破位后 rebound_max_gap 日内
        gap_bd = t - bd
        if not (1 <= gap_bd <= STANCE["rebound_max_gap"]):
            continue
        # 当日必须阳线 + 收盘站回支撑 + 吞没破位日实体
        if not (c[t] > o[t] and c[t] >= sup and c[t] >= o[bd]):
            continue
        # （破位后到 t 之前不能已有反包入选，避免同一波重复；由 update_ledger 去重兜底）

        # ---- 命中 ----
        gl = _gold_lines(h[t], lo[t])
        plat_win = f"{d[ps]}~{d[pe - 1]}"
        sw_range = round(ph / max(pl, 1e-9), 3)
        sw_std = round(float(c[ps:pe].std() / max(c[ps:pe].mean(), 1e-9)), 4)
        chg = round(float((c[t] / c[t - 1] - 1) * 100), 2) if t > 0 else 0.0
        body = round(float((c[t] - o[t]) / max(o[t], 1e-9) * 100), 2)  # 阳线实体%
        back_sup = round(float(c[t] / sup - 1) * 100, 2)                # 站回支撑幅度%
        # 评分：阳线实体 + 站回支撑 + 平台集中度（纯排序参考）
        score = round(0.45 * min(body / 8.0, 1.0)
                      + 0.35 * min(back_sup / 10.0, 1.0)
                      + 0.20 * concentration, 4)
        return dict(
            date=str(d[t]),
            last_close=round(float(c[t]), 2),
            open=round(float(o[t]), 2),
            high=round(float(h[t]), 2),
            low=round(float(lo[t]), 2),
            chg=chg,
            body=body,
            platform_top=round(ph, 2),
            support=round(sup, 2),
            bd_date=str(d[bd]),
            bd_close=round(float(c[bd]), 2),
            bd_open=round(float(o[bd]), 2),
            gap_bd=gap_bd,
            **gl,                       # gold_382 / gold_500 / gold_618
            trigger=round(ph, 2),
            req_vol=int(v[t]),
            req_vol_wan=round(float(v[t] / 1e6), 2),
            stop=round(float(c[t]) * (1 - sh.STANCE["stop_loss"]), 2),
            sw_win=plat_win,
            sw_range=sw_range,
            sw_std=sw_std,
            vol_x=round(float(v[t] / max(pv, 1e-9)), 2),
            b1_date=str(d[b1]),
            break_high=round(float(h[b1]), 2),
            strength=round(float(h[b1] / ph), 3),
            vol_ratio=round(float(v[t] / max(v[b1], 1e-9)), 3),
            gap=t - b1,
            plat_len=plat_len,
            plat_conc=round(concentration, 3),
            break_vol_x=round(float(v[b1] / max(pv, 1e-9)), 2),
            break_depth=round(float(c[bd] / sup - 1) * 100, 2),
            score=score,
            ph_basis="close",
        )
    return None


def scan_one(code, df, as_of=None):
    """与 stance_hint / shrink_breakout.scan_one 同签名。"""
    try:
        r = _signal_at_last_bar(df, as_of=as_of)
        if r:
            r["code"] = code
        return r
    except Exception:
        return None


def main():
    import sys
    as_of = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y-%m-%d")
    top = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    f10 = lib.load_f10()
    hits = []
    for f in sorted(glob.glob(os.path.join(lib.KLINE_DIR, "*.csv"))):
        code = os.path.basename(f)[:6]
        row = f10.get(code, {})
        if lib.blocked_by_rule(row.get("name", ""), code, row):
            continue
        try:
            r = scan_one(code, pd.read_csv(f), as_of=as_of)
            if r:
                r["name"] = row.get("name", code)
                hits.append(r)
        except Exception:
            continue
    hits.sort(key=lambda x: -x["score"])
    print(f"反包阳线前期入选 · {as_of} · 命中 {len(hits)} 只")
    for r in hits[:top]:
        print(f"  {r['name']}({r['code']}) 反包{r['date']} 破位{r['bd_date']} "
              f"0.5线{r['gold_500']} 评分{r['score']}")


if __name__ == "__main__":
    main()
