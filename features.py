# -*- coding: utf-8 -*-
"""
实盘因子计算 —— 与回测 diagnose2.py 完全同源的公式
bars：按时间【正序】排列的原始K线（含 open/last/high/low/volume/amount/exchange/date）
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import model2 as M


def board_limit(code):
    """涨停判定阈值，统一走 model2.board_limit，避免回测与实盘两套标准"""
    return M.board_limit(code)


def factors_at(bars, i, code=None):
    """返回第 i 根（含）当日的全部因子 dict；i 超出范围返回 None"""
    n = len(bars)
    if i < 150 or n < 160:
        return None
    c = [x["last"] for x in bars]; h = [x["high"] for x in bars]
    l = [x["low"] for x in bars]; v = [x["volume"] for x in bars]
    o = [x["open"] for x in bars]
    ex = [float(x["exchange"]) for x in bars]
    f = M.feats(bars, i)
    if not f:
        return None
    w60 = bars[max(0, i - 59):i + 1]
    w120 = bars[max(0, i - 119):i + 1]
    w250 = bars[max(0, i - 249):i + 1]
    probes = M.find_probes(bars, upto=n)
    lastprobe = max([p["i"] for p in probes if p["i"] <= i], default=-999)
    gap = i - lastprobe if lastprobe > 0 else 999
    nprobe120 = sum(1 for p in probes if i - p["i"] <= 120 and p["i"] <= i)

    LU = board_limit(code)

    def nlu(seg):
        return sum(1 for j in range(1, len(seg))
                   if (seg[j]["last"] / seg[j - 1]["last"] - 1) * 100 >= LU)

    hi20_prev = max(h[i - 20:i]); hi60_prev = max(h[i - 60:i])
    days_since_hi = 99
    for j in range(i, max(0, i - 60), -1):
        pv = max(h[j - 20:j]) if j >= 20 else h[j]
        if h[j] >= pv:
            days_since_hi = i - j
            break
    up20 = sum(1 for j in range(i - 19, i + 1) if c[j] > o[j]) / 20
    lu10 = nlu(w60[-10:]); lu20 = nlu(w60[-20:])
    v5 = sum(v[i - 4:i + 1]) / 5; v20 = sum(v[i - 19:i + 1]) / 20
    v60 = sum(v[i - 59:i + 1]) / 60
    shrink = 0.0
    for j in range(i, max(0, i - 10), -1):
        if (c[j] / c[j - 1] - 1) * 100 >= LU:
            shrink = v5 / v[j] if v[j] else 0
            break
    body20 = sum(abs(c[j] - o[j]) / c[j - 1] * 100 for j in range(i - 19, i + 1)) / 20
    ma5 = sum(c[i - 4:i + 1]) / 5; ma20 = sum(c[i - 19:i + 1]) / 20
    ma60 = sum(c[i - 59:i + 1]) / 60
    hi60 = max(x["high"] for x in w60); lo60 = min(x["low"] for x in w60)
    probes60 = [p for p in probes if 0 <= i - p["i"] <= 90]
    # ── 「试盘→回踩60日线→十字星企稳」形态字段（2026-10-30 全市场检验判无效，
    #    仅作展示标签、不参与排序。公式与 pattern_test.py 完全一致）──
    rng_i = h[i] - l[i]
    v20_excl = sum(v[i - 20:i]) / 20          # 前20日均量（不含当日）
    # 距最近一次「放量突破」（当日涨≥5% 且量>2×前20日均量）的天数，无则 99。
    # 2026-11-02 在 17.3 万样本上检验：无条件看新鲜度信号很强
    # （当日 15.79% / 1-3日 12.17% / >20日 7.01%，g10≥20% 占比；同日配对差 +2.16pp，88% 天数占优），
    # 但在模型已选出的 TopN 内并不单调（>20日那档 16.85% 反而最高，4-5日 9.45% 最差），
    # 加"dsc>5 打折"实测是负作用（14.60%→14.08%），故【只做展示、不入权重】。
    # 2026-11-04 修正语义：用户指出「涨停2天后首推却标『启动当日』」误导。
    # 旧逻辑从最新K线往回找【最近一次】放量突破即停，一旦启动后又出现放量大阳，
    # 起涨日会被"重置"。现改为：先在近10个交易日内找【最早】的一次放量突破
    # （= 本波起涨日），找不到再退回旧的"最近一次"逻辑（窗口放宽到60日）。
    # 同时给出 launch_gain = 现价相对起涨日前收盘的累计涨幅，供推送端做追高提示。
    dsc = 99
    launch_pre_close = None
    for _j in range(i, max(0, i - 10) - 1, -1):     # 近10日内的最早一次放量突破
        _v20 = sum(v[_j - 20:_j]) / 20
        if _v20 and (c[_j] / c[_j - 1] - 1) * 100 >= 5 and v[_j] > 2 * _v20:
            dsc = i - _j
            launch_pre_close = c[_j - 1]            # 不 break：继续往前找更早的
    if dsc == 99:                                    # 近10日无突破 → 退回旧口径
        for _j in range(i, max(30, i - 60), -1):
            _v20 = sum(v[_j - 20:_j]) / 20
            if _v20 and (c[_j] / c[_j - 1] - 1) * 100 >= 5 and v[_j] > 2 * _v20:
                dsc = i - _j
                break
    return dict(date=bars[i]["date"], close=c[i], board=M.board_name(code), lu_th=LU,
                tr5=round(sum(ex[i - 4:i + 1]) / 5, 2),
                tr20=round(sum(ex[i - 19:i + 1]) / 20, 2),
                amp20=round(sum((w60[j]["high"] - w60[j]["low"]) / w60[j - 1]["last"] * 100
                                for j in range(1, 21)) / 20, 2),
                body20=round(body20, 2),
                maxchg60=round(max((w60[j]["last"] / w60[j - 1]["last"] - 1) * 100
                                   for j in range(1, len(w60))), 1),
                nlu250=nlu(w250), nlu120=nlu(w120), lu10=lu10, lu20=lu20,
                chg5=round((c[i] / c[i - 5] - 1) * 100, 1),
                chg10=round((c[i] / c[i - 10] - 1) * 100, 1),
                chg20=round((c[i] / c[i - 20] - 1) * 100, 1),
                chg60=round((c[i] / c[i - 60] - 1) * 100, 1),
                dist_lo=round((c[i] / lo60 - 1) * 100, 1),
                dist_hi=round((c[i] / hi60 - 1) * 100, 1),
                pos=round(f["pos"], 1), box30=round(f["box30"], 1),
                brk20=round((c[i] / hi20_prev - 1) * 100, 2),
                brk60=round((c[i] / hi60_prev - 1) * 100, 2),
                up20=round(up20, 2), vr5=round(v5 / v20, 3), vr20=round(v20 / v60, 3),
                shrink=round(shrink, 3), camel=round((ma5 - ma20) / ma20 * 100, 3),
                ma20up=1 if ma20 > ma60 else 0, hidate=days_since_hi,
                dd20=round((c[i] / hi20_prev - 1) * 100, 2),
                gap=(999 if gap > 200 else gap), nprobe=nprobe120,
                day_chg=round((c[i] / c[i - 1] - 1) * 100, 2),
                day_vr=round(v[i] / v20 if v20 else 0, 2),
                # 最近一次试盘信息
                last_probe_date=(max([p for p in probes if p["i"] <= i],
                                     key=lambda z: z["i"])["date"] if lastprobe > 0 else ""),
                last_probe_vr=(max([p for p in probes if p["i"] <= i],
                                   key=lambda z: z["i"])["vr"] if lastprobe > 0 else 0),
                # 形态展示标签字段（不入权重）
                dev_ma60=round((c[i] / ma60 - 1) * 100, 2) if ma60 else 0,
                ma60_touch=1 if (ma60 and abs(c[i] / ma60 - 1) <= 0.03) else 0,
                doji=1 if (rng_i > 0 and abs(c[i] - o[i]) / rng_i <= 0.15) else 0,
                probe_recent=1 if any(0 < i - p["i"] <= 25 for p in probes) else 0,
                dsc=dsc,
                launch_gain=(round((c[i] / launch_pre_close - 1) * 100, 1)
                             if (launch_pre_close and dsc < 99) else None),
                vol_dry=1 if (v20_excl and v[i] / v20_excl <= 0.8) else 0)


def percentile(vals, x):
    """x 在 vals 序列中的百分位 0~1"""
    if not vals:
        return 0.5
    n = len(vals)
    lo, hi = 0, n
    while lo < hi:
        mid = (lo + hi) // 2
        if vals[mid] < x:
            lo = mid + 1
        else:
            hi = mid
    return lo / max(1, n - 1)
