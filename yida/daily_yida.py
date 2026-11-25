#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
怡达模型 · 每日选股（接入统一前置流水线）
================================================================
源自怡达股份(300721) 2026-08~11 走势提炼，全A股回溯验证。

【版本】v5.4 D2实时预判版（2026-11-25）
  · 一套引擎通吃两种强势形态：平台型（怡达原型）+ 加速型（南极光原型）
  · 黄金样本标定：超声电子/肯特股份/怡达股份（平台型）、南极光（加速型）
  · v5.4 核心修正：取消"4天放量确认"滞后——原v5要D1~D4四根齐才能算CV分流,
    导致D2提示实际要等D4收盘才发(马后炮,D3买点已过)。v5.4改为D2当天就用
    D1+D2两天数据出提示,标签按 D2/D1量比 预判,确保D3可实际买入。
  · 买点：D2盘后出信号 → 次日D3开盘价买入 + T+1(退出从D4起)
    （entry_rule 可切回 d2_close 回退；黄金样本标签预判5/5正确）

【统一骨架】阴跌 + D1/D2连续2天≥2倍放量 + D1前一日≤2.5倍 + 提示日D2
【双标签预判（只用D1/D2两天）】
  · 平台型(platform)：D2/D1量比<1.35 量能平稳
      - 阴跌≥30%，D1涨停豁免或2~7倍，D2严格2~7倍
      - 跟踪5日 + 破位/缩量退出
  · 加速型(accel)：D2/D1量比≥1.35（D2明显放量、递增启动）
      - 阴跌≥20%，两天均≥2倍（不设上限）
      - 跟踪3日固定止盈（无破位退出）
      - 黄金样本：南极光300940 D2/D1量比1.54→预判加速型

模型规则（参数见 config.json，可改）：
  ① 阴跌前置：D1前一日收盘较过去60日最高价跌幅达标，且收盘 < MA60
     （平台型≥30% / 加速型≥20%）
  ② 放量：D1、D2连续2天成交量 ≥ D1前20日均量的 2倍（v5.4预判,不再等D3/D4）
     · 平台型：D1涨停豁免或2~7倍，D2严格2~7倍，且 D2/D1量比<1.35
     · 加速型：两天≥2倍、D2/D1量比≥1.35（预判为递增启动）
  ③ 提示日 = 放量第2天(D2)当天盘后即发（不再等D3/D4确认）
  ④ 买点 = D3开盘价；退出：平台型跟踪5日(破位/缩量/移动止盈)；加速型跟踪3日固定
  ⑤ 命中 = 策略退出收益 ≥ 3%；D3/D4若缩量,历史跟踪按破位/缩量退出兜底

推送（严格两段式，2026-11-24 定版）：
  · 第一段 = 当天提示的票（signal == 最新交易日）
  · 第二段 = 历史提示、未破位、仍在5日观察期内的票
  · ★破位/缩量的票 → 从K线重算破位线判定后直接剔除，完全不推送（无退出告知段）
  · ★观察期满(≥5交易日) → 自然剔除
  · 破位线不依赖台账字段（历史台账常缺 break_line），一律由 signal 前一根D1实体中点重算

数据流（完全遵循仓库共同前置框架）：
  · 样本池：读 common/state/universe.json（已剔除 ST/立信/监管立案/次新）
  · K线：读 common/state/klines 共享缓存（ctx.kline，严格离线）
  · 台账：ctx.record() 落盘，自动进入次日统一回溯（条件④）
  · 推送：复用 common/notify.py（PushPlus，PUSHPLUS_TOKEN）

用法：
  python3 yida/daily_yida.py              # 正式（选股+台账+推送）
  python3 yida/daily_yida.py --no-push    # 本地复跑/回溯不推送
