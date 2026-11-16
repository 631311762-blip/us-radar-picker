# -*- coding: utf-8 -*-
"""回踩长期均线企稳模型 B_V3.3 核心检测 detect_coil_v2（函数名保持不变，兼容下游）。

校准基准（三个样本须同时满足）：
  1) 领湃科技 300530 @ 2026-08-21（原始基准）：试盘日 07-27 放量大阳
     （+15.80%、量比 4.06、间距 19 天），08-10 冲高后回落，08-21 收小阳
     （实体 +0.61%），收 18.09 贴 MA60=18.16（-0.39%），近60日峰回撤 20.0%，
     近10日曾在 MA60 上方 +11% 运行、最低仅 -0.61%。
  2) 金杯汽车 600609 @ 2026-11-06：试盘日 09-17（+10.84%、量比3.85、间距30天），
     信号日收 3.16，低于 MA60=3.376 达 -6.39%，实体 +0.32%，回撤 18.97%，
     近10日由 +1.66% 一路回落到 -6.39%。
  3) 天顺风能 002531 @ 2026-10-28：试盘日 09-17（+6.10%、量比4.97、间距23天），
     信号日收 6.42，低于 MA60=6.86 达 -6.42%，实体 -0.16%（小阴止跌），回撤 28.51%，
     近10日由 +9.17% 回落到 -6.71%。

相对 B_V2 的改动（2026-11-15）：
  - 贴线带 由 |偏离|<=5% 改为 [-7%, +5%]：允许回踩时深破 MA60
  - 企稳 由「必须阳线」改为「实体 ∈ [-1%, +4%]」：允许小阴/十字止跌
  - 删除 5d「不许深破1.5%」与 5e「轨迹不得低于-3%」
  - 方向约束改为**轨迹回落判定**：近10日须曾在 MA60 上方 >=1%，
    且「前4日均偏离 - 末3日均偏离 >= 2个百分点」——先高后低才算「从高点往MA60靠近」，
    天然排除「均线下方一路爬上来」的贴线形态（中电鑫龙、众生药业、均胜电子等）。
  - 试盘日（18-30天/涨>=5%/量比>=2）、距前高（8%-35%）、评分公式保持不变。

V3.1 最小组合（2026-11-15，用户确认落地）：为纳入另外三个「贴线/企稳/方向全过、只卡试盘日」的样本，
  仅放宽三处试盘日/回撤阈值：量比 2.0→1.5、涨幅 5%→1.5%、回撤下限 8%→6.5%。
  新增校准样本：
  4) 昆船智能 301311 @ 2026-09-16：试盘 08-06（+1.54%、量比2.37、间距29天），回撤 6.92%
  5) 易点天下 301171 @ 2026-08-26：试盘 07-31（+17.27%、量比1.53、间距18天），回撤 26.75%
  6) 领湃科技 300530 @ 2026-09-28：试盘 09-01（+11.90%、量比1.69、间距18天），回撤 17.07%
  六个样本全部满足；11-09 已知坏形态 10 只（中电鑫龙/众生药业/均胜电子等）仍全部滤除。

历史对照回测（2026-08-03~10-30，4568只，3/5/10日收盘收益，不复权）：
  V2原版 2404信号 3/5/10日胜率 44.6/41.5/38.4%，平均 +0.08/-0.36/-1.17%
  V3     4262信号 3/5/10日胜率 44.3/40.8/36.9%，平均 -0.03/-0.55/-1.42%
  V3.1  10380信号 3/5/10日胜率 43.1/40.6/38.3%，平均 -0.22/-0.65/-1.34%（日均 72→176 只）
  股票级去重 V3 / V3.1：3日 45.5/45.5%，5日 46.3/45.3%，10日 42.4/42.7%。
  V3.1 新增 6118 信号中深破MA60>3%的占 27%，该批 5日胜率 38.1%、平均 -1.11%，仍是主要拖累。
  注意：试盘日取「间距最近」的命中日，放宽后会改变既有样本的试盘日与评分
  （金杯 25.83→34.83、天顺 24.13→16.63），非计算错误。

V3.2 量价企稳过滤（2026-11-16，用户要求加入）：研究六校准样本「提示日前3天→提示日」
的量价变化，并与试盘日量比联动，发现最显著的共性是「放量试盘 → 缩量回踩 → 提示日再缩量」：
  六样本 提示日量/40日均量 0.34~0.76（均值0.50），前3日累计涨跌 -7.69%~+0.62%，
  提示日量/前3日均量 0.58~0.94，试盘日量/提示日量 2.30~5.19，
  试盘日量比 1.53~4.06，试盘量比/提示日量能倍数 2.83~9.23。
  对照组 11-09 十只坏形态（从下往上贴线）提示日全部放量：
  量/40日均量 0.76~1.61（均值1.20）、提示日量/前3日均量 0.74~2.55（均值1.44）、
  前3日累计涨跌均值 +2.88%、提示日全部上涨 +1.34%~+7.41%。
  据此落地三条可配置条件（VOL_FILTER_ON 总开关）：
    C1 SIG_VR40_MAX=0.85 提示日缩量；C2 SHRINK_VS_PREV3_MAX=1.0 相对前3日继续缩量；
    C4 TRIAL_SIG_VR_RATIO_MIN=2.5 试盘日量比 / 提示日量能倍数（把试盘量比大小纳入）。
  回测（2026-08-03~10-30，相对 V3.1 基线 10380 信号）：
    C1+C2+C4 → 5556 信号（-46%），信号级 3/5/10日胜率 43.1/40.6/38.3% → 45.5/44.2/39.7%，
    平均 -0.22/-0.65/-1.34% → -0.00/-0.20/-1.05%；股票级 3/5/10日 45.9/46.4/42.8%。
    六校准样本 6/6 通过，坏形态 0/10 通过；11-13 截面 36 → 12 只。

V3.3 提示日 K 线企稳过滤（2026-11-16，用户要求补充第二个筛选条件）：
  整理六校准样本提示日开高低收，并对全市场 5556 个 V3.2 信号按 K 线特征分箱统计后续胜率。
  六样本共性：长下影 lower 0.35~0.68、小实体 body<=0.27、振幅收敛 2.48%~3.27%、
    盘中低点跌破 MA60 且多数创前日新低、收盘距当日最低 +1.14%~+2.30%——
    即「盘中下探破线 → 尾盘收回、留长下影」的探底回升锤子线。
  对照组 11-09 十只坏形态恰好相反：lower 均值 0.14、upper 0.48、body 0.38、
    amp 4.45%、low_vs_prev +2.9%（未破前低）——放量上攻的长上影大实体。
  分箱胜率（基线 3/5/10日 45.5/44.2/39.7%）显示：
    amp>=4% 是主要亏损源（4~6% 档 5日 34.8%、>=6% 档 20.8%）；
    clv>=0.7 单条最高(5日 50.7%)但六样本无一达标，硬加会杀掉全部校准样本，故不采用；
    破前低(low_vs_prev<=+0.5%) 单条即把坏形态滤到 0/10 且 5日 48.6%。
  落地四条（KLINE_FILTER_ON 总开关，False 可退回 V3.2）：
    K1 AMP_MAX=0.04 振幅上限；K2 LOWER_SHADOW_MIN=0.30 长下影；
    K3 C_FROM_LOW∈[0.01,0.03] 收盘距最低甜区；K5 LOW_VS_PREV_MAX=0.005 须破/贴近前低。
    K4 小实体(body<=0.30) 虽是样本共性，但单条胜率 43.3% 反而低于基线，故不纳入；
    K6 clv>=0.35、K7 low_dev>=-9% 几乎无区分力，不纳入，避免过拟合。
  回测（2026-08-03~10-30，相对 V3.2 基线 5556 信号）：
    K1+K2+K3+K5 → 1264 信号（-77%），3/5/10日胜率 45.5/44.2/39.7% → 54.4/54.0/49.7%，
    平均 -0.00/-0.20/-1.05% → +0.76/+0.97/+0.73%；六样本 6/6、坏形态 0/10；
    各评分区间保留率均匀 22~24%，>=80 分段 5日胜率 57.4%/+2.07%。
"""
from 埋伏点_回踩均线模型 import add_indicators

