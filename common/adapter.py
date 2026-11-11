#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
adapter.py —— 让「现有模型」零改核心逻辑地接入共同前置层
========================================================================
背景：各模型原本各自拉全市场列表 / 各自拉 K 线 / 各自判 ST/立信，
      既慢又容易漏条件。共同层已把「ST/立信剔除后的样本池」和「全市场 K 线」
      物理准备好，本模块把这些数据转成各模型「原本期望的字段格式」，
      使模型只需在数据入口函数开头加一小段即可强制吃到共同条件。

用法（每个模型的数据入口函数开头）：
    if adapter:
        _c = adapter.universe_for("shanshui")      # 返回 None 表示共同层未就绪→回退原逻辑
        if _c is not None: return _c

设计要点：
  - 共同层未产出（universe.json 不存在）时一律返回 None，模型自动回退到
    原来的取数逻辑 —— 因此独立运行 / 本地调试 / 旧 workflow 均不受影响。
  - 样本池已物理剔除 ST/立信，模型即使完全没写剔除也不会选到被禁标的，
    实现「即使模型条件里没写，也视为遗漏补上」的强制语义。
  - K 线走共享缓存，避免各模型重复拉全市场（提速）。
"""
import os

try:
    import common_lib as cm
except Exception:  # pragma: no cover
    cm = None


def ready():
    """共同前置层是否已就绪（样本池已产出且是当日交易日产物）。"""
    if cm is None:
        return False
    meta = cm.load_meta()
    if not meta or not meta.get("trading_day"):
        return False
    return os.path.exists(cm.UNIVERSE_JSON)


def universe_for(model):
    """按模型返回其期望格式的样本池；共同层未就绪返回 None。"""
    if not ready():
        return None
    u = cm.load_universe()
    if u is None:
        return None
    m = (model or "").lower()

    if m in ("low_shadow", "low_shadow2"):
        # 原格式: [{symbol, name, pct}]
        return [{"symbol": x["code"], "name": x["name"], "pct": x.get("pct", 0)}
                for x in u]

    if m == "yidian":
        # 原 build_universe 只扫 创业板/科创板/北交所，且返回 (list, meta)
        # 口径对齐：易点 fetch_board 未设 fltt，f2/f3/f8/f10 是放大 100 倍值，
        #   其 build_universe 做 /100 → price=真实价, chg_pct/turn_pct=百分数数值,
        #   vr=真实量比，再与 CHG 0.5~22 / TURN 2~35 / VR>=1.4 比较。
        # 共同层样本池用 fltt=2，pct/turnover/vr 正好已是「百分数数值/真实量比」，
        # 直接传即可（不能再 /100，否则会全部被门槛误杀）。
        boards = ("创业板", "科创板", "北交所")
        rows = []
        cyb = kcb = bj = 0
        for x in u:
            b = x.get("board")
            if b not in boards:
                continue
            rows.append({
                "code": x["code"], "name": x["name"], "board": b,
                "price": x.get("price", 0),
                "chg_pct": x.get("pct", 0),
                "turn_pct": x.get("turnover", 0),
                "vr": x.get("vr", 0),
                "mktcap": x.get("mktcap", 0),
                "float_mktcap": x.get("float_mktcap", 0),
            })
            if b == "创业板":
                cyb += 1
            elif b == "科创板":
                kcb += 1
            else:
                bj += 1
        meta = {"date": cm.load_meta().get("date"), "total": len(rows),
                "cyb_kcb": cyb + kcb, "cyb": cyb, "kcb": kcb, "bj": bj,
                "page_fails": 0, "source": "common_prefilter"}
        return rows, meta

    if m == "kaizhong":
        # 原 refresh_data 用新浪快照 DataFrame：code/symbol/trade/volume/amount/name
        import pandas as pd
        rows = []
        for x in u:
            code = x["code"]
            if x.get("board") == "北交所":      # 凯众原本排除北交所
                continue
            sym = ("sh" if code.startswith("6") else "sz") + code
            rows.append({
                "code": code, "symbol": sym, "name": x["name"],
                "trade": x.get("price", 0),
                "volume": x.get("volume", 0),
                "amount": x.get("amount", 0),
            })
        return pd.DataFrame(rows)

    # shanshui / lingpei: {code,name,board,price,pct,turnover,market_cap_yi,prev_close}
    return [{
        "code": x["code"], "name": x["name"], "board": x.get("board", ""),
        "price": x.get("price", 0), "pct": x.get("pct", 0),
        "turnover": x.get("turnover", 0),
        "market_cap_yi": (x.get("mktcap", 0) or 0) / 1e8,
        "prev_close": x.get("prev_close", 0),
    } for x in u]


def kline_for(code, model, count=None):
    """读共享 K 线缓存并转成模型期望的 bar 格式；未就绪/缺失返回 None。"""
    if cm is None or not os.path.exists(cm.KLINE_DIR):
        return None
    bars = cm.load_kline(code, count=count)
    if not bars:
        return None
    m = (model or "").lower()

    if m in ("shanshui", "lingpei"):
        # {d,o,c,h,l,v}
        return [{"d": b["date"], "o": b["open"], "c": b["close"],
                 "h": b["high"], "l": b["low"], "v": b["volume"]} for b in bars]

    if m in ("low_shadow", "low_shadow2"):
        # {date,open,close,high,low,vol}
        return [{"date": b["date"], "open": b["open"], "close": b["close"],
                 "high": b["high"], "low": b["low"], "vol": b["volume"]} for b in bars]

    if m == "yidian":
        # {date,open,close,high,low,vol,turn}
        return [{"date": b["date"], "open": b["open"], "close": b["close"],
                 "high": b["high"], "low": b["low"], "vol": b["volume"],
                 "turn": b.get("turnover", 0)} for b in bars]

    return bars  # 规范格式


def lixin_exclude():
    """共同层立信排除集；未就绪返回 None（模型回退自有逻辑）。"""
    if not ready():
        return None
    return cm.load_lixin_exclude()


def record(model, picks, date=None, extra=None):
    """便捷写推荐台账（条件④）。返回写入条数，失败返回 0。"""
    try:
        import kit
        return kit.ModelContext(model).record(picks, date=date, extra=extra)
    except Exception:
        return 0


# ------------------------------------------------------------------ 强制注入
_LIST_FUNCS = ("get_universe", "build_universe", "fetch_spot_all")
_KLINE_FUNCS = ("em_hist", "kline")


def install(model, *namespaces):
    """把命名空间里的「列表/K线取数函数」替换为共同层版本（强制生效）。

    只要 prefilter 已产出当日 meta.json（trading_day=True），调用本函数后，
    模型的数据入口就被替换 —— 即使模型自身完全没写 ST/立信/交易日判断，
    也只能拿到「已剔除 + 共享缓存」的数据，无法绕过共同条件。

    namespaces: 传 globals()（取数函数在本模块）或 vars(工具箱模块)。
    共同层未就绪时抛出 RuntimeError —— 各模型入口用 try/except 包住本调用，
    因此旧 workflow（未跑 prefilter）会完全走原来的取数/剔除/推送逻辑，
    不产生任何台账副作用，实现严格向后兼容（切换前旧自动化行为不变）。
    """
    if not ready():
        raise RuntimeError("common prefilter not ready (meta.json missing / non-trading-day)")
    m = (model or "").lower()
    kline_funcs = () if m == "kaizhong" else _KLINE_FUNCS  # 凯众保留自有新浪K线缓存
    patched = []

    def _mk_list(orig, mm):
        def w(*a, **k):
            c = universe_for(mm)
            return c if c is not None else orig(*a, **k)
        w._common_patched = True
        return w

    def _mk_kline(orig, mm):
        def w(code, *a, **k):
            cnt = None
            # 兼容 em_hist(code, count) / kline(code, board, days) 的第二个位置参数
            if a and isinstance(a[0], int):
                cnt = a[0]
            elif "count" in k:
                cnt = k["count"]
            elif "days" in k:
                cnt = k["days"]
            elif len(a) >= 2 and isinstance(a[-1], int):
                cnt = a[-1]
            bars = kline_for(code, mm, count=cnt)
            return bars if bars else orig(code, *a, **k)
        w._common_patched = True
        return w

    for ns in namespaces:
        for fn in _LIST_FUNCS:
            o = ns.get(fn)
            if callable(o) and not getattr(o, "_common_patched", False):
                ns[fn] = _mk_list(o, m)
                patched.append(fn)
        for fn in kline_funcs:
            o = ns.get(fn)
            if callable(o) and not getattr(o, "_common_patched", False):
                ns[fn] = _mk_kline(o, m)
                patched.append(fn)
    return patched
