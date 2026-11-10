# -*- coding: utf-8 -*-
"""回踩长期均线企稳模型 B_V2 核心检测 detect_coil_v2。
要素：回踩 MA60 企稳（小阳线）+ 试盘放量大阳间距 18-30 天 + 距前高回撤。
阈值校准基准：领湃科技(300530) 2026-08-21 原始样本——
  试盘日 07-27（放量大阳：涨幅 +15.80%、量比 4.06，距信号日 19 个交易日），
  07-27~07-31 为高位放量震荡区（+15.8/-1.9/+20.0/-5.8/+8.75），
  其后冲高（08-10 +9.1%、08-12 +3.4%）后回落，08-19 大跌 -5.46% 触及 MA60，
  信号日 08-21：阴跌止住收一根小阳（实体 +0.61%），
  收盘 18.09 贴 MA60=18.16（偏离 -0.39%）、贴 MA30=18.06（+0.16%），
  近 60 日峰 22.60@05-29，回撤 20.0%，
  近 10 日轨迹：曾在均线上方 +8.9% 运行、最低 -0.61% 未深破。
"""
from 埋伏点_回踩均线模型 import add_indicators

# ---- 阈值（以领湃科技 300530 2026-08-21 样本校准）----
NEAR_MA_DEV = 0.05        # 收盘价偏离 MA60 <= 5% 视为贴线（样本信号日 -0.39%，只看 MA60）
YANG_MAX = 0.04           # 企稳小阳线：信号日实体上限 4%（样本 08-21 实体 +0.61%）
TRIAL_VOL_RATIO = 2.0     # 试盘日量能 >= 近 40 日均量 2 倍（样本 4.06）
TRIAL_UP = 0.05           # 试盘日涨幅 >= 5%（样本 +15.80%，20cm 板）
GAP_MIN, GAP_MAX = 18, 30  # 试盘日到当前交易日数范围（样本 7-27 -> 8-21 = 19 天）
DROP_MIN, DROP_MAX = 0.08, 0.35  # 距前高（近60日峰值）回撤幅度范围（样本 20.0%）
PEAK_WINDOW = 60          # 前高取近 60 日最高价
# ---- 方向约束（2026-11-09/10 修订：只要「上方运行→回落贴线」，不要「均线下方爬上来」）----
MA60_MAX_BREAK = 0.015     # 现价最多低于 MA60 1.5%（回踩允许轻微毛刺，样本 -0.39%）
DIP_WINDOW = 10            # 轨迹检查窗口（交易日）
TREND_ABOVE_MIN = 0.015    # 窗口内须有收盘高出当时 MA60 至少 1.5%（样本 08-13 +8.9%）
DIP_MAX_BELOW = 0.03       # 窗口内收盘不得低于当时 MA60 达 3%（样本最低 -0.61%）


def detect_coil_v2(bars):
    """返回信号 dict 或 None。信号 dict 含 score / gap_days / drop_from_peak / near_ma / trial_date。"""
    n = len(bars)
    if n < 65:                      # 不足 60 根无法算 MA60
        return None
    bars = add_indicators(bars)
    cur = bars[-1]

    ma60 = cur.get("ma60")
    if ma60 is None:
        return None

    # 1) 回踩长期均线：只看 MA60，收盘价偏离 <= NEAR_MA_DEV（校准样本 -0.39%）
    dev60 = abs(cur["c"] - ma60) / ma60
    if dev60 > NEAR_MA_DEV:
        return None

    # 2) 企稳小阳线：信号日当天为阳线（收>=开）且实体 <= YANG_MAX
    #    （样本 08-21：前两日为阴线回踩，当日止跌收小阳 +0.61%）
    if not ((cur["c"] >= cur["o"]) and (cur["c"] / cur["o"] - 1) <= YANG_MAX):
        return None

    # 3) 试盘日：在间距 [GAP_MIN, GAP_MAX] 内找放量大阳日
    #    （量能 >= 近40日均量 TRIAL_VOL_RATIO 倍、涨幅 >= TRIAL_UP 的阳线）；
    #    取间距最近的（样本 07-27：+15.80%、量比 4.06、间距 19 天）
    trial_idx = None
    for gap in range(GAP_MIN, GAP_MAX + 1):
        i = n - 1 - gap
        if i < 40:
            break
        b = bars[i]
        avg_v = sum(x["v"] for x in bars[i - 40:i]) / 40
        if (b["c"] > b["o"]
                and (b["c"] / b["o"] - 1) >= TRIAL_UP
                and avg_v > 0 and b["v"] >= TRIAL_VOL_RATIO * avg_v):
            trial_idx = i
            break
    if trial_idx is None:
        return None

    # 4) 试盘间距已在搜索时保证 18-30 天
    gap = (n - 1) - trial_idx

    # 5) 距前高跌幅：近 PEAK_WINDOW 日峰值回撤到当前（样本近60日峰回撤 20.0%）
    peak = max(b["h"] for b in bars[-PEAK_WINDOW:])
    drop = (peak - cur["c"]) / peak
    if not (DROP_MIN <= drop <= DROP_MAX):
        return None

    # 5d) 方向约束：现价不得有效跌破 MA60（排除均线之下反弹上来的形态）
    if cur["c"] < ma60 * (1 - MA60_MAX_BREAK):
        return None

    # 5e) 方向约束：近 DIP_WINDOW 日轨迹须"上方运行→回落贴线"，
    #     排除在均线下方徘徊/深V反弹后爬上来的形态
    if n >= DIP_WINDOW + 60:
        devs = []
        for i in range(n - DIP_WINDOW, n):
            ma_i = sum(b["c"] for b in bars[i - 59:i + 1]) / 60
            if ma_i:
                devs.append(bars[i]["c"] / ma_i - 1)
        if devs:
            if max(devs) < TREND_ABOVE_MIN:   # 近期须在均线上方运行过
                return None
            if min(devs) < -DIP_MAX_BELOW:    # 期间未有效跌破均线
                return None

    # 评分：贴线越紧、间距越居中、回撤越合理，分越高
    score = 100 - dev60 * 1000 - abs(gap - 24) * 1.5 - abs(drop - 0.20) * 120
    return {
        "signal": True,
        "score": max(round(score, 2), 0),
        "gap_days": gap,
        "drop_from_peak": round(drop, 4),
        "dev60": round(dev60, 4),
        "near_ma": "MA60",
        "trial_date": bars[trial_idx]["d"],
    }