# ---- 共用阈值（沿用领湃 300530 校准口径）----
YANG_MAX = 0.04           # 信号日实体上限 4%
BODY_MIN = -0.01          # 信号日实体下限 -1%（允许小阴/十字）
TRIAL_VOL_RATIO = 1.5     # 试盘日量能 >= 近40日均量 1.5 倍（V3.1：易点1.53、领湃1.96）
TRIAL_UP = 0.015          # 试盘日涨幅 >= 1.5%（V3.1：昆船 8-06 +1.54%）
GAP_MIN, GAP_MAX = 18, 30  # 试盘日到信号日间距（交易日）
DROP_MIN, DROP_MAX = 0.065, 0.35  # 距近60日峰值回撤范围（V3.1：昆船 6.92%）
PEAK_WINDOW = 60

# ---- V3 贴线带与方向约束 ----
NEAR_UP = 0.05            # 收盘最多高于 MA60 5%
NEAR_DOWN = 0.07          # 收盘最多低于 MA60 7%（容纳金杯 -6.39%、天顺 -6.42%）
DIP_WINDOW = 10           # 轨迹窗口
TREND_ABOVE_MIN = 0.01    # 窗口内须曾在 MA60 上方 >=1%
FALL_MIN = 0.02           # 前4日均偏离 - 末3日均偏离 >= 2 个百分点（须向下回落）

