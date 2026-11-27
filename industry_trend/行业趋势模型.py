# -*- coding: utf-8 -*-
"""
行业趋势模型（固化版 · 共享行情表适配）
=====================================
用途：作为"二次确认过滤器"，不独立选股。
      对上游模型（怡达/破位/奥雅）已选出的股票池做技术确认，
      命中者标记为【重点样本】提示给用户。

信号口径（来自 2026-11-27 确认）：
  1. 启动窗口：月末最后 N 个交易日（默认 N=5，按交易日）
  2. 均线金叉：MA5 上穿 MA10（金叉发生在窗口内，或窗口末仍保持 MA5>MA10）
  3. 均线拐头：MA20 向上（当前 MA20 > 3 日前 MA20，即 MA20 斜率>0）
  4. 辅助因子：价格站上 MA20、量能放大（量比>1.2）、站上 MA60

分级：
  - 重点样本：金叉确认 + MA20拐头 + 站上MA20  （三条件全满足）
  - 观察样本：金叉确认 但 MA20未拐头（反弹初期，等确认）
  - 稳健样本：MA20拐头 + 站上MA60（趋势型，弹性较低）

调用方式：
  from 行业趋势模型 import confirm, batch_confirm
  result = confirm("600839")            # 单只确认（个股）
  results = batch_confirm(["600839","603985"])  # 批量确认

数据源（2026-11-27 改造）：
  - 由原 akshare 直连东财 + hfq 后复权，改为读取本仓库共享行情表
    common/state/market_daily（qfq 前复权），与全部模型统一口径；
  - read-only，不访问外网；上游选股均为 A 股个股，共享表 100% 覆盖。
  - 注：ETF（如 515880）不在共享行情表内；本模型定位是给个股做确认，
    如需确认 ETF 请另行接入 ETF 行情，勿用本模块。

依赖：pandas / numpy（共享行情表由 common/common_lib.py 提供，无需 akshare）
"""

import os
import sys

import pandas as pd
import numpy as np

# ============ 模型参数（可在此调整）============
WINDOW_DAYS = 5          # 月末确认窗口（交易日）
MA20_LOOKBACK = 3        # MA20 拐头判定回看天数
VOL_RATIO_MIN = 1.2      # 放量阈值（量比）
GOLDEN_MAX_AGE = 5       # 金叉有效天数（距今天数）

# ============ 共享行情表接口（只读本地，不联网）============
def _load_kline():
    """定位并加载 common.common_lib.load_kline。兼容被主流水线 import 与独立运行。"""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "..", "common"),   # 本模块放在 <repo>/行业趋势模型/ 下
        os.path.join(here, "common"),         # 本模块放在 <repo>/ 根下
        here,                                 # 本模块已被放入 common/ 内
    ]
    for c in candidates:
        c = os.path.abspath(c)
        if os.path.isfile(os.path.join(c, "common_lib.py")) and c not in sys.path:
            sys.path.insert(0, c)
    try:
        from common_lib import load_kline
        return load_kline
    except Exception as e:
        raise RuntimeError("无法加载共享行情表 common_lib.load_kline：%s" % e)


def _fetch(code, end_date=None, count=210):
    """从共享行情表读取个股日线（qfq 前复权），返回标准化 DataFrame 或 (None, err)。

    保持与原版一致的函数签名风格，供 confirm() 复用。
    """
    try:
        load_kline = _load_kline()
    except Exception as e:
        return None, str(e)
    # bars 为 dict 列表：{code,date,open,high,low,close,volume,turnover}
    bars = load_kline(str(code), count=count, end=end_date, offline=True)
    if not bars or len(bars) < 25:
        return None, "共享行情表数据不足或无此代码: %s" % code
    df = pd.DataFrame(bars)
    # 统一字段（兼容个别 bar 用 day/last/vol 别名）
    if "日期" not in df.columns:
        src = "date" if "date" in df.columns else ("day" if "day" in df.columns else None)
        df["日期"] = pd.to_datetime(df[src]) if src else pd.NaT
    else:
        df["日期"] = pd.to_datetime(df["日期"])
    if "收盘" not in df.columns:
        src = "close" if "close" in df.columns else ("last" if "last" in df.columns else None)
        df["收盘"] = pd.to_numeric(df[src], errors="coerce") if src else np.nan
    else:
        df["收盘"] = pd.to_numeric(df["收盘"], errors="coerce")
    if "成交量" not in df.columns:
        src = "volume" if "volume" in df.columns else ("vol" if "vol" in df.columns else None)
        df["成交量"] = pd.to_numeric(df[src], errors="coerce") if src else np.nan
    else:
        df["成交量"] = pd.to_numeric(df["成交量"], errors="coerce")
    # 涨跌幅：共享表无该字段，用相邻收盘价计算（与 qfq 口径一致）
    df["涨跌幅"] = df["收盘"].pct_change() * 100
    df = df.sort_values("日期").reset_index(drop=True)
    return df, None


