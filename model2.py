# -*- coding: utf-8 -*-
"""
主升浪买点模型 v2 —— 试盘 + 时间窗口 + 形态确认
=================================================
相对 v1 的核心改进：v1 只在"已经放量涨停突破"后才报警，此时已买不进；
v2 在涨停前 1~5 个交易日的地量埋伏点报警。

样本实证（交易日间距）：
  华瓷股份 sz001216
    2026-08-11 试盘（高开放量，留 6.9% 长上影） → 2026-09-15 涨停启动   间隔 25 日
    2025-07-31 试盘（8.9 倍量，-10.2%）          → 2025-11-05 启动       间隔 63 日
  吉大正元 sz003029
    2026-09-10 试盘（5.8 倍量，盘中冲 19.23 回落）→ 2026-10-15 涨停启动   间隔 19 日
    2026-08-06 试盘（涨停，2.99 倍量）           → 2026-10-15 涨停启动   间隔 44 日
  ⇒ 试盘后 21 / 42 / 63 个交易日（± 3~5）为启动高发窗口
"""
import json, math

# ── 参数 ───────────────────────────────────────────────
P = dict(
    # 试盘识别
    probe_vr=2.2,        # 当日量 / 前20日均量
    probe_tr=2.5,        # 当日换手率 %
    probe_hi=0.98,       # 当日最高 / 前20日最高
    probe_maxgap=110,    # 只回溯最近 110 个交易日的试盘
    # 时间窗口（交易日）
    harm=(21, 42, 63),   # 1 / 2 / 3 个月
    tol=(3, 8),          # ±3 满分，±8 衰减到 0
    # 形态
    converge_max=5.0,    # 均线粘合上限 %
    chg20_abs_max=9.0,   # 近20日涨幅绝对值上限 %
    box30_max=26.0,      # 近30日振幅上限 %
    pos_lo=25, pos_hi=85,
    quiet_max=1.25,      # 近5日均量/近20日均量 上限（要求缩量）
    low_lift_min=0.97,
    # 市值
    mv_lo=25e8, mv_hi=140e8,
)


def avg(a):
    return sum(a) / len(a) if a else 0.0


def board_name(code):
    """股票代码 → 板块名"""
    c = (code or "").lower()
    if c.startswith("sz30"):
        return "创业板"
    if c.startswith("sh688") or c.startswith("sh689"):
        return "科创板"
    if c.startswith("bj") or c.startswith("sh83") or c.startswith("sz83") or c.startswith("sz43"):
        return "北交所"
    if c.startswith("sz00"):
        return "深主板"
    if c.startswith("sh60"):
        return "沪主板"
    return "其他"


def board_limit(code):
    """按板块返回涨停判定阈值（%）：主板10、创业板/科创板20、北交所30。
    创业板/科创板涨 10~19% 并不是涨停，用统一 9.5% 判定会把它们误记成涨停。"""
    c = (code or "").lower()
    if c.startswith("sz30") or c.startswith("sh688") or c.startswith("sh689"):
        return 19.5
    if c.startswith("bj") or c.startswith("sh83") or c.startswith("sz83") or c.startswith("sz43"):
        return 29.5
    return 9.5


def find_probes(bars, upto=None):
    """找出试盘日。bars 按时间【正序】排列，index 越大越新。
    返回 [{i,date,vr,tr,shadow,chg,close}]"""
    n = min(upto if upto is not None else len(bars), len(bars))
    out = []
    for i in range(25, n):
        prev = bars[i - 20:i]
        v20 = avg([x["volume"] for x in prev])
        hi20 = max(x["high"] for x in prev)
        vi = bars[i]["volume"]; hi = bars[i]["high"]
        curr = bars[i]["last"]; exi = float(bars[i]["exchange"])
        prevclose = bars[i - 1]["last"]
        vr = vi / v20 if v20 else 0
        if vr < P["probe_vr"] or exi < P["probe_tr"]:
            continue
        if hi < hi20 * P["probe_hi"]:
            continue
        # 必须是"试探后回落"，不能是已经走出来的主升浪
        if i + 6 < len(bars):
            after = max(x["high"] for x in bars[i + 1:i + 7])
            if after > hi * 1.06:
                continue   # 随后继续大涨 → 不是试盘，是已经在拉升
        out.append(dict(i=i, date=bars[i]["date"], vr=round(vr, 2), tr=exi,
                        shadow=round((hi - curr) / hi * 100, 2),
                        chg=round((curr / prevclose - 1) * 100, 2), close=curr))
    return out


def probe_gap_score(gap, probes_upto=None):
    """给定距某次试盘的交易日数，返回 0~1 的时间窗口分"""
    best = 0.0
    for target in P["harm"]:
        d = abs(gap - target)
        if d <= P["tol"][0]:
            s = 1.0
        elif d <= P["tol"][1]:
            s = 1.0 - (d - P["tol"][0]) / (P["tol"][1] - P["tol"][0])
        else:
            s = 0.0
        best = max(best, s)
    return best


