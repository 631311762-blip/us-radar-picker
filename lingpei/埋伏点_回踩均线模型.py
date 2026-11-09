# -*- coding: utf-8 -*-
"""埋伏点_回踩均线模型（依赖模块）：ST 判定、均线指标。
重建版：按原 prompt 口径推断实现。
"""


def is_st(u):
    """剔除 ST / *ST / 退市 / N 次新。u 可为名字字符串或含 name/code 的 dict。"""
    if isinstance(u, dict):
        name = u.get("name", "") or ""
        code = str(u.get("code", ""))
    else:
        name, code = u, ""
    if not name:
        return False
    n = name.upper()
    if "ST" in n:          # 含 ST / *ST
        return True
    if "退" in name:        # 退市
        return True
    if code and code.startswith("N"):  # 上市 N 日次新
        return True
    return False


def add_indicators(bars):
    """给 bars 附加 ma5/ma10/ma20/ma30/ma60。返回同一列表（原地修改）。"""
    n = len(bars)
    closes = [b["c"] for b in bars]
    for period in (5, 10, 20, 30, 60):
        key = "ma%d" % period
        for i in range(n):
            if i + 1 < period:
                bars[i][key] = None
            else:
                bars[i][key] = sum(closes[i + 1 - period:i + 1]) / period
    return bars