def confirm(code, name="", end_date=None):
    """
    对单只标的做模型确认。
    返回 dict：命中等级、信号明细、建议。
    """
    df, err = _fetch(code, end_date)
    if err:
        return {"代码": code, "名称": name, "等级": "数据异常", "金叉": False,
                "金叉距今天数": 999, "MA5>MA10": False, "MA20拐头": False,
                "MA20斜率%": 0, "站上MA20": False, "站上MA60": False,
                "量比": 0, "近5日%": 0, "当日涨跌%": 0, "收盘": 0,
                "说明": err, "建议": "跳过", "基准日": ""}

    c = df["收盘"]
    df["MA5"] = c.rolling(5).mean()
    df["MA10"] = c.rolling(10).mean()
    df["MA20"] = c.rolling(20).mean()
    df["MA60"] = c.rolling(60).mean()
    df["VMA20"] = df["成交量"].rolling(20).mean()

    last = df.iloc[-1]
    # 金叉：窗口内 MA5 上穿 MA10
    cross_days = []
    for i in range(-WINDOW_DAYS, 0):
        if (df["MA5"].iloc[i] > df["MA10"].iloc[i]) and (df["MA5"].iloc[i-1] <= df["MA10"].iloc[i-1]):
            cross_days.append(i)
    golden = len(cross_days) > 0
    golden_age = abs(cross_days[-1]) if cross_days else 999
    ma_bull = last["MA5"] > last["MA10"]

    # MA20 拐头
    ma20_slope = (last["MA20"] / df["MA20"].iloc[-1-MA20_LOOKBACK] - 1) * 100
    ma20_up = ma20_slope > 0
    above20 = last["收盘"] > last["MA20"]
    above60 = (last["收盘"] > last["MA60"]) if pd.notna(last["MA60"]) else False

    vol_ratio = last["成交量"] / last["VMA20"] if last["VMA20"] > 0 else 0
    ret5 = (last["收盘"] / df["收盘"].iloc[-6] - 1) * 100

    # 分级判定
    if golden and ma20_up and above20:
        level, desc, action = "重点样本", "金叉+MA20拐头+站上MA20，三条件共振", "重点提示：可关注介入时机"
    elif ma20_up and above60 and ma_bull:
        level, desc, action = "重点样本", "多头排列+MA20拐头+站上MA60，趋势稳健", "重点提示：趋势型机会"
    elif golden and not ma20_up:
        level, desc, action = "观察样本", "金叉已成立但MA20仍下行，反弹初期", "观察：等MA20拐头确认"
    elif ma20_up and above20:
        level, desc, action = "观察样本", "MA20拐头但未金叉，蓄势中", "观察：等金叉确认"
    else:
        level, desc, action = "未命中", "技术信号未达确认标准", "不提示"

    return {
        "代码": code, "名称": name,
        "等级": level,
        "当日涨跌%": round(float(last["涨跌幅"]) if pd.notna(last["涨跌幅"]) else 0, 2),
        "收盘": round(float(last["收盘"]), 3),
        "金叉": golden, "金叉距今天数": golden_age, "MA5>MA10": bool(ma_bull),
        "MA20斜率%": round(float(ma20_slope), 2), "MA20拐头": bool(ma20_up),
        "站上MA20": bool(above20), "站上MA60": bool(above60),
        "量比": round(float(vol_ratio), 2), "近5日%": round(float(ret5), 1),
        "说明": desc, "建议": action,
        "基准日": str(last["日期"].date()),
    }


def batch_confirm(codes, names=None, end_date=None):
    """批量确认。codes: 代码列表；names: 可选名称映射 dict"""
    out = []
    for code in codes:
        nm = (names or {}).get(code, "")
        out.append(confirm(code, nm, end_date))
    return out


def filter_focus(codes, names=None, end_date=None):
    """
    供微信推送调用的精简接口：只返回【重点样本】。
    """
    res = batch_confirm(codes, names, end_date)
    focus = [r for r in res if r["等级"] == "重点样本"]
    return focus


def format_push(focus_list, source_model=""):
    """把重点样本格式化成微信推送文本片段"""
    if not focus_list:
        return ""
    lines = [f"⭐【重点样本 · 行业趋势模型确认】"]
    if source_model:
        lines.append(f"来源：{source_model} 选出且通过本模型确认")
    for r in focus_list:
        lines.append(
            f"• {r['名称']}({r['代码']}) {r['收盘']} "
            f"涨跌{r['当日涨跌%']:+.1f}% | {r['说明']}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    codes = sys.argv[1:] or ["600839", "603985", "600186", "600410", "002261"]
    print("=" * 70)
    print("行业趋势模型 — 确认结果（共享行情表 qfq）")
    print("=" * 70)
    for r in batch_confirm(codes):
        print(f"\n{r['代码']} {r['名称']} → 【{r['等级']}】")
        print(f"  金叉={r['金叉']}({r['金叉距今天数']}天前) MA20拐头={r['MA20拐头']}(斜率{r['MA20斜率%']:+.2f}%) "
              f"站上MA20={r['站上MA20']} 站上MA60={r['站上MA60']} 量比={r['量比']}")
        print(f"  {r['建议']}")
