#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回补 L1 每日大盘环境历史快照 state/market_env.jsonl（生产上线前置步骤）。

数据来源：low_shadow/state/env_factor_result.json
  · panel     : 27 交易日逐日全市场等权涨跌 eq_chg、8 道硬条件通过数 n_base、H5 数 n_h5
  · daily_env : 逐日环境档（仅参考；本脚本按生产口径重算 regime）

与生产 compute_market_env() 口径严格对齐：
  - eq_chg_pct   : 全市场等权当日涨跌（百分数数值，如 -2.082 表示 -2.082%）
  - regime       : >0 强 / [-1, 0] 中性 / < -1 弱（与 env_advice / 回测分档一致）
  - n_base       : 8 道硬条件通过数 = n_qualified + sum(h5_funnel.values())
  - n_qualified  : H5 精选后数量（对应 panel.n_h5）
  - adv_ratio_pct: 历史面板未留存 → 留空；生产历史计算只依赖 eq_chg_pct / n_base

本脚本只写 state/market_env.jsonl，不参与任何筛选 / 评分 / 参数决策。
可重复执行（全量重建）；生产运行时的同日去重由 _append_env_history() 负责。
"""
import json
import os

BASE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(BASE, "state", "env_factor_result.json")
DST = os.path.join(BASE, "state", "market_env.jsonl")


def regime_of(eq):
    if eq is None:
        return None
    return "强" if eq > 0 else ("弱" if eq < -1.0 else "中性")


def main():
    d = json.load(open(SRC, encoding="utf-8"))
    panel = d["panel"]
    rows = []
    for x in panel:
        eq = x.get("eq_chg")
        rows.append({
            "date": x["date"],
            "eq_chg_pct": eq,
            "regime": regime_of(eq),
            "n_base": x.get("n_base"),
            "n_qualified": x.get("n_h5"),
            "source": "env_factor_result.json/panel (10-08~11-13 全市场回测)",
        })
    os.makedirs(os.path.dirname(DST), exist_ok=True)
    with open(DST, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("回补完成：%d 条 → %s" % (len(rows), DST))
    for r in rows[-5:]:
        print("  %s  eq=%s  regime=%s  n_base=%s  n_qualified=%s" % (
            r["date"], r["eq_chg_pct"], r["regime"], r["n_base"], r["n_qualified"]))


if __name__ == "__main__":
    main()
