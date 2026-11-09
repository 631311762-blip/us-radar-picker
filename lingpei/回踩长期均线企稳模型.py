# -*- coding: utf-8 -*-
"""回踩长期均线企稳模型 B_V2 核心检测 detect_coil_v2。
要素（来自原 prompt）：回踩 MA60/MA30 + 小阳线 + 试盘量能间距 18-30 天 + 距前高跌幅。
重建版：阈值按合理推断实现，需在真实标的上核对后再用于实盘。
"""
from 埋伏点_回踩均线模型 import add_indicators

# ---- 阈值（重建版推断，可调）----
NEAR_MA_DEV = 0.03        # 收盘价偏离长期均线 <= 3% 视为贴线
YANG_MAX = 0.03           # 企稳小阳线：单日涨幅上限 3%
TRIAL_VOL_RATIO = 1.6     # 试盘日量能 >= 近 40 日均量 1.6 倍
TRIAL_UP = 0.03           # 试盘日涨幅 >= 3%
TRIAL_BODY = 0.01         # 试盘日实体 >= 1%
GAP_MIN, GAP_MAX = 18, 30  # 试盘日到当前交易日数范围
DROP_MIN, DROP_MAX = 0.08, 0.35  # 距前高（试盘段峰值）回撤幅度范围


def detect_coil_v2(bars):
    """返回信号 dict 或 None。信号 dict 含 score / gap_days / drop_from_peak / near_ma / trial_date。"""
    n = len(bars)
    if n < 65:                      # 不足 60 根无法算 MA60
        return None
    bars = add_indicators(bars)
    cur = bars[-1]

    ma60 = cur.get("ma60")
    ma30 = cur.get("ma30")
    if ma60 is None or ma30 is None:
        return None

    # 1) 回踩长期均线：收盘价贴近 MA60 或 MA30（偏离 <= NEAR_MA_DEV）
    dev60 = abs(cur["c"] - ma60) / ma60
    dev30 = abs(cur["c"] - ma30) / ma30
    if min(dev60, dev30) > NEAR_MA_DEV:
        return None

    # 2) 企稳小阳线：最近 3 根均为小阳线（收>=开，涨幅 <= YANG_MAX），且不破均线
    last3 = bars[-3:]
    if not all((b["c"] >= b["o"]) and (b["c"] / b["o"] - 1) <= YANG_MAX for b in last3):
        return None

    # 3) 试盘日：往前找一根放量中阳线（量能、涨幅、实体达标）
    near40 = [b["v"] for b in bars[-41:-1]]
    avg_v = sum(near40) / len(near40) if near40 else 0
    trial_idx = None
    for i in range(n - 2, n - 41, -1):
        b = bars[i]
        body = (b["c"] - b["o"]) / b["o"]
        if b["v"] >= TRIAL_VOL_RATIO * avg_v and (b["c"] / b["o"] - 1) >= TRIAL_UP and body >= TRIAL_BODY:
            trial_idx = i
            break
    if trial_idx is None:
        return None

    # 4) 试盘量能间距 18-30 天
    gap = (n - 1) - trial_idx
    if not (GAP_MIN <= gap <= GAP_MAX):
        return None

    # 5) 距前高跌幅：试盘段（trial 起至当前）峰值回撤到当前
    peak = max(b["h"] for b in bars[trial_idx:])
    drop = (peak - cur["c"]) / peak
    if not (DROP_MIN <= drop <= DROP_MAX):
        return None

    # 评分：贴线越紧、间距越居中、回撤越合理，分越高
    score = 100 - min(dev60, dev30) * 1000 - abs(gap - 24) * 1.5 - abs(drop - 0.20) * 120
    return {
        "signal": True,
        "score": max(round(score, 2), 0),
        "gap_days": gap,
        "drop_from_peak": round(drop, 4),
        "dev60": round(dev60, 4),
        "dev30": round(dev30, 4),
        "near_ma": "MA60" if dev60 <= dev30 else "MA30",
        "trial_date": bars[trial_idx]["d"],
    }
