#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prefilter.py —— 共同前置层入口（合并流水线第一步，全局强制，仅跑一次）
========================================================================
四个共同条件的落地顺序（在单 runner 内只跑一次，供所有模型复用）：

  ① 全市场行情记录   : 批量缓存全市场 K 线 → common/state/klines/<code>.csv
  ② 立信审计剔除     : 生成 lixin_exclude.json（25年报审计=立信 OR 26续聘改聘公告改立信）
  ③ ST 全剔除        : 生成 universe.json 样本池时按名称硬剔除 ST/*ST/退市/次新
  ④ 回溯推荐+修正模型 : 由 run_all.py 的 settle_all() 对注册表所有模型统一结算/调参
                        （本步产出 universe 后即具备条件，实际结算在模型跑完后）

  ② ③ 属于「准入剔除」，在样本池生成阶段物理生效 —— 样本池里根本不含这些
  代码，任何下游模型（哪怕自身没写立信/ST 判断）都不可能选到，实现
  「即使模型条件里没有，也是遗漏补上」的强制约束。

产物（common/state/）：
  lixin_exclude.json  立信排除集（含命中依据）
  universe.json       已剔除 ST + 立信 的全市场样本池
  klines/*.csv        全市场行情 K 线缓存
  meta.json           本次运行元信息（trading_day / 各条件统计）

用法：
  python3 common/prefilter.py                  # 正式（全量）
  python3 common/prefilter.py --f10-only      # 立信仅查 F10（快，跳过公告扫描）
  python3 common/prefilter.py --no-kline      # 跳过 K 线批量（调试）
  python3 common/prefilter.py --no-lixin     # 跳过立信更新（复用已有排除集）
"""
import os
import sys
import argparse
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common_lib as cm

BJT = cm.BJT


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--f10-only", action="store_true", help="立信仅 F10 通道，跳过 26 公告扫描")
    ap.add_argument("--no-kline", action="store_true", help="跳过全市场 K 线批量缓存（调试）")
    ap.add_argument("--no-lixin", action="store_true", help="跳过立信排除集更新，复用已有")
    args = ap.parse_args()

    today = datetime.datetime.now(BJT).strftime("%Y-%m-%d")
    print("=" * 64)
    print("共同前置层 prefilter  %s" % today)
    print("=" * 64)

    meta = {
        "date": today,
        "generated_at": datetime.datetime.now(BJT).isoformat(timespec="seconds"),
    }

    # ---- 交易日 / 行情更新判定 ----
    is_td, quote_date, _ = cm.check_trading_day()
    meta["quote_date"] = quote_date
    meta["trading_day"] = bool(is_td)
    if not is_td:
        print("[prefilter] 非交易日 / 行情未更新（最新日 %s ≠ 当日 %s），"
              "整体跳过，仅产出 meta。" % (quote_date, today))
        cm.save_meta(meta)
        return 0

    # ---- 条件②：立信审计排除集（25年报=立信 OR 26续聘/改聘公告改立信） ----
    if args.no_lixin:
        lixin = cm.load_lixin_exclude()
        print("[prefilter] --no-lixin：复用已有排除集 %d 只" % len(lixin))
        meta["lixin_count"] = len(lixin)
    else:
        import lixin as lx
        base_u = cm.get_universe(verbose=False, apply_lixin=False)
        codes = [x["code"] for x in base_u]
        _, stats = lx.build(codes, do_ann=not args.f10_only)
        meta["lixin_count"] = stats.get("total_excluded", 0)
        meta["lixin_stats"] = stats

    # ---- 条件③ + 生成样本池（ST 名称硬剔除 + 立信物理剔除） ----
    universe = cm.get_universe()      # 内部已应用 ST + 立信 剔除
    if not universe:
        print("[prefilter] 样本池为空，疑似数据源异常，终止。")
        meta["trading_day"] = False
        meta["error"] = "universe_empty"
        cm.save_meta(meta)
        return 1
    cm.save_universe(universe)
    meta["universe_count"] = len(universe)
    from collections import Counter
    meta["board_dist"] = dict(Counter(x["board"] for x in universe))

    # ---- 条件①：全市场行情记录（一次性 K 线缓存，供所有模型复用） ----
    if args.no_kline:
        print("[prefilter] --no-kline：跳过 K 线批量缓存。")
    else:
        ok, fail = cm.fetch_klines_batch(universe, count=cm.KLINE_CACHE_COUNT,
                                        workers=cm.KLINE_WORKERS)
        meta["kline_ok"] = ok
        meta["kline_fail"] = fail

    cm.save_meta(meta)
    print("[prefilter] 完成：样本池 %d 只，立信排除 %d 只，trading_day=%s"
          % (len(universe), meta.get("lixin_count", 0), is_td))
    return 0


if __name__ == "__main__":
    sys.exit(main())
