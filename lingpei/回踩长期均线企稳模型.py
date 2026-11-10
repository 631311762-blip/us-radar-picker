# -*- coding: utf-8 -*-
"""回踩长期均线企稳模型 B_V2 核心检测 detect_coil_v2。
要素：回踩 MA60 企稳（连续小阳）+ 试盘量能间距 18-30 天 + 距前高回撤。
阈值校准基准：领湃科技(300306) 2026-08-21 原始样本——
  试盘日 07-23（阳线实体+1.93%、量比1.68、距信号日21天），
  回踩日 08-19（阴线触 MA60，-0.48%），企稳确认 08-20/08-21 连续两阳，
  信号日收盘高出 MA60 +4.05%，近60日峰回撤 9.0%，
  近10日轨迹：曾在均线上方 +3.2% 运行、最低 -1.97% 未深破。
"""
from 埋伏点_回踩均线模型 import add_indicators

# ---- 阈值（以领湃科技 2026-08-21 样本校准）----
NEAR_MA_DEV = 0.05        # 收盘价偏离 MA60 <= 5% 视为贴线（样本信号日 +4.05%）
YANG_MAX = 0.04           # 企稳小阳线：单日实体上限 4%（样本 08-20 实体 +2.33%）
YANG_COUNT = 2            # 企稳确认：最近 2 根均为阳线（样本 08-20/08-21，08-19 为阴线回踩）
TRIAL_VOL_RATIO = 1.6     # 试盘日量能 >= 近 40 日均量 1.6 倍（样本 1.68）
TRIAL_UP = 0.015          # 试盘日涨幅 >= 1.5%（样本 +1.93%）
TRIAL_BODY = 0.01         # 试盘日实体 >= 1%（样本 +1.93%）
GAP_MIN, GAP_MAX = 18, 30  # 试盘日到当前交易日数范围（样本 21 天）
DROP_MIN, DROP_MAX = 0.08, 0.35  # 距前高（近60日峰值）回撤幅度范围（样本 9.0%）
PEAK_WINDOW = 60          # 前高取近 60 日最高价
# ---- 方向约束（2026-11-09/10 修订：只要「上方运行→回落贴线」，不要「均线下方爬上来」）----
MA60_MAX_BREAK = 0.015     # 现价最多低于 MA60 1.5%（回踩允许轻微毛刺）
DIP_WINDOW = 10            # 轨迹检查窗口（交易日）
TREND_ABOVE_MIN = 0.015    # 窗口内须有收盘高出当时 MA60 至少 1.5%（样本 08-17 +3.16%）
DIP_MAX_BELOW = 0.03       # 窗口内收盘不得低于当时 MA60 达 3%（样本最低 -1.97%）


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

    # 1) 回踩长期均线：只看 MA60，收盘价偏离 <= NEAR_MA_DEV（校准样本 +4.05%）
    dev60 = abs(cur["c"] - ma60) / ma60
    if dev60 > NEAR_MA_DEV:
        return None

    # 2) 企稳小阳线：最近 YANG_COUNT 根均为阳线且实体 <= YANG_MAX（样本 08-20/08-21）
    last_y = bars[-YANG_COUNT:]
    if not all((b["c"] >= b["o"]) and (b["c"] / b["o"] - 1) <= YANG_MAX for b in last_y):
        return None

    # 3) 试盘日：往前找一根放量中阳线（量能、涨幅、实体达标），
    #    取间距落在 [GAP_MIN, GAP_MAX] 内的最近一根（有多根时取最近的）
    near40 = [b["v"] for b in bars[-41:-1]]
    avg_v = sum(near40) / len(near40) if near40 else 0
    trial_idx = None
    for i in range(n - 2, n - 41, -1):
        b = bars[i]
        gap = (n - 1) - i
        if gap < GAP_MIN:
            continue                    # 太近，继续往前找
        if gap > GAP_MAX:
            break                       # 超出间距上限，停止
        body = (b["c"] - b["o"]) / b["o"]
        if b["v"] >= TRIAL_VOL_RATIO * avg_v and (b["c"] / b["o"] - 1) >= TRIAL_UP and body >= TRIAL_BODY:
            trial_idx = i
            break
    if trial_idx is None:
        return None

    # 4) 试盘量能间距已在搜索时保证 18-30 天
    gap = (n - 1) - trial_idx

    # 5) 距前高跌幅：近 PEAK_WINDOW 日峰值回撤到当前（样本近60日峰回撤 9.0%）
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
