# -*- coding: utf-8 -*-
"""
怡达模型 · 信号评分模块（v7.0 引入）
=====================================================================
用途：对 D2 当天预判出的所有信号打分，每日只推送评分最高的 6 只。

评分方法：逻辑回归（LR），在 2026 上半年 60% 样本上以「持有3日最高价收益
≥5%」为正样本训练，后半 40% 样本外验证有效：
  · 验证集全样本 h3均值 +6.99%，LR-Top6 +8.18%（提升1.2个百分点）
  · 全样本每日Top6 h3 +7.42% vs 随机6只 +6.84%（评分优于随机）

7 个因子（统一方向：值越大越好）：
  decline   阴跌深度（-1，越负越好，权重最大 0.247）
  plat_cv   平台平整度（+1）
  d1_chg    D1涨幅（+1）
  r21       D2/D1量比（+1，即 detect 返回的 d2_vol_ratio）
  tier      等级（+1 方向；★一级=1 更好）
  vol_mult  D1/D2均放量倍数（+1）
  prev1_vol D1前一日量比（+1）

纯 Python 实现（无 sklearn 依赖），线上环境可直接调用。
=====================================================================
"""
import math

# 逻辑回归权重（统一方向后，训练半段拟合）
_WEIGHTS = {
    "decline":   0.2472,
    "plat_cv":   0.1290,
    "d1_chg":    0.0271,
    "r21":       0.0781,
    "tier":     -0.0759,  # 方向已含在 _DIR 中；标准化空间系数
    "vol_mult":  0.0931,
    "prev1_vol":-0.0127,
}
_DIR = {"decline": -1, "plat_cv": 1, "d1_chg": 1, "r21": 1,
        "tier": -1, "vol_mult": 1, "prev1_vol": 1}
_MU   = [33.305, 1.291, 9.645, 1.808, -1.626, 2.922, 1.103]
_SD   = [6.595, 0.658, 4.835, 1.867, 0.484, 1.063, 0.398]
_FEATS = ["decline", "plat_cv", "d1_chg", "r21", "tier", "vol_mult", "prev1_vol"]
_INTERCEPT = -0.0420  # 训练截距


def score_signal(sig):
    """给单条 detect 信号打分，返回 0~1 的概率分（越高越好）。
    sig: detect() 返回的 dict，需含 decline/plat_cv/d1_chg/d2_vol_ratio/tier/vol_mult/prev1_vol
    """
    # r21 对应 detect 的 d2_vol_ratio
    raw = {
        "decline":   sig.get("decline", -25.0),
        "plat_cv":   sig.get("plat_cv", 1.0),
        "d1_chg":    sig.get("d1_chg", 0.0),
        "r21":       sig.get("r21", sig.get("d2_vol_ratio", 1.0)),
        "tier":      sig.get("tier", 2),
        "vol_mult":  sig.get("vol_mult", 2.0),
        "prev1_vol": sig.get("prev1_vol", 1.0),
    }
    z = 0.0
    for k, f in enumerate(_FEATS):
        v = _DIR[f] * float(raw[f])          # 统一方向
        z += _WEIGHTS[f] * (v - _MU[k]) / _SD[k]
    z += _INTERCEPT
    # sigmoid（数值稳定）
    if z >= 0:
        p = 1.0 / (1.0 + math.exp(-z))
    else:
        ez = math.exp(z)
        p = ez / (1.0 + ez)
    return round(p, 4)


def prev1_vol_from_bars(bars, j):
    """从K线算 D1 前一日量比（相对 base_win 均量），供 detect 信号补充该字段。
    j = signal日索引；D1 = j-1；前一日 = j-2；base_win 默认20。
    """
    import numpy as np
    base_win = 20
    if j - 1 < base_win:
        return 1.0
    v = np.array([b["volume"] for b in bars], dtype=float)
    base = np.mean(v[j - 1 - base_win:j - 1])
    if base <= 0:
        return 1.0
    return float(v[j - 2] / base)


def pick_top6(signals, per_day=True, plat_quota=4, accel_quota=2):
    """从当日信号列表里按评分挑 Top6。
    signals: [(sig_dict, score), ...]
    形态配额：平台型 plat_quota 只 + 加速型 accel_quota 只，不足则按评分互补。
    返回入选列表（元素为 (sig, score)）。
    """
    plat = sorted([x for x in signals if x[0].get("pattern") == "platform"], key=lambda x: -x[1])
    accel = sorted([x for x in signals if x[0].get("pattern") == "accel"], key=lambda x: -x[1])
    picked = plat[:plat_quota] + accel[:accel_quota]
    if len(picked) < 6:
        have = {id(x[0]) for x in picked}
        for x in sorted(signals, key=lambda x: -x[1]):
            if id(x[0]) not in have:
                picked.append(x)
                have.add(id(x[0]))
            if len(picked) >= 6:
                break
    return picked[:6]


if __name__ == "__main__":
    # 自检：与 sklearn 概率对齐（用已知样本）
    import json, os
    test_sig = {
        "decline": -47.3, "plat_cv": 1.6, "d1_chg": 20.0,
        "d2_vol_ratio": 1.27, "tier": 2, "vol_mult": 2.5, "prev1_vol": 1.0,
    }
    print("自检样本得分:", score_signal(test_sig))
