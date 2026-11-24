# -*- coding: utf-8 -*-
"""统一回溯口径模块 v1.0 —— 所有选股模型共用一把尺子。

口径（用户 2026-11-24 确立，详见 /workspace/回溯口径统一规范_2026-11-24.md）：
  - 提示日 D0 = 模型发出信号的交易日；所有"天"按交易日计（跳过周末/节假日，停牌顺延）。
  - 买入：D+1（第二天）**开盘价** P_buy。
  - 卖出（均为当日**最高价**）：
        1 -> D+1（第二天）；3 -> D+3（第四天）；5 -> D+5（第六天）；10 -> D+10（第十一天）。
  - 收益率 R_n = (H_(D+n) - P_buy) / P_buy * 100%。
  - 胜率：R_n > 0 的样本占比；收益率：R_n 的算术平均。
  - 复权：前复权(qfq)，避免除权跳空假跌。
  - 边界：T+1 一字涨停买不进按理想化仍计入（与"最高价卖出"口径一致）；
          凑不齐 D+n 的样本该周期剔除；数据按交易日对齐。

数据格式：bars = [{date:'YYYY-MM-DD', open, high, low, close, vol, amount}, ...] 按日期升序。
本模块为纯计算 + 可选取数，无模型耦合，任何模型均可 import。
"""
from __future__ import annotations

from datetime import datetime

HORIZONS = (1, 3, 5, 10)


def _date_key(b):
    d = b.get("date")
    return str(d)[:10]


def _find_d0_index(bars, signal_date):
    """定位提示日 D0 在 bars 中的下标。signal_date 允许 YYYY-MM-DD / YYYYMMDD。"""
    sd = str(signal_date).replace("-", "")[:8]
    for i, b in enumerate(bars):
        if _date_key(b).replace("-", "")[:8] == sd:
            return i
    return None


def evaluate_signal(signal_date, code, bars, horizons=HORIZONS):
    """对单个信号按统一口径回溯。

    返回 dict：{'code','signal_date','buy_date','buy_price',
                'r1','r3','r5','r10', 'sell_date_1',...} 或 None（信号日不在数据/数据不足）。
    """
    if not bars:
        return None
    i0 = _find_d0_index(bars, signal_date)
    if i0 is None or i0 + 1 >= len(bars):
        return None
    buy = bars[i0 + 1]                      # D+1
    p_buy = float(buy.get("open") or 0)
    if p_buy <= 0:
        return None
    out = {
        "code": str(code),
        "signal_date": _date_key(bars[i0]),
        "buy_date": _date_key(buy),
        "buy_price": round(p_buy, 4),
    }
    for n in horizons:
        j = i0 + n                          # D+n
        if j >= len(bars):
            out["r%d" % n] = None           # 数据不足，该周期剔除
            out["sell_date_%d" % n] = None
            continue
        sell = bars[j]
        h = float(sell.get("high") or 0)
        if h <= 0:
            out["r%d" % n] = None
            out["sell_date_%d" % n] = None
            continue
        out["r%d" % n] = round((h - p_buy) / p_buy * 100.0, 4)
        out["sell_date_%d" % n] = _date_key(sell)
    return out


def summarize(records, horizons=HORIZONS):
    """批量统计：各周期胜率(R>0)、平均收益、样本数。

    records: evaluate_signal 返回的 dict 列表。
    返回 {'1':{'n','win_rate','avg_ret','wins'}, ...}
    """
    summary = {}
    for n in horizons:
        key = "r%d" % n
        vals = [r[key] for r in records if r.get(key) is not None]
        n_valid = len(vals)
        if n_valid == 0:
            summary[str(n)] = {"n": 0, "wins": 0, "win_rate": None, "avg_ret": None}
            continue
        wins = sum(1 for v in vals if v > 0)
        summary[str(n)] = {
            "n": n_valid,
            "wins": wins,
            "win_rate": round(wins / n_valid * 100.0, 2),
            "avg_ret": round(sum(vals) / n_valid, 4),
        }
    return summary


def load_bars_qfq(code, start="20180101", end=None):
    """取前复权日线；优先共享行情表，缺失时用 akshare(qfq) 兜底。

    code 形如 '300030' 或 'sz300030'。
    """
    end = end or datetime.now().strftime("%Y%m%d")
    raw = str(code)
    pure = raw[-6:] if len(raw) > 6 else raw
    sym = ("sh" if pure.startswith("6") else "sz") + pure

    # 1) 共享行情表
    try:
        import os
        import sys
        repo_root = os.environ.get("REPO_ROOT") or os.getcwd()
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        from common.market_store import MarketStore
        store = MarketStore()
        bars = store.load_bars(pure, start=start[:4] + "-" + start[4:6] + "-" + start[6:],
                               end=end[:4] + "-" + end[4:6] + "-" + end[6:])
        if bars:
            return bars
    except Exception:
        pass

    # 2) akshare 前复权兜底
    import akshare as ak
    df = ak.stock_zh_a_daily(symbol=sym, start_date=start, end_date=end, adjust="qfq")
    out = []
    for r in df.itertuples():
        out.append({
            "date": str(r.date)[:10],
            "open": float(r.open), "high": float(r.high),
            "low": float(r.low), "close": float(r.close),
            "vol": float(getattr(r, "volume", 0) or 0),
            "amount": 0.0,
        })
    return out


if __name__ == "__main__":
    # 自测：阳普医疗 2026-10-29 提示日
    bars = load_bars_qfq("300030", start="20260101")
    r = evaluate_signal("2026-10-29", "300030", bars)
    print("单信号回溯:", r)
    print("汇总:", summarize([r] if r else []))