# ---- V3.2 量价企稳过滤（2026-11-16，六校准样本前3日/提示日量价研究得出）----
# 六样本提示日全部缩量、坏形态全部放量上攻，是最有区分力的三个条件：
#   C1 提示日量 <= 40日均量 0.85 倍（样本 0.34~0.76；坏形态 0.76~1.61）
#   C2 提示日量 <= 前3日均量（样本 0.58~0.94；坏形态 0.74~2.55，均值1.44）
#   C4 试盘日量比 / 提示日量能倍数 >= 2.5（样本 2.83~9.23；把试盘日量比与提示日缩量联动）
VOL_FILTER_ON = True       # 总开关，False 可整体关闭退回 V3.1
SIG_VR40_MAX = 0.85        # 提示日量能上限（相对近40日均量）
SHRINK_VS_PREV3_MAX = 1.0  # 提示日量 / 前3日均量 上限
TRIAL_SIG_VR_RATIO_MIN = 2.5  # 试盘日量比 / 提示日量能倍数 下限

# ---- V3.3 提示日 K 线企稳过滤（2026-11-16，K线形态分箱胜率研究得出）----
# 六样本=长下影小实体探底回升锤子线；坏形态=长上影大实体放量上攻，形态恰好相反。
KLINE_FILTER_ON = True        # 总开关，False 可整体关闭退回 V3.2
AMP_MAX = 0.04                # K1 提示日振幅上限（相对前收）；样本2.48~3.27%，>=4%档明显亏损
LOWER_SHADOW_MIN = 0.30       # K2 下影占全幅下限；样本0.35~0.68，坏形态均值0.14
C_FROM_LOW_MIN, C_FROM_LOW_MAX = 0.01, 0.03  # K3 收盘距当日最低甜区；样本1.14~2.30%
LOW_VS_PREV_MAX = 0.005       # K5 最低价须破/贴近前日低点；样本-1.9%~+0.4%，坏形态均值+2.9%

# ---- V2 旧阈值（legacy 保留，便于回退/对照）----
V2_NEAR_MA_DEV = 0.05
V2_MA60_MAX_BREAK = 0.015
V2_TREND_ABOVE_MIN = 0.015
V2_DIP_MAX_BELOW = 0.03