"""
import os
import sys
import json
import argparse
import datetime
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
for _p in (REPO, os.path.join(REPO, "common")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import kit
import notify as common_notify

CFG_PATH = os.path.join(HERE, "config.json")
CFG = json.load(open(CFG_PATH, encoding="utf-8"))


def load_cfg():
    return CFG


# ---------- 形态识别 ----------
def _limit_up_threshold(code, cfg):
    """按板块返回涨停判定阈值（留四舍五入余量）：
       创业板/科创板 20cm / 北交所 30cm / 沪深主板 10cm"""
    s = str(code).zfill(6)
    if s.startswith(("300", "301", "688", "689")):
        return cfg.get("limit_up_growth", 0.195)
    if s.startswith(("8", "4", "92")):
        return cfg.get("limit_up_bj", 0.295)
    return cfg.get("limit_up_main", 0.098)


def _rebound_filter(c, o, i, j, d1_limit_up, cfg):
    """下跌中继反抽过滤（v5.1，仅平台型）：剔除高位诱多尾部风险。
    大亏损10只前5日涨幅全部为正(0.4~10.1%)＝下跌途中反抽放量；黄金样本要么
    前5日为负(真底部)要么>10%(真突破)。命中任一即剔除：
      A) D1涨停豁免 且 D1前5日涨幅>0
      B) D1前5日涨幅 0.5~11% 且 (|D2涨跌幅|≥8% 或 D2收阴)
      C) D1前5日涨幅 2~11% 且 D2温和阳线 0~6%
    2026回测：10/10全拦截大亏损，黄金5/5零误伤。"""
    if not cfg.get("risk_filter", True):
        return False
    if i < 6:
        return False
    pre5 = (c[i - 1] / c[i - 6] - 1) * 100
    d2_chg = (c[j] / c[i] - 1) * 100
    d2_yin = 1 if c[j] < o[j] else 0
    a = bool(d1_limit_up and pre5 > 0)
    b = bool(0.5 <= pre5 <= 11 and (abs(d2_chg) >= 8 or d2_yin))
    cc = bool(2 <= pre5 <= 11 and 0 < d2_chg < 6)
    return a or b or cc


def _d1_shape_ok(o, c, code, i, cfg):
    """v6 规则①：D1质量 = 阳包阴 或 D0涨停豁免。
    阳包阴 = D0阴线 且 D1阳线 且 D1收≥D0开 且 D1开≤D0收。
    D0涨停(主板9.8%/双创19.5%/北交29.5%)则豁免——救回'涨停后整理'的追涨型样本(超声/肯特二次)。
    返回 (是否通过, D0是否涨停, D1是否阳包阴)。"""
    if not cfg.get("shape_filter", True):
        return True, False, False
    if i < 2:
        return True, False, False
    d0_lu = c[i - 1] / c[i - 2] - 1 >= _limit_up_threshold(code, cfg)
    d0_yin = c[i - 1] < o[i - 1]
    d1_yang = c[i] > o[i]
    baoyin = bool(d0_yin and d1_yang and c[i] >= o[i - 1] and o[i] <= c[i - 1])
    return (d0_lu or baoyin), bool(d0_lu), baoyin


def _d2_giveback_ok(c, i, j, d0_lu, cfg):
    """v6 规则②：D2回吐不过半。D2收盘不得把D1涨幅跌回一半以上；
    D0涨停时按D0涨停幅度的一半为限(追涨型样本的缓冲更宽)。
    giveback = D1收 - D2收；half = D0涨停半幅 或 D1涨幅半幅。"""
    if not cfg.get("giveback_filter", True):
        return True, 0.0, 0.0
    d1_gain = c[i] - c[i - 1]
    half = (c[i - 1] - c[i - 2]) / 2 if d0_lu else d1_gain / 2
    giveback = c[i] - c[j]
    return giveback <= half, float(giveback), float(half)


def _platform_cv(c, low, i, cfg):
    """v6 规则③：D1前的下跌企稳平台。在 D1前 lookback~3 日区间找收盘CV最小的
    win 日窗口，返回 (最小CV, 窗口起点, 平台低点=窗口内最低价)。平台越窄越可靠。
    与回测口径一致：CV用收盘价算，平台低点用 low 的最低值。"""
    win = int(cfg.get("platform_cv_win", 5))
    lbk = int(cfg.get("platform_cv_lookback", 12))
    best = None
    for st in range(max(win, i - lbk), i - 3):
        seg = c[st:st + win]
        if len(seg) < win:
            continue
        cv = float(np.std(seg) / np.mean(seg)) if np.mean(seg) > 0 else 9.9
        if best is None or cv < best[0]:
            best = (cv, st)
    if best is None:
        return 9.9, i - win, float(np.min(low[max(0, i - win):i]))
    cv, st = best
    return cv, st, float(np.min(low[st:st + win]))


def _fake_break(c, i, st, plat_low):
    """v6 规则④(分级非过滤)：假破位收复 = 平台后曾有收盘跌破平台低点，但D1火速收复。
    仅作★一级标记，不做硬过滤——肯特/南极光等黄金样本无破位，硬过滤会误伤。"""
    if i <= st + 5:
        return False
    broke = any(c[t] < plat_low for t in range(st + 5, i))
    return bool(broke and c[i] > plat_low)


def exit_sim(o, h, c, v, dates, j, buy, pattern, pat_hold, cfg, break_line, shrink_line, n,
             buy_idx=None):
    """返回 (ret%, exit_reason, exit_day, held, max_price)。
    v6.1 收益口径开关 ret_basis：
      · "high"（默认）：买入后持有窗口内最高价收益 = (窗口最高价/买入价-1)*100。
        用户认可口径——盘中可触及，符合"D3-D4-D5最高点"止盈。
      · "strategy"：v5.3 分类止盈（破位/缩量/移动止盈/到期收盘），保守口径。
    v5.3 说明（strategy口径）：
    v5.3 新增 buy_idx=实际买入日索引（D3）：T+1 当日不可卖，退出判定从 buy_idx+1（D4）起。
      不传 buy_idx 时兼容 v5.2（判定从 j+1 起）。
    平台型：盘中触及 tp_activate(默认+10%)激活移动止盈，此后收盘跌破『持仓期收盘峰值
      ×(1-tp_drawdown)』离场；破位/缩量硬退出优先；pat_hold 日到期按收盘平仓。
    加速型：死拿 pat_hold 日收盘，不设任何中途退出。
    ret 为策略实际退出收益（不再是关注期最高价），hit 据此判定。"""
    max_price = float(h[buy_idx] if buy_idx is not None else j)
    # v6.1 最高价口径：买入后持有窗口(D3~D5)内最高价收益，用户认可口径
    if cfg.get("ret_basis", "high") == "high" and buy_idx is not None:
        end = min(j + pat_hold, n - 1)
        if end >= buy_idx:
            win_high = float(np.max(h[buy_idx:end + 1]))
            max_price = win_high
            ret = (win_high / buy - 1) * 100
            return ret, None, None, end - j, max_price
    # 判定起点：v5.3 T+1 = 买入次日；v5.2 = D2 次日
    check_from = (buy_idx + 1) if buy_idx is not None else (j + 1)
    if pattern == "accel":
        k = min(j + pat_hold, n - 1)
        if k > j:
            max_price = max(max_price, float(np.max(h[check_from:k + 1])) if k >= check_from else max_price)
        ret = (float(c[k]) / buy - 1) * 100
        return ret, None, None, k - j, max_price

    # ---- 平台型：硬退出 + 宽松移动止盈 + 到期 ----
    activate = cfg.get("tp_activate", 0.10)
    drawdown = cfg.get("tp_drawdown", 0.03)
    peak_close = buy
    activated = False
    # T+1：买当天(check_from 之前)不判定退出，但峰值照常累积
    for k in range(check_from, min(j + pat_hold, n)):
        hi, cl = float(h[k]), float(c[k])
        max_price = max(max_price, hi)
        peak_close = max(peak_close, cl)
        if not activated and hi / buy - 1 >= activate:
            activated = True
        if cl < break_line:
            return (cl / buy - 1) * 100, "破位", dates[k], k - j, max_price
        if v[k] < shrink_line:
            return (cl / buy - 1) * 100, "缩量", dates[k], k - j, max_price
        if activated and cl <= peak_close * (1 - drawdown):
            return (cl / buy - 1) * 100, "移动止盈", dates[k], k - j, max_price
    k = min(j + pat_hold, n - 1)
    ret = (float(c[k]) / buy - 1) * 100
    return ret, None, None, k - j, max_price


def detect(bars, cfg, code=""):
    """bars: 共享K线列表 [{date,open,high,low,close,volume}, ...] 升序
    code: 6位代码，用于判断D1是否涨停（涨停启动日豁免规则）
    返回最新一个有效信号 dict 或 None"""
    if not bars or len(bars) < cfg["lookback"] + cfg["base_win"] + cfg["hold_days"] + 5:
        return None
    dates = [str(b["date"]) for b in bars]
    h = np.array([float(b["high"]) for b in bars])
    c = np.array([float(b["close"]) for b in bars])
    v = np.array([float(b["volume"]) for b in bars])
    o = np.array([float(b["open"]) for b in bars])
    l = np.array([float(b["low"]) for b in bars])
    n = len(c)
    lb, bw = cfg["lookback"], cfg["base_win"]
    hold, sig_day = cfg["hold_days"], cfg["signal_day"]

    # 只用最近 window 天找信号，避免每天重复推同一形态
    start = max(lb + bw, n - cfg.get("recent_window", 15))
    # 滚动基准
    base = np.array([np.mean(v[i - bw:i]) if i >= bw else np.nan for i in range(n)])
    ma60 = np.array([np.mean(c[i - lb:i]) if i >= lb else np.nan for i in range(n)])
    peak = np.array([np.max(h[i - lb:i]) if i >= lb else np.nan for i in range(n)])

    # v5.4：从后往前扫,优先返回窗口内【最新】信号(避免旧信号吃掉当天新提示)
    for i in range(n - 2, start - 1, -1):
        b = base[i]
        if not np.isfinite(b) or b <= 0 or not np.isfinite(peak[i]):
            continue
        prev = c[i - 1]
        decline = prev / peak[i] - 1
        if prev >= ma60[i]:          # 统一骨架：必须在 MA60 之下（空头排列）
            continue
        # v5.4 D2实时预判：只用 D1、D2 两根放量K线（不再等D3/D4）
        #   原v5需四根齐才能算CV分流,导致D2提示滞后到D4收盘(马后炮)。
        #   v5.4只要D1+D2连续2天放量就当天出提示,标签按 D2/D1 量比预判。
        j = i + sig_day - 1          # 提示日 D2 = i+1
        if j >= n:
            continue
        vols = v[i:j + 1]            # D1、D2 两根
        if len(vols) < 2:
            continue
        ratios = vols / b
        d1_limit_up = c[i] / c[i - 1] - 1 >= _limit_up_threshold(code, cfg)
        # D1前一日不得已放量（确保D1是启动首日）
        if v[i - 1] / b > 2.5:
            continue
        r21 = float(v[j] / v[i])     # D2/D1 量比 = 标签预判分水岭

        # ===== v6.1 统一纳入门槛（只用D1+D2两天，修复v5.4双标签分流边界缺陷）=====
        # 旧版按r21硬分流后各卡各门槛：涨停票(r21≥1.35)被判加速却要求D1≥2x而漏掉；
        #   阴跌20~30%的票(r21<1.35)被判平台却要求≥30%而漏掉。共漏18/273历史样本。
        # 新版：统一门槛=阴跌≥20% + D1放量(涨停豁免或≥2x) + D2放量≥2x；
        #   r21仅用于标签展示(加速/平台)，不再因标签不同而卡门槛。392样本+曼卡龙全纳入。
        decline_ok = decline <= -cfg.get("accel_decline_min", 0.20)
        d1_vol_ok = d1_limit_up or ratios[0] >= cfg["vol_lo"]
        d2_vol_ok = ratios[1] >= cfg["vol_lo"]
        if not (decline_ok and d1_vol_ok and d2_vol_ok):
            continue
        # ===== v6.2 D1真涨过滤（2026-11-25）：D1收盘≥前收 或 D0涨停豁免 =====
        # 背景：维尔利11-23是"假阳真跌"(开2.75收2.78但较11-20收2.80仍跌0.71%)，
        #   本质是下跌途中的放量、非企稳启动；纯涨幅门槛会把它放进来。
        # 规则：D1必须相对前一日真涨(收盘≥前收)；若D0涨停(如肯特二次8-24的D0=8-21涨停)则豁免。
        # 验证：黄金5/5全保住(肯特核心8-21涨停+20%真涨；肯特二次靠D0涨停豁免)、
        #   曼卡龙11-23(+16%)保住、维尔利(-0.71%且D0非涨停)精准挡掉。
        if cfg.get("d1_real_up_filter", True):
            d1_real_up = c[i] >= c[i - 1]
            d0_limit_up = c[i - 1] / c[i - 2] - 1 >= _limit_up_threshold(code, cfg) if i >= 2 else False
            if not (d1_real_up or d0_limit_up):
                continue
        # ===== v6.3 弱反弹过滤（2026-11-25）：剔除"阴跌反弹一日游" =====
        # 背景：茶花股份/宁波富达11-23这类——D0加速大阴、D1只是超跌反抽(涨幅<10%)、
        #   D2随即跌回D1收盘之下(不续涨)。本质是下跌中继的弱反弹,非企稳启动。
        # 规则：D1涨幅≥10%(涨停级启动) 或 D2收盘≥D1收盘(续涨确认)，两条满足其一才纳入；
        #   两条都不满足(弱D1+回吐D2)→剔除。只用D1/D2,严守D2当天预判。
        # 验证：黄金5+曼卡龙全保(超声/南极光靠D2续涨,肯特/怡达/曼卡龙靠D1涨幅)；
        #   392可核验273只中剔除40只(14.7%),剔除组均值+9.46%/命中87.5%低于保留组
        #   +13.50%/93.1%,零深亏——纯提纯、不避险。开关false可回退v6.2。
        if cfg.get("weak_rebound_filter", True):
            d1_chg_pct = c[i] / c[i - 1] - 1
            d2_holds = c[j] >= c[i]
            if not (d1_chg_pct >= cfg.get("weak_rebound_d1_min", 0.10) or d2_holds):
                continue
        # ===== v6.5 收复前5日跌幅过滤（2026-11-25 折中版，默认开）=====
        # 背景：用户要求"D1D2两天必须收复前5日跌幅，避免下跌弱反弹"。
        # 直接硬卡"D2收盘≥5日前收盘"会误伤15只深跌V反强势票(如科兴D2-12.6%但3日+23%)。
        # 折中版：只在【前5日确实在跌(跌幅≤阈值-3%)】且【D2仍未收复5日前收盘】时才剔除——
        #   放过"前5没怎么跌、只是D2深调"的票；只剔"真跌了却没收回"的弱反弹。
        # 只用 D1(i)、D2(j) 及 j-5 收盘，全部在提示日及之前，严守D2当天预判。
        # 验证：黄金6样本零误伤；392可核验273只剔除21只(均值+8.14%/命中81.0% < 保留+13.30%/93.3%)；
        #   对11-25当天10只新提示零影响(全部已收复5日)。false可回退v6.4。
        if cfg.get("recover5_filter", False) and j >= 5:
            c5 = c[j - 5]                       # 5个交易日前收盘
            dec5 = c[i - 1] / c5 - 1            # 前5日跌幅(到D1前收)
            if dec5 <= -cfg.get("recover5_dec5_min", 0.03) and c[j] < c5:
                continue                        # 真跌了却没收回 → 弱反弹,剔除
        # 标签预判（仅展示/分级，不影响纳入）
        if r21 >= cfg.get("vol_ratio_accel", 1.35):
            pattern, pat_hold = "accel", cfg.get("accel_hold_days", 3)
        else:
            pattern, pat_hold = "platform", cfg["hold_days"]
        # v5.1 风控：平台型若命中「下跌中继反抽」特征则剔除（高位诱多尾部风险）
        if pattern == "platform" and _rebound_filter(c, o, i, j, d1_limit_up, cfg):
            continue
        # ===== v6.1 形态字段：仅计算供★分级展示，三重硬过滤默认全部关闭 =====
        # 依据(2026-11-25 最高价口径决定性验证)：
        #   392历史样本+曼卡龙必须100%纳入；最高价口径下 v6砍掉的275只收益+8.90%/命中80%/深亏0，
        #   与保留的+10.15%/82%/0几乎一致——形态过滤没筛掉风险，白白丢了机会。
        #   收盘口径下"阳包阴最优"是假象(最高价口径各组均+9~10.7%)。
        # 字段照算(供tier分级/回溯)，三个过滤由 config 开关控制，默认 false=全纳入。
        shape_ok, d0_lu, d1_baoyin = _d1_shape_ok(o, c, code, i, cfg)
        give_ok, giveback, give_half = _d2_giveback_ok(c, i, j, d0_lu, cfg)
        plat_cv, plat_st, plat_low = _platform_cv(c, l, i, cfg)
        # —— 三重硬过滤（默认关，仅回退/用户自建筛选时开启）——
        if cfg.get("shape_filter", False) and not shape_ok:
            continue
        if cfg.get("giveback_filter", False) and not give_ok:
            continue
        if cfg.get("platform_cv_filter", False) and plat_cv > cfg.get("platform_cv_max", 0.03):
            continue
        # ★假破位收复：仅分级标记,不硬过滤（不影响纳入，只排序优先）
        fake_break = _fake_break(c, i, plat_st, plat_low) if cfg.get("tier1_fake_break", True) else False
        tier = 1 if fake_break else 2
        d1_chg = c[i] / prev - 1
        # v5.4：破位线 = D1实体中点；缩量线 = 两天放量均量×ratio（预判只用D1/D2）
        break_line = (float(bars[i]["open"]) + float(bars[i]["close"])) / 2
        shrink_line = np.mean(vols) * cfg.get("shrink_ratio", 0.5)
        cv = float(np.std(vols) / np.mean(vols))  # 两天量波动率(仅作展示参考,不再用于分流)

        # v5.3 实盘买点：D2盘后出信号买不到 → 次日D3开盘价买入，T+1从D4起判定退出
        entry_rule = cfg.get("entry_rule", "d3_open")
        buy_idx = j + 1  # D3 买入日
        pending = False  # 当天最新提示：D3尚未到来，无法知道开盘价
        if entry_rule == "d3_open" and buy_idx < n:
            buy = float(o[buy_idx])       # D3 开盘价（真实可成交）
        elif entry_rule == "d3_open":
            buy = float(c[j])             # D3未到，占位（主程序标注待开盘）
            pending = True
        else:
            buy = float(c[j])             # 回退 v5.2：D2 收盘买
            buy_idx = None

        # v5.3 分类止盈结算（T+1：买当天不判定退出）
        if pending:
            # D3 未到，尚无买入与持有，收益不可结算
            ret, exit_reason, exit_day, checked, max_price = None, None, None, 0, float(c[j])
        else:
            ret, exit_reason, exit_day, checked, max_price = exit_sim(
                o, h, c, v, dates, j, buy, pattern, pat_hold, cfg, break_line, shrink_line, n,
                buy_idx=buy_idx)
        return dict(
            code="",  # 占位，主程序填 code
            pattern=pattern,
            d1=dates[i], signal=dates[j],
            buy=round(float(buy), 2),
            entry_day=dates[buy_idx] if (buy_idx is not None and buy_idx < n) else "",
            pending=pending,  # True=当天提示,D3未开盘,买入价占位
            d1_chg=round(float(d1_chg) * 100, 1),
            d2_vol_ratio=round(float(v[j] / v[i]), 2),
            decline=round(float(decline) * 100, 1),
            vol_mult=round(float(np.mean(ratios)), 2),
            cv=round(float(cv) * 100, 1),
            # v6 形态字段（供分级展示/回溯）
            tier=tier,                          # 1=★假破位收复(一级), 2=普通平台(二级)
            fake_break=bool(fake_break),
            d0_limit_up=bool(d0_lu),
            d1_baoyin=bool(d1_baoyin),
            plat_cv=round(float(plat_cv) * 100, 1),   # 平台收盘CV(%)
            plat_low=round(float(plat_low), 2),
            giveback=round(float(giveback), 2),
            break_line=round(float(break_line), 2),
            shrink_line=round(float(shrink_line)),
            hold_days=pat_hold,
            exit_reason=exit_reason,
            exit_day=exit_day,
            held_days=checked,
            ret5=round(ret, 1) if ret is not None else None,  # exit_sim已返回百分数
            hmax=round(float(max_price / buy - 1) * 100, 1),  # 关注期最高价收益(参考)
            hit=bool(ret is not None and ret >= cfg["target"] * 100),  # target(0.03)转百分数
        )
    return None


def _has_history(ctx):
    """台账里是否已有历史推荐批次"""
    try:
        import glob, os
        return len(glob.glob(os.path.join(ctx.reco_dir, "*.json"))) > 0
    except Exception:
        return False


def _load_reco_signals(ctx):
    """从台账加载历史入选信号，按 (code, 提示日signal) 去重，保留最早一次记录。
    返回 {(code,signal): sig_dict}。raw 里保存了 detect 的全部字段。"""
    import glob, os
    sigs = {}
    for f in sorted(glob.glob(os.path.join(ctx.reco_dir, "*.json"))):
        d = kit._read_json(f)
        if not d:
            continue
        rec_date = d.get("date", "")
        for p in d.get("picks", []):
            code = str(p.get("code", "")).zfill(6)
            raw = p.get("raw", {}) or {}
            signal = raw.get("signal", "")
            if not code or not signal:
                continue
            key = (code, signal)
            if key in sigs:
                continue  # 同一轮形态只保留最早记录
            sigs[key] = dict(
                code=code, name=p.get("name", ""), signal=signal,
                buy=raw.get("buy") or p.get("close"),
                pattern=raw.get("pattern", "platform"),   # 历史信号默认平台型
                hold_days=raw.get("hold_days", CFG.get("hold_days", 5)),
                break_line=raw.get("break_line"),
                shrink_line=raw.get("shrink_line"),
                vol_mult=raw.get("vol_mult"), decline=raw.get("decline"),
                cv=raw.get("cv"), d1_limit_up=raw.get("d1_limit_up"),
            )
    return sigs


def _track_signal(sig, bars, cfg, latest_date, latest_trading_day=None):
    """把单条历史信号跟踪到最新交易日（v6.4 跟踪规则）。
    ★ 跟踪三日：D2提示后 D3/D4/D5 三个交易日，期满满自然结束（不再推送）。
    ★ 止损出局：跟踪期内收盘跌破 D1 阳线实体中点 (开+收)/2 → 出局。
      此止损独立于 ret_basis，最高价口径下也照常生效（修复旧版 exit_sim 被绕过的bug）。
    ★ stop_by_low=true 时改用盘中最低价触及止损线判定，默认按收盘价(稳健)。
    所有线均从K线重算，不依赖台账可能缺失的字段。"""
    dates = [str(b["date"]) for b in bars]
    o = [float(b["open"]) for b in bars]
    h = [float(b["high"]) for b in bars]
    l = [float(b["low"]) for b in bars]
    c = [float(b["close"]) for b in bars]
    sig_date = sig.get("signal", "")
    if sig_date not in dates:
        return None
    j = dates.index(sig_date)          # D2 提示日
    if j < 1:
        return None
    pattern = sig.get("pattern", "platform")
    pat_hold = int(cfg.get("track_days", 3))   # v6.4 统一跟3日
    d1 = j - 1
    bl = (o[d1] + c[d1]) / 2                   # D1实体中点=止损线
    # 最新交易日索引（真实数据末日）
    last_idx = len(c) - 1
    ref_day = latest_trading_day or latest_date
    if ref_day in dates:
        last_idx = dates.index(ref_day)
    # 买入价 D3 开盘
    entry_rule = cfg.get("entry_rule", "d3_open")
    buy_idx = j + 1
    if entry_rule == "d3_open" and buy_idx < len(c):
        buy = float(o[buy_idx])
    else:
        buy = float(sig["buy"]) if sig.get("buy") else c[j]
    # ---- 跟踪窗口 D3..D(j+pat_hold) ----
    win_end = min(j + pat_hold, last_idx, len(c) - 1)
    stop_reason, stop_day, stop_ret = None, None, None
    for k in range(j + 1, win_end + 1):
        trig = (l[k] < bl) if cfg.get("stop_by_low", False) else (c[k] < bl)
        if trig:
            stop_reason, stop_day = "破D1半", dates[k]
            stop_ret = (c[k] / buy - 1) * 100 if buy > 0 else 0.0
            break
    held = sum(1 for d in dates if sig_date < d <= dates[last_idx])
    if stop_ret is not None:
        ret_pct = stop_ret
    else:
        px = float(c[last_idx])
        ret_pct = (px / buy - 1) * 100 if buy > 0 else 0.0
    return dict(exit_day=stop_day, exit_reason=stop_reason, pattern=pattern,
                pat_hold=pat_hold, ret=round(ret_pct, 1), held=held,
                buy=round(buy, 2), break_line=round(float(bl), 2))


def _push_two_section(ctx, picks, universe, cfg):
    """严格两段式推送（用户定版 2026-11-24）：
    第一段 = 当天提示的票（signal == 最新交易日/最新提示日）
    第二段 = 历史提示、仍在5日观察期、且未破位的票
    ★ 破位/缩量的票 → 直接剔除，完全不推送（无退出告知段）
    ★ 观察期满(≥5交易日) → 自然剔除
    """
    import datetime
    today = datetime.datetime.now(kit.cm.BJT).strftime("%m-%d")
    today_full = datetime.datetime.now(kit.cm.BJT).strftime("%Y-%m-%d")

    # 1) 汇总所有信号：台账历史 + 本次 picks（picks 参数最全，覆盖同 key）
    allsig = _load_reco_signals(ctx)
    for p in picks:
        code = str(p.get("code", "")).zfill(6)
        signal = p.get("signal", "")
        if not code or not signal:
            continue
        allsig[(code, signal)] = dict(
            code=code, name=p.get("name", ""), signal=signal,
            buy=p.get("buy"), vol_mult=p.get("vol_mult"),
            decline=p.get("decline"), cv=p.get("cv"),
            pattern=p.get("pattern", "platform"),
            hold_days=p.get("hold_days"),
            tier=p.get("tier", 2), fake_break=p.get("fake_break", False),
            plat_cv=p.get("plat_cv"), d1_baoyin=p.get("d1_baoyin"),
            d0_limit_up=p.get("d0_limit_up"),
        )

    # 2) v6.4铁律：第一段=当天新提示，signal必须严格等于系统当日(today_full)。
    #    绝不用max(台账signal)冒充当天——否则流水线失败当天会把昨日信号重推。
    #    第二段历史跟踪的终点=样本K线真实末日(latest_date)，与系统当日可能不同(周末/停牌)。
    latest_signal = today_full
    latest_date = today_full
    for (code, _), sig in allsig.items():
        b = kit.cm.load_kline(code, offline=True)
        if b:
            latest_date = str(b[-1]["date"])
            break

    # 3) 逐信号跟踪并分流：破位/缩量一律剔除，不进任何段落
    seg1, seg2 = [], []
    dropped = []
    for (code, sig_date), sig in allsig.items():
        if sig.get("buy") is None:
            continue
        bars = kit.cm.load_kline(code, offline=True)
        if not bars:
            continue
        st = _track_signal(sig, bars, cfg, latest_date)
        if not st:
            continue
        item = dict(sig)
        item.update(st)
        # ★破位/缩量 → 直接剔除，不推送
        if st["exit_day"]:
            dropped.append(f"{code} {sig.get('name','')} {st['exit_reason']}({st['exit_day']})")
            continue
        # 观察期满 → 剔除（按各自形态的跟踪周期）
        if st["held"] >= st["pat_hold"]:
            dropped.append(f"{code} {sig.get('name','')} 观察期满{st['held']}日")
            continue
        # 最新提示日那批 → 第一段；更早的历史提示仍在观察 → 第二段
        (seg1 if sig_date == latest_signal else seg2).append(item)

    # 4) 组装推送（只有两段，票名带形态标签 + v6★分级）
    def tag(x):
        base = "⚡加速" if x.get("pattern") == "accel" else "🔵平台"
        return ("★" + base) if int(x.get("tier") or 2) == 1 else base
    # 一级(假破位收复)优先：tier小者在前，同级按放量倍数
    def rank(z):
        return (int(z.get("tier") or 2), -float(z.get("vol_mult") or 0))
    sig_mmdd = latest_signal[5:] if len(latest_signal) >= 10 else today
    n_accel = sum(1 for x in seg1 + seg2 if x.get("pattern") == "accel")
    n_tier1 = sum(1 for x in seg1 if int(x.get("tier") or 2) == 1)
    entry_open = cfg.get("entry_rule", "d3_open") == "d3_open"
    ver = str(cfg.get("_version", "v6.4")).split()[0]
    lines = [f"**怡达{ver} · {today}提示** ｜ 样本{len(universe)} ｜ D2当天实时预判", ""]

    lines.append(f"【当天提示 {sig_mmdd} · {len(seg1)}只（★假破位{n_tier1}）】")
    if not seg1:
        lines.append("当日无新提示")
    # 干净版：个股只列代码/名称/标签，策略与买点统一放脚注说明一次
    for x in sorted(seg1, key=rank):
        lines.append(f"`{x['code']}` {x['name']} {tag(x)}")

    lines += ["", f"【二、历史跟踪·持有中 {len(seg2)}只（未出局）】"]
    if not seg2:
        lines.append("无")
    for x in sorted(seg2, key=lambda z: (z["signal"], int(z.get("tier") or 2)), reverse=True):
        flag = "✓" if x["ret"] >= cfg["target"] * 100 else ""
        lines.append(f"`{x['code']}` {x['name']} {tag(x)} {x['signal'][5:]}提示 买{x['buy']} "
                     f"观察{x['held']}/{x['pat_hold']}日 {x['ret']:+.1f}%{flag}")

    foot = ("★假破位收复优先 ｜ 跟3日,三日内收盘破D1阳线实体一半即出局" if not entry_open
            else "📌 D2当天盘后提示,D3开盘价介入(T+1次日可卖) ｜ 跟3日,三日内收盘跌破D1阳线实体一半即出局 ｜ 最高价口径")
    lines += ["", foot, "⚠️非投资建议"]
    title = f"怡达{ver} {today} · 新提示{len(seg1)}(★{n_tier1}) 跟踪中{len(seg2)}"
    body = "\n".join(lines)
    print(f"[yida] ===PUSH_BODY_START===\n{title}\n{body}\n===PUSH_BODY_END===", flush=True)
    return common_notify.send_alert(title, body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-push", action="store_true", help="只选股落台账，不推送")
    args = ap.parse_args()

    cfg = load_cfg()
    ctx = kit.ModelContext("yida")
    print("[yida] 读取前置流水线样本池（common/state/universe.json）...", flush=True)
    universe = ctx.universe()
    print(f"[yida] 样本池 {len(universe)} 只", flush=True)

    picks = []
    for x in universe:
        code = str(x.get("code", "")).zfill(6)
        bars = ctx.kline(code, count=cfg["lookback"] + cfg["base_win"] + cfg["hold_days"] + 30)
        if not bars:
            continue
        sig = detect(bars, cfg, code=code)
        if not sig:
            continue
        sig["code"] = code
        sig["name"] = x.get("name", "")
        sig["score"] = sig["vol_mult"]
        sig["close"] = sig["buy"]
        sig["board"] = x.get("board", "")
        picks.append(sig)

    n_t1 = sum(1 for p in picks if int(p.get("tier") or 2) == 1)
    print(f"[yida-v6] 识别信号 {len(picks)} 个（平台{sum(1 for p in picks if p.get('pattern')=='platform')} "
          f"/ 加速{sum(1 for p in picks if p.get('pattern')=='accel')} / ★假破位{n_t1}）", flush=True)
    for p in sorted(picks, key=lambda z: (int(z.get("tier") or 2), -float(z.get("vol_mult") or 0))):
        tag = "★⚡加速" if p.get("pattern") == "accel" and int(p.get("tier") or 2) == 1 else (
            "★🔵平台" if int(p.get("tier") or 2) == 1 else (
                "⚡加速" if p.get("pattern") == "accel" else "🔵平台"))
        print(f"  {p['code']} {p['name']} {tag} 提示日{p['signal']} "
              f"阴跌{p['decline']}% 放量{p['vol_mult']}x 平台CV{p.get('plat_cv')}% "
              f"D0涨停{'是' if p.get('d0_limit_up') else '否'}/阳包阴{'是' if p.get('d1_baoyin') else '否'}", flush=True)

    # 台账（自动进入统一回溯）
    ctx.record(picks)
    print("[yida] 台账已写入", flush=True)

    # 回溯结算
    try:
        ctx.review(save=True)
    except Exception as e:
        print(f"[yida] 回溯结算跳过：{e}", flush=True)

    # 推送（两段式：第一段=本次提示日新票；第二段=历史入选跟踪到今日）
    if not args.no_push and (picks or _has_history(ctx)):
        ok, msg = _push_two_section(ctx, picks, universe, cfg)
        print(f"[yida] 推送：{'成功' if ok else '失败'} {msg}", flush=True)


if __name__ == "__main__":
    main()