def feats(bars, i):
    """截至第 i 根（含）的形态指标。bars 时间正序。
    内部转成"新→旧"的窗口便于按 :n 切片取最近 n 根。"""
    if i < 65:
        return None
    seg = list(reversed(bars[max(0, i - 119):i + 1]))
    c = [x["last"] for x in seg]; h = [x["high"] for x in seg]
    l = [x["low"] for x in seg]; v = [x["volume"] for x in seg]
    ex = [float(x["exchange"]) for x in seg]
    if len(c) < 60:
        return None
    hi60, lo60 = max(h[:60]), min(l[:60])
    hi30, lo30 = max(h[:30]), min(l[:30])
    box30 = (hi30 - lo30) / lo30 * 100
    pos = (c[0] - lo60) / (hi60 - lo60) * 100 if hi60 > lo60 else 50
    def ma(k): return sum(c[:k]) / k
    mas = [ma(5), ma(10), ma(20), ma(30)]
    conv = (max(mas) - min(mas)) / min(mas) * 100
    v5, v20 = sum(v[:5]) / 5, sum(v[5:25]) / 20
    vr5 = v5 / v20 if v20 else 9
    day_vr = v[0] / (sum(v[1:6]) / 5) if len(v) > 5 else 0
    s = c[19::-1]; nn = len(s); xs = list(range(nn))
    mx, my = avg(xs), avg(s)
    slope = (sum((x - mx) * (y - my) for x, y in zip(xs, s)) /
             sum((x - mx) ** 2 for x in xs)) / my * 100 if my else 0
    low_lift = min(l[:10]) / min(l[:30]) if min(l[:30]) else 1
    return dict(date=seg[0]["date"], close=c[0],
                converge=conv, box30=box30, pos=pos, slope=slope,
                vr5=vr5, day_vr=day_vr, low_lift=low_lift,
                tr20=avg(ex[:20]), tr5=avg(ex[:5]),
                chg20=(c[0] / c[20] - 1) * 100,
                chg5=(c[0] / c[5] - 1) * 100,
                day_chg=(c[0] / c[1] - 1) * 100,
                hi30=hi30, lo30=lo30,
                # 是否在试盘高点之下（未被爆炒）
                ret60=(c[0] / c[59] - 1) * 100)


def rank(bars, i, max_lookback=120):
    """截止第 i 根，给出综合买点分 0~100 与分项"""
    f = feats(bars, i)
    if not f:
        return None
    probes = find_probes(bars, upto=i + 1)
    probes = [p for p in probes if i - p["i"] <= P["probe_maxgap"]]
    # 时间窗口：取所有试盘中最近一次进入窗口且分数最高的
    tinfo = []
    for p in probes:
        gap = i - p["i"]
        s = probe_gap_score(gap)
        if s > 0:
            tinfo.append(dict(date=p["date"], gap=gap, score=s, vr=p["vr"],
                              tr=p["tr"], shadow=p["shadow"], chg=p["chg"]))
    tinfo.sort(key=lambda x: (-x["score"], -x["gap"]))
    twindow = max([t["score"] for t in tinfo], default=0.0)

    # ── 分项打分 ──
    s_time = 30 * twindow                                            # 时间窗口 30
    s_conv = 16 * max(0, 1 - f["converge"] / P["converge_max"])      # 均线粘合 16
    s_box = 10 * max(0, 1 - f["box30"] / P["box30_max"])             # 窄箱体   10
    s_chg = 10 * max(0, 1 - abs(f["chg20"]) / P["chg20_abs_max"])    # 滞涨     10
    s_quiet = 14 * max(0, 1 - max(0, f["vr5"] - 0.75) / 0.75)        # 缩量     14
    s_pos = 10 * max(0, 1 - abs(f["pos"] - 60) / 40)                 # 位置     10
    s_lift = 6 * min(1, max(0, (f["low_lift"] - 0.95) / 0.05))       # 底部抬升  6
    s_slope = 4 * max(0, 1 - abs(f["slope"] - 0.05) / 0.4)           # 斜率      4
    total = s_time + s_conv + s_box + s_chg + s_quiet + s_pos + s_lift + s_slope
    # 硬门槛：无量涨停 / 已经拉升过的不给推荐
    if f["day_chg"] >= 9.5 or f["chg5"] > 12 or f["vr5"] > 1.6:
        total *= 0.55      # 已启动，追高风险 → 大幅降权（埋伏模型不买已涨的）
    S = dict(conv=s_conv, box=s_box, chg=s_chg, quiet=s_quiet, lowl=s_lift, slp=s_slope)
    return dict(score=round(total, 1), time=round(s_time, 1),
                **{k: round(v, 1) for k, v in S.items()},
                twindow=round(twindow, 2), tpicks=tinfo[:3], **f)


def find_rallies(bars, min_gain=25, win=10):
    """找出所有主升浪：某日放量涨>=5%，之后 win 日内最高价较其收盘涨>=min_gain%"""
    n = len(bars)
    c = [x["last"] for x in bars]; h = [x["high"] for x in bars]
    v = [x["volume"] for x in bars]
    out = []
    for i in range(30, n - 3):
        win_u = min(win, n - 1 - i)          # 末段数据不足时缩短观察窗
        if win_u < 5:
            break
        v20 = avg(v[i - 20:i])
        vr = v[i] / v20 if v20 else 0
        chg = (c[i] / c[i - 1] - 1) * 100
        peak = max(h[i:i + win_u + 1])
        gain = (peak / c[i] - 1) * 100
        if vr >= 1.8 and chg >= 5.0 and gain >= min_gain:
            out.append(dict(i=i, date=bars[i]["date"], vr=vr, chg=chg, gain=gain))
    ded = []
    for r in out:
        if ded and r["i"] - ded[-1]["i"] <= 5:
            continue
        ded.append(r)
    return ded