def _trial_index(bars, n):
    for gap in range(GAP_MIN, GAP_MAX + 1):
        i = n - 1 - gap
        if i < 40:
            break
        b = bars[i]
        avg_v = sum(x["v"] for x in bars[i - 40:i]) / 40
        if (b["c"] > b["o"] and (b["c"] / b["o"] - 1) >= TRIAL_UP
                and avg_v > 0 and b["v"] >= TRIAL_VOL_RATIO * avg_v):
            return i
    return None


def _trajectory(bars, n):
    """近 DIP_WINDOW 日收盘相对当时 MA60 的偏离序列。"""
    devs = []
    for i in range(n - DIP_WINDOW, n):
        ma_i = sum(b["c"] for b in bars[i - 59:i + 1]) / 60
        devs.append(bars[i]["c"] / ma_i - 1 if ma_i else 0.0)
    return devs


def detect_coil_v2(bars):
    """B_V3 主检测。返回信号 dict 或 None。
    字段与 V2 完全兼容（score/gap_days/drop_from_peak/dev60/near_ma/trial_date），
    另新增 dev_signed（带符号偏离）、fall_pp（轨迹落差，百分点）、
    V3.2 量价字段（sig_vr40/shrink_prev3/trial_vr）、
    V3.3 K线字段（k_amp/k_lower/k_c_from_low/k_low_vs_prev）。
    """
    n = len(bars)
    if n < 65:
        return None
    bars = add_indicators(bars)
    cur = bars[-1]
    ma60 = cur.get("ma60")
    if ma60 is None or ma60 <= 0:
        return None

    # 1) 贴线带：允许回踩时深破 MA60 到 -7%
    dev = (cur["c"] - ma60) / ma60
    if not (-NEAR_DOWN <= dev <= NEAR_UP):
        return None

    # 2) 企稳：小阳/小阴/十字均可，实体 -1% ~ +4%
    body = cur["c"] / cur["o"] - 1
    if not (BODY_MIN <= body <= YANG_MAX):
        return None

    # 3) 试盘日（不变）
    trial_idx = _trial_index(bars, n)
    if trial_idx is None:
        return None
    gap = (n - 1) - trial_idx

    # 4) 距前高（不变）
    peak = max(b["h"] for b in bars[-PEAK_WINDOW:])
    drop = (peak - cur["c"]) / peak
    if not (DROP_MIN <= drop <= DROP_MAX):
        return None

    # 5) 方向约束（新）：上方运行过 + 轨迹向下回落
    fall_pp = 0.0
    if n >= DIP_WINDOW + 60:
        devs = _trajectory(bars, n)
        if devs:
            if max(devs) < TREND_ABOVE_MIN:      # 没在上方运行过 = 均线下方爬升
                return None
            head = sum(devs[:4]) / 4
            tail = sum(devs[-3:]) / 3
            fall_pp = (head - tail) * 100
            if fall_pp < FALL_MIN * 100:         # 轨迹不是向下（横盘/上行）
                return None

    # 6) 量价企稳过滤（V3.2）：放量试盘 → 缩量回踩 → 提示日再缩量
    sig_vr = shrink = trial_vr = None
    if VOL_FILTER_ON:
        avg_v_sig = sum(x["v"] for x in bars[n - 41:n - 1]) / 40   # 近40日均量，不含当日
        sig_vr = cur["v"] / avg_v_sig if avg_v_sig > 0 else 0.0
        prev3 = bars[n - 4:n - 1]                                   # T-3, T-2, T-1
        shrink = cur["v"] / (sum(x["v"] for x in prev3) / 3)
        avg_v_t = sum(x["v"] for x in bars[trial_idx - 40:trial_idx]) / 40
        trial_vr = bars[trial_idx]["v"] / avg_v_t if avg_v_t > 0 else 0.0
        if sig_vr > SIG_VR40_MAX:                    # C1 提示日必须缩量
            return None
        if shrink > SHRINK_VS_PREV3_MAX:             # C2 相对前3日继续缩量
            return None
        if sig_vr <= 0 or trial_vr / sig_vr < TRIAL_SIG_VR_RATIO_MIN:  # C4 试盘量比联动
            return None

    # 7) 提示日 K 线企稳过滤（V3.3）：长下影 + 小振幅 + 收盘距低适中 + 盘中破前低
    #    即「盘中下探破 MA60/前低 → 尾盘收回」的探底回升锤子线，区别于放量上攻的长上影大实体。
    k_amp = k_lower = k_c_low = k_low_prev = None
    if KLINE_FILTER_ON:
        prev = bars[-2]
        rng = cur["h"] - cur["l"] if cur["h"] > cur["l"] else 1e-9
        k_amp = rng / prev["c"] if prev["c"] > 0 else 9.9
        k_lower = (min(cur["o"], cur["c"]) - cur["l"]) / rng
        k_c_low = cur["c"] / cur["l"] - 1 if cur["l"] > 0 else 9.9
        k_low_prev = cur["l"] / prev["l"] - 1 if prev["l"] > 0 else 9.9
        if k_amp > AMP_MAX:                                  # K1 振幅收敛
            return None
        if k_lower < LOWER_SHADOW_MIN:                       # K2 长下影
            return None
        if not (C_FROM_LOW_MIN <= k_c_low <= C_FROM_LOW_MAX):  # K3 收盘距低甜区
            return None
        if k_low_prev > LOW_VS_PREV_MAX:                     # K5 盘中须破/贴近前低
            return None

    score = 100 - abs(dev) * 1000 - abs(gap - 24) * 1.5 - abs(drop - 0.20) * 120
    return {
        "signal": True,
        "score": max(round(score, 2), 0),
        "gap_days": gap,
        "drop_from_peak": round(drop, 4),
        "dev60": round(abs(dev), 4),          # 保持 V2 口径（绝对值），兼容下游
        "dev_signed": round(dev, 4),
        "fall_pp": round(fall_pp, 2),
        "sig_vr40": round(sig_vr, 3) if sig_vr is not None else None,
        "shrink_prev3": round(shrink, 3) if shrink is not None else None,
        "trial_vr": round(trial_vr, 3) if trial_vr is not None else None,
        "k_amp": round(k_amp, 4) if k_amp is not None else None,
        "k_lower": round(k_lower, 3) if k_lower is not None else None,
        "k_c_from_low": round(k_c_low, 4) if k_c_low is not None else None,
        "k_low_vs_prev": round(k_low_prev, 4) if k_low_prev is not None else None,
        "near_ma": "MA60",
        "trial_date": bars[trial_idx]["d"],
    }


