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
  meta.json           本次运行元信息（trading_day / 各条件统计 / 降级状态 / 告警）

2026-11-13 变更（诊断报告第 2、3 项）：
  · fail-closed → fail-degraded：样本池为空且基础快照健康时，用快照强制重建
    样本池继续跑，而不是直接 exit 1 让整条流水线标红；快照也不可用才判定
    真实故障（保留失败状态，交给聚合告警与人工介入）。
  · base_universe.json 增加完整性校验（条数 / 字段 / 价格健全率），异常即告警。
  · K 线缓存覆盖率、熔断状态、降级标记写入 meta.json，覆盖率不足时推送告警，
    避免下游模型「静默使用残缺缓存」而无人知晓。
  · 告警一律 best-effort：渠道缺失/网络异常只打印日志，绝不影响退出码。

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

try:
    import notify
except Exception as _e:      # 告警模块异常绝不能阻断选股
    notify = None
    print("[prefilter] 告警模块不可用（不影响选股）：%s" % _e)

BJT = cm.BJT
ALERTS = []                 # 本次运行的告警汇总，写入 meta.json


def _alert(title, content):
    """尽最大努力发一条告警；任何失败都只记录，不影响流水线退出码。"""
    ALERTS.append({"title": title, "content": content})
    print("[prefilter][ALERT] %s | %s" % (title, content.replace("\n", " ")))
    if notify is None:
        return
    try:
        ok, detail = notify.send_alert(title, content)
        print("[prefilter][ALERT] 推送 %s：%s" % ("成功" if ok else "未送达", detail))
    except Exception as e:
        print("[prefilter][ALERT] 推送异常（忽略）：%s" % e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--f10-only", action="store_true", help="立信仅 F10 通道，跳过 26 公告扫描")
    ap.add_argument("--ann-full", action="store_true", help="立信 B 通道跑 2026 全年基线（默认增量窗口）")
    ap.add_argument("--no-kline", action="store_true", help="跳过全市场 K 线批量缓存（调试）")
    ap.add_argument("--no-kz", action="store_true", help="跳过凯众不复权 K 线预填（调试）")
    ap.add_argument("--no-lixin", action="store_true", help="跳过立信排除集更新，复用已有")
    args = ap.parse_args()

    today = datetime.datetime.now(BJT).strftime("%Y-%m-%d")
    print("=" * 64)
    print("共同前置层 prefilter  %s" % today)
    print("=" * 64)

    meta = {
        "date": today,
        "generated_at": datetime.datetime.now(BJT).isoformat(timespec="seconds"),
        "degraded": False,
        "alerts": ALERTS,
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

    # ---- 基础股票池快照健康校验（降级兜底的数据源必须先可信）----
    snap_ok, snap_reason, snap_rows = cm.check_base_universe_health()
    meta["snapshot_health"] = {
        "ok": snap_ok, "reason": snap_reason, "rows": len(snap_rows),
    }
    print("[prefilter] 基础快照健康校验：%s（%d 只）"
          % ("PASS" if snap_ok else "FAIL: " + snap_reason, len(snap_rows)))
    if not snap_ok:
        _alert("基础股票池快照异常",
               "base_universe.json 健康校验失败：%s。\n"
               "一旦实时行情接口被阻断，流水线将失去兜底数据源，请尽快重新种入富字段快照。"
               % snap_reason)

    # ---- 条件②：立信审计排除集（25年报=立信 OR 26续聘/改聘公告改立信） ----
    if args.no_lixin:
        lixin = cm.load_lixin_exclude()
        print("[prefilter] --no-lixin：复用已有排除集 %d 只" % len(lixin))
        meta["lixin_count"] = len(lixin)
    else:
        import lixin as lx
        base_u = cm.get_universe(verbose=False, apply_lixin=False)
        codes = [x["code"] for x in base_u]
        _, stats = lx.build(codes, do_ann=not args.f10_only,
                            ann_mode="full" if args.ann_full else "incremental")
        meta["lixin_count"] = stats.get("total_excluded", 0)
        meta["lixin_stats"] = stats

    # ---- 条件③ + 生成样本池（ST 名称硬剔除 + 立信物理剔除） ----
    universe = cm.get_universe()      # 内部已应用 ST + 立信 剔除
    if not universe:
        # fail-degraded：常规路径（已含 get_universe 内部的快照回退）都拿不到样本池，
        # 再用「已通过健康校验」的快照强制重建一次，而不是直接 exit 1 让整条流水线红。
        if snap_ok:
            print("[prefilter][ALERT] 实时样本池为空，尝试用基础快照降级继续（%s）。" % snap_reason)
            universe = cm.get_universe(force_snapshot=True)
        if not universe:
            _alert("前置层真实故障：样本池为空",
                   "实时行情与基础快照均不可用（%s），本次流水线终止。\n"
                   "这属于真实故障，保留失败状态以便聚合告警与人工介入。" % snap_reason)
            meta["trading_day"] = False
            meta["error"] = "universe_empty"
            meta["degraded"] = False
            cm.save_meta(meta)
            return 1
        meta["degraded"] = True
        _alert("前置层已降级：使用基础股票池快照",
               "实时行情源异常，样本池改由基础快照构建（%d 只）。\n"
               "ST / 立信硬剔除照常生效，但价格字段为快照口径（可能非当日），请留意结果口径。"
               % len(universe))
    cm.save_universe(universe)
    meta["universe_count"] = len(universe)
    ustatus = cm.universe_status()
    meta["universe_source"] = "snapshot" if meta["degraded"] else ustatus.get("source", "realtime")
    from collections import Counter
    meta["board_dist"] = dict(Counter(x["board"] for x in universe))

    # ---- 条件①：全市场行情记录（一次性 K 线缓存，供所有模型复用） ----
    if args.no_kline:
        print("[prefilter] --no-kline：跳过 K 线批量缓存。")
        meta["kline"] = {"skipped": True}
    else:
        ok, fail = cm.fetch_klines_batch(universe, count=cm.KLINE_CACHE_COUNT,
                                        workers=cm.KLINE_WORKERS)
        meta["kline_ok"] = ok
        meta["kline_fail"] = fail
        meta["kline"] = cm.kline_stats()
        st = meta["kline"]
        if st.get("degraded"):
            _alert("共享K线缓存覆盖率不足",
                   "成功 %d / 需拉 %d（覆盖率 %.1f%%），失败 %d、超预算跳过 %d。\n"
                   "下游模型将回退自行抓取，耗时与数据完整度受影响；"
                   "若持续发生请调 KLINE_BATCH_BUDGET 或检查兜底源。"
                   % (st["ok"], st["targets"], st["coverage"] * 100,
                      st["fail"], st["skip"]))

    # ---- 凯众专用：不复权 K 线预填（使凯众零网络抓取，口径与新浪逐日一致）----
    if args.no_kline or args.no_kz:
        print("[prefilter] 跳过凯众不复权 K 线预填。")
    else:
        try:
            kz_ok, kz_fail = cm.cache_kaizhong_klines(universe)
            meta["kz_kline_ok"] = kz_ok
            meta["kz_kline_fail"] = kz_fail
            meta["kz_kline"] = cm.kline_stats("kaizhong")
            st = meta["kz_kline"]
            if st.get("degraded"):
                _alert("凯众不复权K线覆盖率不足",
                       "成功 %d / 需预填 %d（覆盖率 %.1f%%），失败 %d、超预算跳过 %d；"
                       "凯众将自行回退新浪抓取。"
                       % (st["ok"], st["targets"], st["coverage"] * 100,
                          st["fail"], st["skip"]))
        except Exception as e:
            # 失败不阻断：凯众会自行回退新浪抓取，仅慢一些，不影响结果与正确性
            print("[prefilter] 凯众不复权 K 线预填失败（凯众自行回退）：%s" % e)
            meta["kz_kline_error"] = str(e)

    # ---- 可观测性汇总：写入 meta.json 供 workflow Step Summary / 排障使用 ----
    meta["data_source_status"] = cm.universe_status()
    meta["kline_source_status"] = cm.kline_source_status()
    cm.save_meta(meta)
    print("[prefilter] 完成：样本池 %d 只，立信排除 %d 只，trading_day=%s，"
          "来源=%s，降级=%s，告警=%d 条"
          % (len(universe), meta.get("lixin_count", 0), is_td,
             meta["universe_source"], meta["degraded"], len(ALERTS)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