def detect_coil_v2_legacy(bars):
    """B_V2 原版逻辑（2026-11-09/10 版本），仅供回退与对照回测。"""
    n = len(bars)
    if n < 65:
        return None
    bars = add_indicators(bars)
    cur = bars[-1]
    ma60 = cur.get("ma60")
    if ma60 is None:
        return None
    dev60 = abs(cur["c"] - ma60) / ma60
    if dev60 > V2_NEAR_MA_DEV:
        return None
    if not ((cur["c"] >= cur["o"]) and (cur["c"] / cur["o"] - 1) <= YANG_MAX):
        return None
    trial_idx = _trial_index(bars, n)
    if trial_idx is None:
        return None
    gap = (n - 1) - trial_idx
    peak = max(b["h"] for b in bars[-PEAK_WINDOW:])
    drop = (peak - cur["c"]) / peak
    if not (DROP_MIN <= drop <= DROP_MAX):
        return None
    if cur["c"] < ma60 * (1 - V2_MA60_MAX_BREAK):
        return None
    if n >= DIP_WINDOW + 60:
        devs = _trajectory(bars, n)
        if devs:
            if max(devs) < V2_TREND_ABOVE_MIN:
                return None
            if min(devs) < -V2_DIP_MAX_BELOW:
                return None
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
